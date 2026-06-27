from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import unittest

from app.queries import (
    QueryValidationError,
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
        self.assertEqual(query.window_every, "10m")
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
                }
            ],
            "air_quality": [],
        }

        response = nodes_response(latest, stale_after_seconds=600)

        self.assertEqual(response["nodes"][0]["status"], "stale")
        self.assertEqual(response["nodes"][0]["age_seconds"], 1800)


if __name__ == "__main__":
    unittest.main()
