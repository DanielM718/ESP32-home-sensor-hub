from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from unittest.mock import Mock, patch

from app.air_quality_policy import interpret_station
from app.battery_status import (
    STATUS_BATTERY_LOW,
    STATUS_BATTERY_OK,
    STATUS_BATTERY_SHUTDOWN,
)
from app.config import InfluxSettings
from app.queries import (
    AIR_QUALITY_FIELDS,
    SENSOR_TYPES,
    DurableInventoryCache,
    InfluxReadRepository,
    QueryValidationError,
    air_quality_context_flux,
    air_quality_context_response,
    air_quality_sensor_status,
    durable_inventory_flux,
    events_flux,
    latest_flux,
    latest_response,
    latest_with_node_status,
    nodes_response,
    readings_flux,
    readings_query_from_params,
    readings_response,
)
from app.workflows import FIELDS_BY_SENSOR_TYPE, NUMERIC_FIELDS

FIELDS_BY_SENSOR_TYPE_LOOKUP = {
    field: tuple(
        sensor_type
        for sensor_type, fields in FIELDS_BY_SENSOR_TYPE.items()
        if field in fields
    )
    for fields in FIELDS_BY_SENSOR_TYPE.values()
    for field in fields
}


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

    def test_generic_readings_route_does_not_accept_printer_source_types(self) -> None:
        for sensor_type in ("printer", "ams"):
            with (
                self.subTest(sensor_type=sensor_type),
                self.assertRaises(QueryValidationError),
            ):
                readings_query_from_params({"sensor_type": sensor_type})

    def test_incompatible_filter_is_rejected(self) -> None:
        with self.assertRaises(QueryValidationError):
            readings_query_from_params({"sensor_type": "air_quality", "node_id": "1"})

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

    def test_latest_reduces_each_source_before_union(self) -> None:
        flux = latest_flux("environment", "environment_live")

        environment_start = flux.index("environmentLatest =")
        air_start = flux.index("airQualityLatest =")
        union_start = flux.index("union(tables:")
        environment_section = flux[environment_start:air_start]
        air_section = flux[air_start:union_start]

        self.assertIn('from(bucket: "environment")', environment_section)
        self.assertIn("|> range(start: -7d)", environment_section)
        self.assertIn('r._measurement == "environment_reading"', environment_section)
        self.assertNotIn('"air_quality_reading"', environment_section)
        self.assertIn("|> group(", environment_section)
        self.assertIn("|> last()", environment_section)

        self.assertIn('from(bucket: "environment_live")', air_section)
        self.assertIn('r._measurement == "air_quality_reading"', air_section)
        self.assertIn("|> group(", air_section)
        self.assertIn("|> last()", air_section)
        self.assertEqual(flux.count("|> last()"), 2)
        self.assertLess(flux.rindex("|> last()"), union_start)

    def test_durable_inventory_reconstructs_known_sensors_from_permanent_history(
        self,
    ) -> None:
        flux = durable_inventory_flux("environment")

        environment_inventory = flux[
            flux.index("environmentInventory =") : flux.index("airQualityInventory =")
        ]
        air_inventory = flux[
            flux.index("airQualityInventory =") : flux.index("union(tables:")
        ]
        self.assertIn("|> range(start: 0)", environment_inventory)
        self.assertIn('r._measurement == "environment_reading"', environment_inventory)
        self.assertIn("|> range(start: 0)", air_inventory)
        self.assertIn('r._measurement == "air_quality_15m"', air_inventory)
        self.assertIn('"co2_mean"', air_inventory)
        self.assertIn("printerTelemetryInventory", flux)
        self.assertIn('r._measurement == "printer_telemetry"', flux)

    def test_printer_and_multiple_ams_sources_are_discovered_and_remain_stale(
        self,
    ) -> None:
        records = [
            FakeRecord(
                "printer_telemetry",
                "chamber_temperature_c",
                31.5,
                datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
                {
                    "printer_id": "x2d",
                    "component_type": "printer",
                    "component_id": "main",
                    "source": "home_assistant",
                },
            ),
            FakeRecord(
                "printer_telemetry",
                "ams_humidity",
                22.0,
                datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
                {
                    "printer_id": "x2d",
                    "component_type": "ams",
                    "component_id": "ams_1",
                    "source": "home_assistant",
                },
            ),
            FakeRecord(
                "printer_telemetry",
                "ams_humidity",
                40.0,
                datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
                {
                    "printer_id": "x2d",
                    "component_type": "ams",
                    "component_id": "ams_2",
                    "source": "home_assistant",
                },
            ),
        ]
        latest = latest_response(records)

        self.assertEqual([item["id"] for item in latest["printer"]], ["x2d"])
        self.assertEqual(
            [item["id"] for item in latest["ams"]],
            ["x2d/ams_1", "x2d/ams_2"],
        )
        self.assertEqual(latest["ams"][0]["available_fields"], ["ams_humidity"])

        latest["generated_at"] = "2026-08-02T12:00:00Z"
        nodes = nodes_response(latest, stale_after_seconds=60)["nodes"]
        ams = [item for item in nodes if item["sensor_type"] == "ams"]
        self.assertEqual(len(ams), 2)
        self.assertTrue(all(item["status"] == "offline" for item in ams))
        self.assertEqual(ams[0]["source_id"], "x2d/ams_1")

    def test_latest_query_never_scans_unbounded_history(self) -> None:
        flux = latest_flux("environment", "environment_live")

        self.assertNotIn("range(start: 0)", flux)
        self.assertNotIn("environmentInventory", flux)
        self.assertNotIn("airQualityInventory", flux)
        self.assertIn("range(start: -7d)", flux)
        self.assertIn("range(start: -30m)", flux)

    def test_latest_live_lookback_is_bounded_and_shorter_than_retention(self) -> None:
        flux = latest_flux("environment", "environment_live")
        air_section = flux[flux.index("airQualityLatest =") :]

        self.assertIn("|> range(start: -30m)", air_section)
        self.assertNotIn("-3d", air_section)
        self.assertNotIn("-72h", air_section)

    def test_latest_context_finds_active_events_older_than_a_day(self) -> None:
        flux = air_quality_context_flux("environment", "environment_live")

        self.assertIn("activeEventStates", flux)
        self.assertIn("|> range(start: 0)", flux)
        latest_state = flux.index("|> last()")
        active_filter = flux.index('r._value == "active"')
        self.assertLess(latest_state, active_filter)
        self.assertIn('group(columns: ["location", "event_type"])', flux)

    def test_context_uses_aligned_live_window_and_permanent_aggregates(self) -> None:
        flux = air_quality_context_flux("environment", "environment_live")

        live_section = flux[flux.index("live =") : flux.index("aggregates =")]
        aggregate_section = flux[
            flux.index("aggregates =") : flux.index("activeEventStates =")
        ]
        self.assertIn("date.truncate(t: now(), unit: 15m)", live_section)
        self.assertIn('from(bucket: "environment_live")', live_section)
        self.assertIn('from(bucket: "environment")', aggregate_section)
        self.assertIn('r._measurement == "air_quality_15m"', aggregate_section)
        self.assertIn("|> range(start: -25h)", aggregate_section)
        self.assertNotIn('|> sort(columns: ["_time"])', flux)

    def test_event_history_separates_mixed_value_types_by_field(self) -> None:
        query = readings_query_from_params(
            {"range": "24h", "sensor_type": "air_quality"}
        )

        flux = events_flux("environment", query)

        self.assertIn(
            'group(columns: ["location", "topic", "event_type", "metric", "_field"])',
            flux,
        )
        self.assertNotIn('|> sort(columns: ["_time"])', flux)

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
            "|> map(fn: (r) => ({r with _value: float(v: r.battery_mv)}))"
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
        self.assertIn("airAggregateP95", flux)
        self.assertNotIn("legacyAirMean", flux)
        self.assertNotIn('r._measurement == "air_quality_reading"', flux)
        self.assertIn("union(tables:", flux)

    def test_one_hour_air_history_uses_only_live_raw_tier(self) -> None:
        query = readings_query_from_params(
            {"range": "1h", "sensor_type": "air_quality", "location": "office"}
        )

        flux = readings_flux("environment", query, live_bucket="environment_live")

        self.assertIn('from(bucket: "environment_live")', flux)
        self.assertNotIn('from(bucket: "environment")', flux)
        self.assertIn('r._measurement == "air_quality_reading"', flux)

    def test_long_air_histories_use_only_permanent_aggregate_tier(self) -> None:
        for range_key in ("24h", "7d", "30d"):
            with self.subTest(range_key=range_key):
                query = readings_query_from_params(
                    {"range": range_key, "sensor_type": "air_quality"}
                )
                flux = readings_flux(
                    "environment", query, live_bucket="environment_live"
                )
                self.assertIn('r._measurement == "air_quality_15m"', flux)
                self.assertNotIn('r._measurement == "air_quality_reading"', flux)

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
        self.assertEqual(node["available_fields"], ["temperature_c", "humidity"])
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
        self.assertEqual(set(station["available_fields"]), set(AIR_QUALITY_FIELDS))

    def test_permanent_air_quality_aggregate_restores_identity_and_capabilities(
        self,
    ) -> None:
        old = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        values = {
            "location": "printer_room",
            "topic": "home/air/printer_room",
            "sensor_type": "air_quality",
        }
        station = latest_response(
            [
                FakeRecord("air_quality_15m", "co2_mean", 721.0, old, values),
                FakeRecord("air_quality_15m", "temperature_c_mean", 24.5, old, values),
                FakeRecord("air_quality_15m", "humidity_mean", 42.3, old, values),
            ]
        )["air_quality"][0]

        self.assertEqual(station["id"], "printer_room")
        self.assertEqual(station["last_seen"], "2026-01-01T12:00:00Z")
        self.assertEqual(station["co2"], 721.0)
        self.assertEqual(
            station["available_fields"], ["co2", "temperature_c", "humidity"]
        )

    def test_legacy_empty_identity_does_not_duplicate_or_hide_valid_node_id(
        self,
    ) -> None:
        old = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        legacy = {
            "location": "office",
            "node_id": "",
            "topic": "home/air/office",
            "sensor_type": "air_quality",
        }
        identified = {**legacy, "node_id": "100"}
        records = [
            FakeRecord("air_quality_15m", "co2_mean", 700.0, old, identified),
            FakeRecord(
                "air_quality_15m",
                "co2_mean",
                721.0,
                old + timedelta(minutes=15),
                legacy,
            ),
            FakeRecord(
                "environment_reading",
                "temperature_c",
                20.0,
                old,
                {"node_id": "", "sensor_type": "environment"},
            ),
        ]

        response = latest_response(records)

        self.assertEqual(len(response["air_quality"]), 1)
        self.assertEqual(response["air_quality"][0]["id"], "office")
        self.assertEqual(response["air_quality"][0]["node_id"], 100)
        self.assertEqual(response["environment"], [])

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
                self.assertIn("battery_mv", node["available_fields"])

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

    def test_node_becomes_offline_after_four_stale_windows(self) -> None:
        latest = {
            "generated_at": "2026-01-01T13:00:01Z",
            "environment": [
                {
                    "id": "1",
                    "sensor_type": "environment",
                    "node_id": 1,
                    "last_seen": "2026-01-01T12:00:00Z",
                    "available_fields": ["temperature_c", "humidity"],
                }
            ],
            "air_quality": [],
        }

        node = nodes_response(latest, stale_after_seconds=600)["nodes"][0]

        self.assertEqual(node["status"], "offline")
        self.assertEqual(node["stale_reason"], "no_recent_reading")

    def test_a_printer_reporting_itself_off_is_not_called_silent(self) -> None:
        """Regression from a live incident: offline next to a 3-second reading.

        The observer polls Home Assistant continuously, so a printer that has
        been switched off keeps producing fresh observations that say
        ``online=false``. The status override is right -- the printer is
        offline -- but the reason said ``no_recent_reading``, which asserted
        the opposite of what happened and made a healthy observer look like a
        broken ingest path. The AMS entities carried by the same poll stayed
        ``online``, which is what made the contradiction visible.
        """

        latest = {
            "generated_at": "2026-01-01T12:00:03Z",
            "printer": [
                {
                    "id": "x2d",
                    "sensor_type": "printer",
                    "printer_id": "x2d",
                    "source_id": "x2d",
                    "component_id": "main",
                    "last_seen": "2026-01-01T12:00:00Z",
                    "online": False,
                }
            ],
            "ams": [
                {
                    "id": "x2d/ams_1",
                    "sensor_type": "ams",
                    "printer_id": "x2d",
                    "source_id": "x2d",
                    "component_id": "ams_1",
                    "last_seen": "2026-01-01T12:00:00Z",
                }
            ],
        }

        nodes = {
            node["id"]: node
            for node in nodes_response(latest, stale_after_seconds=1800)["nodes"]
        }

        printer = nodes["x2d"]
        self.assertEqual(printer["age_seconds"], 3)
        self.assertEqual(printer["status"], "offline")
        self.assertEqual(printer["stale_reason"], "reported_offline")
        # The sibling carried by the same poll is unaffected.
        self.assertEqual(nodes["x2d/ams_1"]["status"], "online")

    def test_a_printer_that_is_genuinely_silent_still_reads_as_silent(self) -> None:
        """The new reason must not mask a real ingest outage."""

        latest = {
            "generated_at": "2026-01-01T12:00:00Z",
            "printer": [
                {
                    "id": "x2d",
                    "sensor_type": "printer",
                    "printer_id": "x2d",
                    "source_id": "x2d",
                    "component_id": "main",
                    "last_seen": "2026-01-01T04:00:00Z",
                    "online": False,
                }
            ],
        }

        node = nodes_response(latest, stale_after_seconds=1800)["nodes"][0]

        self.assertEqual(node["status"], "offline")
        self.assertEqual(node["stale_reason"], "no_recent_reading")

    def test_a_fresh_printer_reporting_online_keeps_no_reason(self) -> None:
        latest = {
            "generated_at": "2026-01-01T12:00:03Z",
            "printer": [
                {
                    "id": "x2d",
                    "sensor_type": "printer",
                    "printer_id": "x2d",
                    "source_id": "x2d",
                    "component_id": "main",
                    "last_seen": "2026-01-01T12:00:00Z",
                    "online": True,
                }
            ],
        }

        node = nodes_response(latest, stale_after_seconds=1800)["nodes"][0]

        self.assertEqual(node["status"], "online")
        self.assertIsNone(node["stale_reason"])

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
                        STATUS_BATTERY_OK | STATUS_BATTERY_LOW | STATUS_BATTERY_SHUTDOWN
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
        self.assertEqual(response["environment"][0]["status"], "online")
        self.assertTrue(response["environment"][0]["values_are_current"])
        self.assertNotIn("nodes", latest)

    def test_offline_sensor_remains_listed_and_values_are_marked_last_known(
        self,
    ) -> None:
        latest = {
            "generated_at": "2026-01-01T13:00:00Z",
            "environment": [],
            "air_quality": [
                {
                    "id": "printer_room",
                    "sensor_type": "air_quality",
                    "location": "printer_room",
                    "topic": "home/air/printer_room",
                    "last_seen": "2026-01-01T12:00:00Z",
                    "co2": 721.0,
                    "available_fields": ["co2"],
                }
            ],
        }

        response = latest_with_node_status(
            latest,
            stale_after_seconds=1800,
            air_quality_stale_after_seconds=20,
        )

        station = response["air_quality"][0]
        self.assertEqual(station["status"], "offline")
        self.assertFalse(station["values_are_current"])
        self.assertEqual(station["co2"], 721.0)
        self.assertEqual(response["nodes"][0]["last_seen"], station["last_seen"])

    def test_required_sen66_uses_the_same_deterministic_freshness_rule(self) -> None:
        seen = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        latest = latest_response(_air_quality_records(seen))

        online = air_quality_sensor_status(
            latest,
            location="printer_room",
            stale_after_seconds=20,
            observed_at=seen + timedelta(seconds=20),
        )
        stale = air_quality_sensor_status(
            latest,
            location="printer_room",
            stale_after_seconds=20,
            observed_at=seen + timedelta(seconds=21),
        )
        unknown = air_quality_sensor_status(
            latest,
            location="never_seen",
            stale_after_seconds=20,
            observed_at=seen,
        )

        self.assertEqual(online["status"], "online")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(unknown["status"], "unknown")

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

    def test_overall_status_includes_direct_co2_exposure_warning_only_when_relevant(
        self,
    ) -> None:
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


class DurableInventoryCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.durable = {
            "generated_at": "2026-01-02T00:00:00Z",
            "environment": [
                {
                    "id": "1",
                    "node_id": 1,
                    "sensor_type": "environment",
                    "topic": "home/sensors/1",
                    "last_seen": "2025-12-20T00:00:00Z",
                    "temperature_c": 21.5,
                    "battery_mv": None,
                    "battery_measurement_ok": False,
                    "available_fields": ["temperature_c"],
                }
            ],
            "air_quality": [
                {
                    "id": "office",
                    "node_id": 100,
                    "location": "office",
                    "sensor_type": "air_quality",
                    "topic": "home/air/office",
                    "last_seen": "2025-12-20T00:00:00Z",
                    "co2": 721.0,
                    "available_fields": ["co2"],
                }
            ],
        }

    def test_offline_sen66_survives_live_expiry_and_restart_reconstruction(
        self,
    ) -> None:
        empty_live = {
            "generated_at": "2026-01-02T00:00:00Z",
            "environment": [],
            "air_quality": [],
        }

        before_restart = DurableInventoryCache(self.durable).observe(empty_live)
        after_restart = DurableInventoryCache(self.durable).observe(empty_live)

        for snapshot in (before_restart, after_restart):
            station = snapshot["air_quality"][0]
            self.assertEqual(station["id"], "office")
            self.assertEqual(station["node_id"], 100)
            self.assertEqual(station["last_seen"], "2025-12-20T00:00:00Z")
            self.assertEqual(station["co2"], 721.0)
            enriched = latest_with_node_status(
                snapshot,
                stale_after_seconds=1800,
                air_quality_stale_after_seconds=20,
            )["air_quality"][0]
            self.assertEqual(enriched["status"], "offline")
            self.assertFalse(enriched["values_are_current"])

    def test_cached_sensor_freshness_is_recomputed_from_request_time(self) -> None:
        seen = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        live = {
            "generated_at": seen.isoformat(),
            "environment": [],
            "air_quality": [
                {
                    "id": "garage",
                    "location": "garage",
                    "sensor_type": "air_quality",
                    "last_seen": seen.isoformat(),
                    "co2": 612,
                    "available_fields": ["co2"],
                }
            ],
        }
        cache = DurableInventoryCache(
            {"generated_at": seen.isoformat(), "environment": [], "air_quality": []}
        )
        cache.observe(live)

        cases = (
            (20, "online", True, None),
            (21, "stale", False, "no_recent_reading"),
            (81, "offline", False, "no_recent_reading"),
        )
        for age_seconds, expected_status, values_are_current, stale_reason in cases:
            with self.subTest(age_seconds=age_seconds):
                observed_at = seen + timedelta(seconds=age_seconds)
                snapshot = cache.observe(
                    {
                        "generated_at": observed_at.isoformat(),
                        "environment": [],
                        "air_quality": [],
                    }
                )
                station = latest_with_node_status(
                    snapshot,
                    stale_after_seconds=1800,
                    air_quality_stale_after_seconds=20,
                )["air_quality"][0]
                self.assertEqual(station["age_seconds"], age_seconds)
                self.assertEqual(station["status"], expected_status)
                self.assertIs(station["values_are_current"], values_are_current)
                self.assertEqual(station["stale_reason"], stale_reason)

        cached_station = cache.snapshot()["air_quality"][0]
        fresh_interpretation = interpret_station(
            cached_station,
            stale_after_seconds=20,
            now=seen + timedelta(seconds=20),
        )["co2"]
        stale_interpretation = interpret_station(
            cached_station,
            stale_after_seconds=20,
            now=seen + timedelta(seconds=21),
        )["co2"]
        self.assertFalse(fresh_interpretation["is_stale"])
        self.assertTrue(stale_interpretation["is_stale"])
        self.assertEqual(stale_interpretation["severity"], "unavailable")

    def test_observation_during_refresh_is_not_discarded(self) -> None:
        now = [0.0]
        cache = DurableInventoryCache(
            self.durable,
            refresh_seconds=1,
            clock=lambda: now[0],
        )
        now[0] = 2.0
        loader_started = Event()
        release_loader = Event()

        def load_old_snapshot() -> dict[str, object]:
            loader_started.set()
            release_loader.wait(timeout=1)
            return self.durable

        self.assertTrue(cache.refresh_if_due(load_old_snapshot))
        self.assertTrue(loader_started.wait(timeout=1))
        cache.observe(
            {
                "generated_at": "2026-01-02T00:00:05Z",
                "environment": [],
                "air_quality": [
                    {
                        "id": "garage",
                        "location": "garage",
                        "sensor_type": "air_quality",
                        "last_seen": "2026-01-02T00:00:05Z",
                        "co2": 612,
                        "available_fields": ["co2"],
                    }
                ],
            }
        )
        release_loader.set()
        assert cache._refresh_thread is not None
        cache._refresh_thread.join(timeout=1)

        snapshot = cache.observe(
            {
                "generated_at": "2026-01-02T00:00:06Z",
                "environment": [],
                "air_quality": [],
            }
        )
        self.assertIn("garage", {item["id"] for item in snapshot["air_quality"]})

    def test_newer_existing_sensor_observation_survives_refresh(self) -> None:
        now = [0.0]
        cache = DurableInventoryCache(
            self.durable,
            refresh_seconds=1,
            clock=lambda: now[0],
        )
        now[0] = 2.0
        loader_started = Event()
        release_loader = Event()

        def load_old_snapshot() -> dict[str, object]:
            loader_started.set()
            release_loader.wait(timeout=1)
            return self.durable

        self.assertTrue(cache.refresh_if_due(load_old_snapshot))
        self.assertTrue(loader_started.wait(timeout=1))
        cache.observe(
            {
                "generated_at": "2026-01-02T00:00:05Z",
                "environment": [],
                "air_quality": [
                    {
                        "id": "office",
                        "node_id": 100,
                        "location": "office",
                        "sensor_type": "air_quality",
                        "topic": "home/air/office",
                        "last_seen": "2026-01-02T00:00:05Z",
                        "co2": 900,
                        "available_fields": ["co2"],
                    }
                ],
            }
        )
        release_loader.set()
        assert cache._refresh_thread is not None
        cache._refresh_thread.join(timeout=1)

        station = cache.observe(
            {
                "generated_at": "2026-01-02T00:00:06Z",
                "environment": [],
                "air_quality": [],
            }
        )["air_quality"][0]
        self.assertEqual(station["last_seen"], "2026-01-02T00:00:05Z")
        self.assertEqual(station["co2"], 900)

    def test_sparse_environment_merge_matches_old_record_union(self) -> None:
        old = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        new = old + timedelta(minutes=1)
        values = {
            "node_id": "7",
            "topic": "home/sensors/7",
            "sensor_type": "environment",
        }
        durable_records = [
            FakeRecord("environment_reading", "temperature_c", 21.0, old, values),
            FakeRecord("environment_reading", "humidity", 40.0, old, values),
            FakeRecord("environment_reading", "battery_mv", 4000, old, values),
            FakeRecord(
                "environment_reading", "status_flags", STATUS_BATTERY_OK, old, values
            ),
        ]
        observed_records = [
            FakeRecord("environment_reading", "temperature_c", 22.0, new, values)
        ]

        expected = latest_response(durable_records + observed_records)["environment"][0]
        actual = DurableInventoryCache(
            latest_response(durable_records, include_internal=True)
        ).observe(latest_response(observed_records, include_internal=True))[
            "environment"
        ][0]

        self.assertEqual(actual, expected)
        self.assertEqual(actual["humidity"], 40.0)
        self.assertIsNone(actual["battery_mv"])
        self.assertIsNone(actual["battery_measurement_ok"])

    def test_sparse_air_quality_merge_matches_old_record_union(self) -> None:
        old = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        new = old + timedelta(seconds=5)
        values = {
            "node_id": "100",
            "location": "office",
            "topic": "home/air/office",
            "sensor_type": "air_quality",
        }
        durable_records = [
            FakeRecord("air_quality_reading", "co2", 700, old, values),
            FakeRecord("air_quality_reading", "pm25", 5.0, old, values),
        ]
        observed_records = [FakeRecord("air_quality_reading", "co2", 750, new, values)]

        expected = latest_response(durable_records + observed_records)["air_quality"][0]
        actual = DurableInventoryCache(
            latest_response(durable_records, include_internal=True)
        ).observe(latest_response(observed_records, include_internal=True))[
            "air_quality"
        ][0]

        self.assertEqual(actual, expected)
        self.assertIsNone(actual["pm25"])
        self.assertEqual(actual["available_fields"], ["co2", "pm25"])

    def test_split_battery_timestamps_match_old_record_union(self) -> None:
        old = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        new = old + timedelta(seconds=5)
        values = {
            "node_id": "7",
            "topic": "home/sensors/7",
            "sensor_type": "environment",
        }
        cases = (
            (
                [FakeRecord("environment_reading", "battery_mv", 4000, old, values)],
                [
                    FakeRecord(
                        "environment_reading",
                        "status_flags",
                        STATUS_BATTERY_OK,
                        new,
                        values,
                    )
                ],
            ),
            (
                [
                    FakeRecord(
                        "environment_reading",
                        "status_flags",
                        STATUS_BATTERY_OK,
                        old,
                        values,
                    )
                ],
                [FakeRecord("environment_reading", "battery_mv", 4000, new, values)],
            ),
        )

        for durable_records, observed_records in cases:
            with self.subTest(observed_field=observed_records[0].field):
                expected = latest_response(durable_records + observed_records)[
                    "environment"
                ][0]
                actual = DurableInventoryCache(
                    latest_response(durable_records, include_internal=True)
                ).observe(latest_response(observed_records, include_internal=True))[
                    "environment"
                ][0]

                self.assertEqual(actual, expected)
                self.assertIsNone(actual["battery_mv"])

    def test_new_sensor_is_discovered_from_bounded_observation(self) -> None:
        cache = DurableInventoryCache(self.durable)
        latest = {
            "generated_at": "2026-01-02T00:00:00Z",
            "environment": [],
            "air_quality": [
                {
                    "id": "garage",
                    "node_id": 101,
                    "location": "garage",
                    "sensor_type": "air_quality",
                    "topic": "home/air/garage",
                    "last_seen": "2026-01-01T23:59:59Z",
                    "co2": 612,
                    "available_fields": ["co2"],
                }
            ],
        }

        observed = cache.observe(latest)
        after_live_expiry = cache.observe(
            {
                "generated_at": "2026-01-03T00:00:00Z",
                "environment": [],
                "air_quality": [],
            }
        )

        self.assertEqual(
            {station["id"] for station in observed["air_quality"]},
            {"office", "garage"},
        )
        self.assertEqual(
            {station["id"] for station in after_live_expiry["air_quality"]},
            {"office", "garage"},
        )

    def test_multiple_sensor_types_and_null_battery_semantics_are_preserved(
        self,
    ) -> None:
        snapshot = DurableInventoryCache(self.durable).snapshot()

        self.assertEqual(len(snapshot["environment"]), 1)
        self.assertEqual(len(snapshot["air_quality"]), 1)
        node = snapshot["environment"][0]
        self.assertIsNone(node["battery_mv"])
        self.assertFalse(node["battery_measurement_ok"])

    def test_refresh_failure_retains_known_good_inventory(self) -> None:
        now = [0.0]
        cache = DurableInventoryCache(
            self.durable,
            refresh_seconds=1,
            clock=lambda: now[0],
        )
        now[0] = 2.0

        def fail() -> dict[str, object]:
            raise RuntimeError("Influx temporarily unavailable")

        with self.assertLogs("home_sensor.queries", level="ERROR"):
            self.assertTrue(cache.refresh_if_due(fail))
            assert cache._refresh_thread is not None
            cache._refresh_thread.join(timeout=1)

        snapshot = cache.snapshot()
        self.assertEqual(snapshot["air_quality"][0]["id"], "office")
        self.assertEqual(snapshot["environment"][0]["id"], "1")

        now[0] = 4.0
        recovered = {
            "generated_at": "2026-01-02T00:00:00Z",
            "environment": self.durable["environment"],
            "air_quality": [],
        }
        self.assertTrue(cache.refresh_if_due(lambda: recovered))
        assert cache._refresh_thread is not None
        cache._refresh_thread.join(timeout=1)
        self.assertEqual(cache.snapshot()["air_quality"], [])

    def test_successful_refresh_reconciles_changed_permanent_inventory(self) -> None:
        now = [0.0]
        cache = DurableInventoryCache(
            self.durable,
            refresh_seconds=1,
            clock=lambda: now[0],
        )
        now[0] = 2.0
        cache.observe(
            {
                "generated_at": "2026-01-02T00:00:00Z",
                "environment": [],
                "air_quality": [
                    {
                        "id": "garage",
                        "location": "garage",
                        "sensor_type": "air_quality",
                        "last_seen": "2026-01-01T23:59:59Z",
                    }
                ],
            }
        )
        replacement = {
            "generated_at": "2026-01-02T00:00:00Z",
            "environment": self.durable["environment"],
            "air_quality": [],
        }

        self.assertTrue(cache.refresh_if_due(lambda: replacement))
        assert cache._refresh_thread is not None
        cache._refresh_thread.join(timeout=1)

        self.assertEqual(cache.snapshot()["air_quality"], [])

    def test_concurrent_calls_start_only_one_background_refresh(self) -> None:
        now = [0.0]
        cache = DurableInventoryCache(
            self.durable,
            refresh_seconds=1,
            clock=lambda: now[0],
        )
        now[0] = 2.0
        release = Event()
        calls = 0
        calls_lock = Lock()

        def load() -> dict[str, object]:
            nonlocal calls
            with calls_lock:
                calls += 1
            release.wait(timeout=1)
            return self.durable

        with ThreadPoolExecutor(max_workers=12) as executor:
            started = list(
                executor.map(lambda _: cache.refresh_if_due(load), range(24))
            )

        self.assertEqual(sum(started), 1)
        self.assertEqual(calls, 1)
        release.set()
        assert cache._refresh_thread is not None
        cache._refresh_thread.join(timeout=1)

    def test_closed_cache_cannot_start_another_refresh(self) -> None:
        now = [0.0]
        cache = DurableInventoryCache(
            self.durable,
            refresh_seconds=1,
            clock=lambda: now[0],
        )
        cache.close()
        now[0] = 2.0

        self.assertFalse(cache.refresh_if_due(lambda: self.durable))

    def test_repository_latest_executes_only_bounded_flux(self) -> None:
        repository = object.__new__(InfluxReadRepository)
        repository._settings = InfluxSettings(
            url="http://127.0.0.1:8086",
            org="test",
            bucket="environment",
            write_token="unused",
            read_token="unused",
            live_bucket="environment_live",
        )
        repository._inventory = DurableInventoryCache(self.durable)
        repository._query = Mock(return_value=[])

        snapshot = repository.latest()

        flux = repository._query.call_args.args[0]
        self.assertNotIn("range(start: 0)", flux)
        self.assertEqual(repository._query.call_count, 1)
        self.assertEqual(snapshot["air_quality"][0]["id"], "office")

    def test_repository_startup_fails_closed_when_reconstruction_fails(self) -> None:
        client = Mock()
        client.query_api.return_value.query.side_effect = RuntimeError(
            "Influx temporarily unavailable"
        )
        settings = InfluxSettings(
            url="http://127.0.0.1:8086",
            org="test",
            bucket="environment",
            write_token="unused",
            read_token="unused",
            live_bucket="environment_live",
        )

        with (
            patch("influxdb_client.InfluxDBClient", return_value=client),
            self.assertLogs("home_sensor.queries", level="ERROR"),
            self.assertRaisesRegex(RuntimeError, "temporarily unavailable"),
        ):
            InfluxReadRepository(settings)

        client.close.assert_called_once_with()


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


MONITORING_GRAPH_LATEST = {
    "environment": [
        {
            "sensor_type": "environment",
            "node_id": 1,
            "id": "1",
            "available_fields": ["temperature_c", "humidity", "battery_mv"],
        }
    ],
    "air_quality": [
        {
            "sensor_type": "air_quality",
            "location": "office",
            "id": "office",
            "available_fields": ["temperature_c", "co2", "pm25"],
        }
    ],
    "printer": [
        {
            "sensor_type": "printer",
            "printer_id": "x2d",
            "id": "x2d",
            "available_fields": [
                "chamber_temperature_c",
                "bed_temperature_c",
                "online",
                "printer_is_printing",
            ],
        }
    ],
    "ams": [
        {
            "sensor_type": "ams",
            "printer_id": "x2d",
            "ams_id": "ams_1",
            "source_id": "x2d/ams_1",
            "id": "x2d/ams_1",
            "available_fields": [
                "ams_humidity",
                "ams_temperature_c",
                "ams_active",
            ],
        },
        {
            "sensor_type": "ams",
            "printer_id": "x2d",
            "ams_id": "external_spool_1",
            "source_id": "x2d/external_spool_1",
            "id": "x2d/external_spool_1",
            "available_fields": ["ams_active"],
        },
    ],
}


class MonitoringGraphCapabilityContractTest(unittest.TestCase):
    """The shared Monitoring graph is driven entirely by backend capability data.

    The shipped defect was a leak between two different sources of truth: the
    measurement picker unioned the *global* field catalog while the source
    picker only knew about environment/air-quality entities, so Bambu
    measurements were offered and then reported unavailable. These tests pin
    the per-source contract the graph must derive its choices from.
    """

    @staticmethod
    def _graphable(entity):
        """Mirror of the frontend rule: numeric-aggregatable fields only."""

        return tuple(
            field for field in entity["available_fields"] if field in NUMERIC_FIELDS
        )

    def test_case1_graphable_sources_span_all_four_families(self) -> None:
        graphable = {
            str(entity.get("id")): self._graphable(entity)
            for family in ("environment", "air_quality", "printer", "ams")
            for entity in MONITORING_GRAPH_LATEST[family]
            if self._graphable(entity)
        }
        # environment, SEN66, printer and the real AMS all qualify...
        self.assertEqual(sorted(graphable), ["1", "office", "x2d", "x2d/ams_1"])
        # ...and identities never collide across families.
        self.assertEqual(len(graphable), 4)

    def test_case2_ams_humidity_is_selectable_for_the_ams_source(self) -> None:
        ams = MONITORING_GRAPH_LATEST["ams"][0]
        self.assertIn("ams_humidity", self._graphable(ams))
        self.assertIn("ams_temperature_c", self._graphable(ams))
        # Selectable means the catalog really attributes it to this family.
        self.assertIn("ams", FIELDS_BY_SENSOR_TYPE_LOOKUP["ams_humidity"])

    def test_case3_chamber_temperature_is_selectable_for_the_printer(self) -> None:
        printer = MONITORING_GRAPH_LATEST["printer"][0]
        self.assertIn("chamber_temperature_c", self._graphable(printer))
        self.assertIn("printer", FIELDS_BY_SENSOR_TYPE_LOOKUP["chamber_temperature_c"])

    def test_case4_cross_family_selection_keeps_identities_distinct(self) -> None:
        selected = [
            MONITORING_GRAPH_LATEST["environment"][0],
            MONITORING_GRAPH_LATEST["printer"][0],
            MONITORING_GRAPH_LATEST["ams"][0],
        ]
        ids = [str(entity["id"]) for entity in selected]
        self.assertEqual(len(set(ids)), 3, f"source-id collision in {ids}")
        # Every selected source contributes at least one graphable measurement.
        for entity in selected:
            self.assertTrue(self._graphable(entity), entity["id"])
        # A shared unit (degC) is legitimately provided by more than one family.
        degc = {
            family
            for family, fields in FIELDS_BY_SENSOR_TYPE.items()
            if "temperature_c" in fields
        }
        self.assertIn("environment", degc)

    def test_case5_fields_are_not_attributed_across_families(self) -> None:
        # This is the exact leak that produced a false "unavailable".
        self.assertNotIn("ams_humidity", FIELDS_BY_SENSOR_TYPE["printer"])
        self.assertNotIn("chamber_temperature_c", FIELDS_BY_SENSOR_TYPE["ams"])
        self.assertNotIn("ams_humidity", FIELDS_BY_SENSOR_TYPE["environment"])
        self.assertNotIn("co2", FIELDS_BY_SENSOR_TYPE["ams"])

    def test_case6_external_spool_offers_no_graphable_measurement(self) -> None:
        spool = MONITORING_GRAPH_LATEST["ams"][1]
        self.assertEqual(self._graphable(spool), ())
        # It stays a real, discoverable source - it is only hidden from the graph.
        self.assertEqual(spool["available_fields"], ["ams_active"])
        self.assertIn("ams_active", FIELDS_BY_SENSOR_TYPE["ams"])
        self.assertNotIn("ams_active", NUMERIC_FIELDS)

    def test_case7_readings_contract_remains_environment_and_air_quality(self) -> None:
        # /api/readings must keep its legacy semantics: no printer/ams source
        # types accepted, so existing consumers see an unchanged response.
        self.assertEqual(SENSOR_TYPES, {"all", "environment", "air_quality"})
        with self.assertRaises(QueryValidationError):
            readings_query_from_params({"sensor_type": "printer"})
        with self.assertRaises(QueryValidationError):
            readings_query_from_params({"sensor_type": "ams"})
