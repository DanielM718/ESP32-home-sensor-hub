from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from home_sensor_discovery.discovery import (  # noqa: E402
    PayloadError,
    discovery_messages,
    is_stale,
    parse_message,
    stable_unique_ids,
)


SHT41_PAYLOAD = b"""{
  "packet_type": "sht41",
  "node_id": 1,
  "sequence": 1523,
  "temperature_c": 24.8,
  "humidity": 41.6,
  "battery_mv": 4058,
  "status_flags": 4
}"""

SEN66_PAYLOAD = b"""{
  "packet_type": "sen66",
  "schema_version": 1,
  "firmware_version": "2.0.0",
  "node_id": 100,
  "sequence": 42,
  "status_flags": 511,
  "co2": 721,
  "pm1": 1.1,
  "pm25": 2.8,
  "pm4": 3.5,
  "pm10": 5.2,
  "voc_index": 88,
  "nox_index": 12,
  "temperature_c": 24.5,
  "humidity": 42.3
}"""


class DiscoveryTests(unittest.TestCase):
    def test_sht41_discovery_is_stable_and_complete(self) -> None:
        record = parse_message(
            "home/sensors/1", SHT41_PAYLOAD, received_at="2026-07-20T12:00:00+00:00"
        )
        first = discovery_messages(record, stale_after_seconds=1800)
        second = discovery_messages(record, stale_after_seconds=1800)

        self.assertEqual(first, second)
        self.assertEqual(record.key, "sht41_node_1")
        self.assertEqual(len(first), 9)
        self.assertEqual(len(stable_unique_ids(first.values())), len(first))
        payloads = [json.loads(payload) for payload in first.values()]
        voltage = next(item for item in payloads if item["name"] == "Battery voltage")
        self.assertEqual(voltage["device_class"], "voltage")
        self.assertIn("bitwise_and(4)", voltage["value_template"])

    def test_sen66_exposes_every_measurement_and_diagnostics(self) -> None:
        record = parse_message(
            "home/air/office", SEN66_PAYLOAD, received_at="2026-07-20T12:00:00+00:00"
        )
        messages = discovery_messages(record, stale_after_seconds=30)
        names = {json.loads(payload)["name"] for payload in messages.values()}

        self.assertTrue(
            {
                "Temperature",
                "Relative humidity",
                "Carbon dioxide",
                "PM1.0",
                "PM2.5",
                "PM4.0",
                "PM10",
                "VOC Index",
                "NOx Index",
                "Status flags",
                "Sequence",
                "Schema version",
                "Firmware version",
                "Device status warning",
                "Last packet",
                "Online",
            }.issubset(names)
        )
        self.assertEqual(len(stable_unique_ids(messages.values())), len(messages))
        pm4 = next(json.loads(payload) for payload in messages.values() if json.loads(payload)["name"] == "PM4.0")
        self.assertEqual(pm4["device_class"], "pm4")

    def test_malformed_payload_does_not_refresh_health(self) -> None:
        malformed = SEN66_PAYLOAD.replace(b'"pm25": 2.8', b'"pm25": "bad"')
        with self.assertRaisesRegex(PayloadError, "pm25 must be a number"):
            parse_message("home/air/office", malformed)

    def test_topic_and_payload_identity_must_match(self) -> None:
        bad = SHT41_PAYLOAD.replace(b'"node_id": 1', b'"node_id": 2')
        with self.assertRaisesRegex(PayloadError, "does not match"):
            parse_message("home/sensors/1", bad)

    def test_stale_boundary_is_fail_closed(self) -> None:
        record = parse_message(
            "home/sensors/1", SHT41_PAYLOAD, received_at="2026-07-20T12:00:00+00:00"
        )
        self.assertFalse(
            is_stale(
                record,
                now=datetime(2026, 7, 20, 12, 29, 59, tzinfo=timezone.utc),
                stale_after_seconds=1800,
            )
        )
        self.assertTrue(
            is_stale(
                record,
                now=datetime(2026, 7, 20, 12, 30, 0, tzinfo=timezone.utc),
                stale_after_seconds=1800,
            )
        )


if __name__ == "__main__":
    unittest.main()
