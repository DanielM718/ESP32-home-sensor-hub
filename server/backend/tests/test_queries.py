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
    AIR_QUALITY_FIELDS,
    QueryValidationError,
    air_quality_context_response,
    events_flux,
    latest_flux,
    latest_with_node_status,
    latest_response,
    nodes_response,
    readings_flux,
    readings_query_from_params,
    readings_response,
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
        self.assertIn("aggregateWindow(every: 1m", flux)

    def test_air_quality_queries_include_every_sen66_field(self) -> None:
        query = readings_query_from_params(
            {"range": "24h", "sensor_type": "air_quality"}
        )

        for flux in (
            latest_flux("environment"),
            readings_flux("environment", query),
        ):
            for field in AIR_QUALITY_FIELDS:
                with self.subTest(field=field):
                    self.assertIn(f'"{field}"', flux)

    def test_latest_context_finds_active_events_older_than_a_day(self) -> None:
        from app.queries import air_quality_context_flux

        flux = air_quality_context_flux("environment", "environment_live")

        self.assertIn("activeEventStates", flux)
        self.assertIn("|> range(start: 0)", flux)
        latest_state = flux.index("|> last()")
        active_filter = flux.index('r._value == "active"')
        self.assertLess(latest_state, active_filter)
        self.assertIn('group(columns: ["location", "event_type"])', flux)

    def test_event_history_separates_mixed_value_types_by_field(self) -> None:
        query = readings_query_from_params(
            {"range": "24h", "sensor_type": "air_quality"}
        )

        flux = events_flux("environment", query)

        self.assertIn(
            'group(columns: ["location", "topic", "event_type", "metric", "_field", "_time"])',
            flux,
        )
        self.assertIn('|> sort(columns: ["_time"])', flux)

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

        battery_value_map = (
            '|> map(fn: (r) => ({r with _value: float(v: r.battery_mv)}))'
        )
        battery_field_map = '|> map(fn: (r) => ({r with _field: "battery_mv"}))'
        value_map_index = flux.index(battery_value_map)
        aggregate_index = flux.index("|> aggregateWindow", value_map_index)
        field_map_index = flux.index(battery_field_map)
        self.assertLess(value_map_index, aggregate_index)
        self.assertLess(aggregate_index, field_map_index)

    def test_all_history_unions_valid_environment_battery_and_air_quality(self) -> None:
        query = readings_query_from_params({"range": "7d"})

        flux = readings_flux("environment", query)

        self.assertIn("environmentMetrics", flux)
        self.assertIn("environmentBattery", flux)
        self.assertIn("airAggregateMean", flux)
        self.assertIn("airAggregateMax", flux)
        self.assertIn("legacyAirMean", flux)
        self.assertIn("union(tables:", flux)

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

    def test_latest_response_returns_every_sen66_field(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        station = latest_response(_air_quality_records(now))["air_quality"][0]

        self.assertEqual(station["location"], "printer_room")
        self.assertEqual(station["topic"], "home/air/printer_room")
        self.assertEqual(
            {field: station[field] for field in AIR_QUALITY_FIELDS},
            {
                "co2": 721,
                "pm1": 1.1,
                "pm25": 2.8,
                "pm4": 3.5,
                "pm10": 5.2,
                "voc_index": 88,
                "nox_index": 12,
                "temperature_c": 24.5,
                "humidity": 42.3,
            },
        )

    def test_latest_response_does_not_reuse_older_raw_diagnostic_ticks(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        values = {
            "location": "printer_room",
            "topic": "home/air/printer_room",
            "sensor_type": "air_quality",
        }
        records = _air_quality_records(now)
        records.append(
            FakeRecord("air_quality_reading", "sample_valid", True, now, values)
        )
        records.append(
            FakeRecord(
                "air_quality_reading",
                "sraw_voc",
                24000,
                now - timedelta(seconds=5),
                values,
            )
        )

        station = latest_response(records)["air_quality"][0]

        self.assertIsNone(station["sraw_voc"])

    def test_latest_response_does_not_reuse_older_invalid_flag(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        values = {
            "location": "printer_room",
            "topic": "home/air/printer_room",
            "sensor_type": "air_quality",
        }
        records = _air_quality_records(now)
        records.append(
            FakeRecord(
                "air_quality_reading",
                "sample_valid",
                False,
                now - timedelta(seconds=5),
                values,
            )
        )

        station = latest_response(records)["air_quality"][0]

        self.assertIsNone(station["sample_valid"])
        self.assertEqual(station["co2"], 721)

    def test_air_quality_history_tolerates_missing_legacy_fields(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        records = [
            record
            for record in _air_quality_records(now)
            if record.field in {"temperature_c", "humidity", "co2", "pm25"}
        ]
        query = readings_query_from_params(
            {"range": "24h", "sensor_type": "air_quality"}
        )

        response = readings_response(records, query)

        self.assertEqual(len(response["series"]), 1)
        point = response["series"][0]["points"][0]
        self.assertEqual(point["co2"], 721)
        self.assertEqual(point["pm25"], 2.8)
        self.assertNotIn("pm1", point)
        self.assertNotIn("voc_index", point)

    def test_history_keeps_only_newest_orphaned_active_event(self) -> None:
        now = datetime(2026, 7, 21, 12, 5, tzinfo=timezone.utc)
        values = {
            "location": "office",
            "topic": "home/air/office",
            "sensor_type": "air_quality",
            "event_type": "pm25_current_level",
            "metric": "pm25",
        }
        event_records = []
        for offset in (0, 15):
            event_records.extend(
                (
                    FakeRecord(
                        "air_quality_event",
                        "state",
                        "active",
                        now + timedelta(seconds=offset),
                        values,
                    ),
                    FakeRecord(
                        "air_quality_event",
                        "trigger_value",
                        180.0,
                        now + timedelta(seconds=offset),
                        values,
                    ),
                )
            )
        query = readings_query_from_params(
            {"range": "24h", "sensor_type": "air_quality"}
        )

        response = readings_response([], query, event_records=event_records)

        self.assertEqual(len(response["events"]), 1)
        self.assertEqual(
            response["events"][0]["time"],
            (now + timedelta(seconds=15)).isoformat().replace("+00:00", "Z"),
        )

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

    def test_air_quality_node_uses_publish_rate_stale_timeout(self) -> None:
        generated = datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc)
        latest = latest_response(
            _air_quality_records(generated - timedelta(seconds=25))
        )
        latest["generated_at"] = generated.isoformat()

        response = nodes_response(
            latest,
            stale_after_seconds=1800,
            air_quality_stale_after_seconds=20,
        )

        self.assertEqual(response["nodes"][0]["status"], "stale")
        self.assertEqual(response["air_quality_stale_after_seconds"], 20)

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

    def test_context_keeps_simultaneous_event_types_separate(self) -> None:
        now = datetime(2026, 7, 21, 12, 5, tzinfo=timezone.utc)
        base_values = {
            "location": "office",
            "topic": "home/air/office",
            "sensor_type": "air_quality",
        }
        records = [
            FakeRecord(
                "air_quality_reading",
                "sample_valid",
                True,
                now,
                base_values,
            )
        ]
        for event_type, metric in (
            ("voc_action_level", "voc_index"),
            ("voc_rapid_rise", "voc_index"),
        ):
            event_values = {
                **base_values,
                "event_type": event_type,
                "metric": metric,
            }
            records.append(
                FakeRecord(
                    "air_quality_event",
                    "state",
                    "active",
                    now,
                    event_values,
                )
            )

        response = air_quality_context_response(
            records,
            expected_publish_seconds=5,
            minimum_coverage_percent=75,
        )

        active = response["locations"]["office"]["active_events"]
        self.assertEqual(
            {event["event_type"] for event in active},
            {"voc_action_level", "voc_rapid_rise"},
        )

    def test_context_keeps_only_latest_state_for_each_event_type(self) -> None:
        now = datetime(2026, 7, 21, 12, 5, tzinfo=timezone.utc)
        base_values = {
            "location": "office",
            "topic": "home/air/office",
            "sensor_type": "air_quality",
            "event_type": "pm25_current_level",
            "metric": "pm25",
        }
        records = [
            FakeRecord(
                "air_quality_event",
                "state",
                "active",
                now,
                base_values,
            ),
            FakeRecord(
                "air_quality_event",
                "state",
                "active",
                now + timedelta(seconds=15),
                base_values,
            ),
            FakeRecord(
                "air_quality_event",
                "state",
                "completed",
                now + timedelta(seconds=30),
                base_values,
            ),
        ]

        response = air_quality_context_response(
            records,
            expected_publish_seconds=5,
            minimum_coverage_percent=75,
        )

        self.assertEqual(response["locations"]["office"]["active_events"], [])

    def test_current_summary_excludes_all_fields_from_invalid_samples(self) -> None:
        now = datetime(2026, 7, 21, 12, 5, tzinfo=timezone.utc)
        values = {
            "location": "office",
            "topic": "home/air/office",
            "sensor_type": "air_quality",
        }
        records = [
            FakeRecord("air_quality_reading", "sample_valid", True, now, values),
            FakeRecord("air_quality_reading", "co2", 700, now, values),
            FakeRecord("air_quality_reading", "voc_index", 100, now, values),
            FakeRecord(
                "air_quality_reading",
                "sample_valid",
                False,
                now + timedelta(seconds=5),
                values,
            ),
            FakeRecord(
                "air_quality_reading",
                "co2",
                5000,
                now + timedelta(seconds=5),
                values,
            ),
            FakeRecord(
                "air_quality_reading",
                "voc_index",
                200,
                now + timedelta(seconds=5),
                values,
            ),
        ]

        response = air_quality_context_response(
            records,
            expected_publish_seconds=5,
            minimum_coverage_percent=75,
        )

        summary = response["locations"]["office"]["current_15m"]
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["valid_sample_count"], 1)
        self.assertEqual(summary["invalid_sample_count"], 1)
        self.assertEqual(summary["co2_mean"], 700)
        self.assertEqual(summary["co2_max"], 700)
        self.assertEqual(summary["voc_index_mean"], 100)
        self.assertEqual(summary["voc_duration_above_150_seconds"], 0)

    def test_overall_status_includes_direct_co2_exposure_warning_only_when_relevant(self) -> None:
        from app.queries import _overall_air_quality_status

        normal = _overall_air_quality_status(
            {
                "co2": {"severity": "good", "category": "Effective"},
                "co2_occupational": {
                    "severity": "informational",
                    "category": "Below occupational values",
                },
            }
        )
        dangerous = _overall_air_quality_status(
            {
                "co2": {"severity": "very_poor", "category": "Ventilate"},
                "co2_occupational": {
                    "severity": "hazardous",
                    "category": "At or above NIOSH IDLH numeric value",
                },
            }
        )

        self.assertEqual(normal["driving_metric"], "co2")
        self.assertEqual(dangerous["driving_metric"], "co2_occupational")
        self.assertEqual(dangerous["severity"], "hazardous")


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


def _air_quality_records(now: datetime) -> list[FakeRecord]:
    values = {
        "location": "printer_room",
        "topic": "home/air/printer_room",
        "sensor_type": "air_quality",
    }
    fields: list[tuple[str, object]] = [
        ("co2", 721),
        ("pm1", 1.1),
        ("pm25", 2.8),
        ("pm4", 3.5),
        ("pm10", 5.2),
        ("voc_index", 88),
        ("nox_index", 12),
        ("temperature_c", 24.5),
        ("humidity", 42.3),
    ]
    return [
        FakeRecord("air_quality_reading", field, value, now, values)
        for field, value in fields
    ]


if __name__ == "__main__":
    unittest.main()
