from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import unittest

from app.battery_status import (
    STATUS_BATTERY_LOW,
    STATUS_BATTERY_OK,
    STATUS_BATTERY_SHUTDOWN,
)
from app.queries import (
    QueryValidationError,
    latest_with_node_status,
    latest_response,
    nodes_response,
    readings_flux,
    readings_query_from_params,
)


@dataclass(frozen=True)
class FakeRecord:
    measurement: str
    field: str
    value: object
    time: datetime
    values: dict[str, object]

    def get_measurement(self) -> str:
        return self.measurement

    def get_field(self) -> str:
        return self.field

    def get_value(self) -> object:
        return self.value

    def get_time(self) -> datetime:
        return self.time


class QueryHelpersTest(unittest.TestCase):
    def test_default_readings_query(self) -> None:
        query = readings_query_from_params({})

        self.assertEqual(query.range_key, "24h")
        self.assertEqual(query.flux_start, "-24h")
        self.assertEqual(query.window_every, "15m")
        self.assertEqual(query.sensor_type, "all")

    def test_invalid_range_is_rejected(self) -> None:
        with self.assertRaises(QueryValidationError):
            readings_query_from_params({"range": "2y"})

    def test_incompatible_filter_is_rejected(self) -> None:
        with self.assertRaises(QueryValidationError):
            readings_query_from_params(
                {"sensor_type": "air_quality", "node_id": "1"}
            )

    def test_readings_flux_escapes_location_filter(self) -> None:
        query = readings_query_from_params(
            {"range": "1h", "sensor_type": "air_quality", "location": "printer_room"}
        )

        flux = readings_flux("environment", query)

        self.assertIn('from(bucket: "environment")', flux)
        self.assertIn('r.location == "printer_room"', flux)
        self.assertIn("aggregateWindow(every: 15m", flux)

    def test_environment_history_requires_matching_battery_ok_flag(self) -> None:
        query = readings_query_from_params(
            {"range": "24h", "sensor_type": "environment", "node_id": "1"}
        )

        flux = readings_flux("environment", query)

        self.assertIn('import "bitwise"', flux)
        self.assertIn('r._field == "battery_mv" or r._field == "status_flags"', flux)
        self.assertIn(
            '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")',
            flux,
        )
        self.assertIn("exists r.battery_mv", flux)
        self.assertIn("exists r.status_flags", flux)
        self.assertIn("bitwise.sand(a: r.status_flags, b: 4) > 0", flux)
        self.assertNotIn("r.status_flags == 4", flux)

    def test_all_history_unions_valid_environment_battery_and_air_quality(self) -> None:
        query = readings_query_from_params({"range": "7d"})

        flux = readings_flux("environment", query)

        self.assertIn(
            "union(tables: [environmentMetrics, environmentBattery, airQuality])",
            flux,
        )

    def test_all_history_with_node_filter_excludes_air_quality(self) -> None:
        query = readings_query_from_params({"node_id": "1"})

        flux = readings_flux("environment", query)

        self.assertIn('r.node_id == "1"', flux)
        self.assertNotIn("airQuality =", flux)

    def test_latest_response_groups_fields_by_node(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        records = [
            FakeRecord(
                "environment_reading",
                "temperature_c",
                24.8,
                now,
                {
                    "node_id": "1",
                    "topic": "home/sensors/1",
                    "sensor_type": "environment",
                },
            ),
            FakeRecord(
                "environment_reading",
                "humidity",
                41.6,
                now + timedelta(seconds=1),
                {
                    "node_id": "1",
                    "topic": "home/sensors/1",
                    "sensor_type": "environment",
                },
            ),
        ]

        response = latest_response(records)

        self.assertEqual(len(response["environment"]), 1)
        node = response["environment"][0]
        self.assertEqual(node["node_id"], 1)
        self.assertEqual(node["temperature_c"], 24.8)
        self.assertEqual(node["humidity"], 41.6)
        self.assertEqual(node["last_seen"], "2026-01-01T12:00:01Z")
        self.assertIsNone(node["status_flags"])
        self.assertIsNone(node["battery_measurement_ok"])
        self.assertIsNone(node["battery_low"])
        self.assertIsNone(node["battery_shutdown"])
        self.assertIsNone(node["battery_mv"])

    def test_latest_response_decodes_battery_status_bits(self) -> None:
        cases = (
            (0, False, False, False, None),
            (STATUS_BATTERY_OK, True, False, False, 4058),
            (
                STATUS_BATTERY_OK | STATUS_BATTERY_LOW,
                True,
                True,
                False,
                4058,
            ),
            (
                STATUS_BATTERY_OK | STATUS_BATTERY_LOW | STATUS_BATTERY_SHUTDOWN,
                True,
                True,
                True,
                4058,
            ),
        )

        for status_flags, ok, low, shutdown, battery_mv in cases:
            with self.subTest(status_flags=status_flags):
                node = _latest_environment_node(
                    status_flags=status_flags,
                    battery_mv=4058,
                )

                self.assertEqual(node["status_flags"], status_flags)
                self.assertIs(node["battery_measurement_ok"], ok)
                self.assertIs(node["battery_low"], low)
                self.assertIs(node["battery_shutdown"], shutdown)
                self.assertEqual(node["battery_mv"], battery_mv)

    def test_latest_response_treats_missing_status_as_unavailable(self) -> None:
        node = _latest_environment_node(status_flags=None, battery_mv=4058)

        self.assertIsNone(node["status_flags"])
        self.assertIsNone(node["battery_measurement_ok"])
        self.assertIsNone(node["battery_low"])
        self.assertIsNone(node["battery_shutdown"])
        self.assertIsNone(node["battery_mv"])

    def test_latest_response_does_not_treat_zero_without_ok_as_measured(self) -> None:
        node = _latest_environment_node(status_flags=0, battery_mv=0)

        self.assertFalse(node["battery_measurement_ok"])
        self.assertIsNone(node["battery_mv"])

    def test_older_shutdown_flag_is_not_attached_to_newer_flagless_packet(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        records = _environment_records(
            now,
            status_flags=(
                STATUS_BATTERY_OK | STATUS_BATTERY_LOW | STATUS_BATTERY_SHUTDOWN
            ),
            battery_mv=3190,
        )
        for index, record in enumerate(records):
            if record.field != "status_flags":
                records[index] = FakeRecord(
                    record.measurement,
                    record.field,
                    record.value,
                    now + timedelta(minutes=15),
                    record.values,
                )

        node = latest_response(records)["environment"][0]

        self.assertIsNone(node["status_flags"])
        self.assertIsNone(node["battery_shutdown"])
        self.assertIsNone(node["battery_mv"])

    def test_nodes_response_marks_stale_nodes(self) -> None:
        latest = {
            "generated_at": "2026-01-01T12:30:00Z",
            "environment": [
                {
                    "id": "1",
                    "sensor_type": "environment",
                    "topic": "home/sensors/1",
                    "node_id": 1,
                    "last_seen": "2026-01-01T12:00:00Z",
                    "battery_mv": 4058,
                    "status_flags": STATUS_BATTERY_OK,
                    "battery_measurement_ok": True,
                    "battery_low": False,
                    "battery_shutdown": False,
                }
            ],
            "air_quality": [],
        }

        response = nodes_response(latest, stale_after_seconds=600)

        self.assertEqual(response["nodes"][0]["status"], "stale")
        self.assertEqual(response["nodes"][0]["age_seconds"], 1800)
        self.assertEqual(response["nodes"][0]["stale_reason"], "no_recent_reading")

    def test_nodes_response_preserves_confirmed_shutdown_while_stale(self) -> None:
        latest = {
            "generated_at": "2026-01-01T12:30:00Z",
            "environment": [
                {
                    "id": "1",
                    "sensor_type": "environment",
                    "topic": "home/sensors/1",
                    "node_id": 1,
                    "last_seen": "2026-01-01T12:00:00Z",
                    "battery_mv": 3190,
                    "status_flags": (
                        STATUS_BATTERY_OK
                        | STATUS_BATTERY_LOW
                        | STATUS_BATTERY_SHUTDOWN
                    ),
                    "battery_measurement_ok": True,
                    "battery_low": True,
                    "battery_shutdown": True,
                }
            ],
            "air_quality": [],
        }

        node = nodes_response(latest, stale_after_seconds=600)["nodes"][0]

        self.assertEqual(node["status"], "stale")
        self.assertEqual(node["stale_reason"], "battery_shutdown")
        self.assertTrue(node["battery_shutdown"])

    def test_latest_snapshot_includes_node_status_without_another_query(self) -> None:
        latest = {
            "generated_at": "2026-01-01T12:00:00Z",
            "environment": [
                {
                    "id": "1",
                    "sensor_type": "environment",
                    "node_id": 1,
                    "last_seen": "2026-01-01T11:59:00Z",
                    "battery_mv": 4058,
                    "status_flags": STATUS_BATTERY_OK,
                    "battery_measurement_ok": True,
                    "battery_low": False,
                    "battery_shutdown": False,
                }
            ],
            "air_quality": [],
        }

        response = latest_with_node_status(latest, stale_after_seconds=1800)

        self.assertEqual(response["stale_after_seconds"], 1800)
        self.assertEqual(len(response["nodes"]), 1)
        self.assertEqual(response["nodes"][0]["status"], "online")
        self.assertEqual(response["nodes"][0]["status_flags"], STATUS_BATTERY_OK)
        self.assertTrue(response["nodes"][0]["battery_measurement_ok"])
        self.assertFalse(response["nodes"][0]["battery_low"])
        self.assertFalse(response["nodes"][0]["battery_shutdown"])
        self.assertNotIn("nodes", latest)


def _latest_environment_node(
    *,
    status_flags: int | None,
    battery_mv: int,
) -> dict[str, object]:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    records = _environment_records(
        now,
        status_flags=status_flags,
        battery_mv=battery_mv,
    )
    return latest_response(records)["environment"][0]


def _environment_records(
    now: datetime,
    *,
    status_flags: int | None,
    battery_mv: int,
) -> list[FakeRecord]:
    values = {
        "node_id": "1",
        "topic": "home/sensors/1",
        "sensor_type": "environment",
    }
    fields: list[tuple[str, object]] = [
        ("sequence", 1523),
        ("temperature_c", 24.8),
        ("humidity", 41.6),
        ("battery_mv", battery_mv),
    ]
    if status_flags is not None:
        fields.append(("status_flags", status_flags))

    return [
        FakeRecord("environment_reading", field, value, now, values)
        for field, value in fields
    ]


if __name__ == "__main__":
    unittest.main()
