from __future__ import annotations

import json
from pathlib import Path
import unittest

from app.models import AirQualityReading, SensorReading
from app.battery_status import (
    STATUS_BATTERY_LOW,
    STATUS_BATTERY_OK,
    STATUS_BATTERY_SHUTDOWN,
)
from app.validation import ValidationError
from bridge.topic_router import reading_from_mqtt_message


SEN66_FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "sen66-full.json"


def payload(data: dict[str, object]) -> bytes:
    return json.dumps(data).encode("utf-8")


class TopicRouterTest(unittest.TestCase):
    def test_sensor_message_is_validated(self) -> None:
        reading = reading_from_mqtt_message(
            "home/sensors/1",
            payload(
                {
                    "node_id": 1,
                    "sequence": 1523,
                    "temperature_c": 24.8,
                    "humidity": 41.6,
                    "battery_mv": 4058,
                    "status_flags": STATUS_BATTERY_OK,
                }
            ),
            max_payload_bytes=4096,
        )

        self.assertIsInstance(reading, SensorReading)
        self.assertEqual(reading.measurement, "environment_reading")
        self.assertEqual(reading.tags["node_id"], "1")
        self.assertEqual(reading.fields["battery_mv"], 4058)
        self.assertEqual(reading.fields["status_flags"], STATUS_BATTERY_OK)

    def test_sensor_status_flags_are_preserved_without_masking(self) -> None:
        cases = (
            0,
            STATUS_BATTERY_OK,
            STATUS_BATTERY_OK | STATUS_BATTERY_LOW,
            STATUS_BATTERY_OK | STATUS_BATTERY_LOW | STATUS_BATTERY_SHUTDOWN,
            STATUS_BATTERY_OK | (1 << 31),
        )

        for status_flags in cases:
            with self.subTest(status_flags=status_flags):
                reading = reading_from_mqtt_message(
                    "home/sensors/1",
                    payload(
                        {
                            "node_id": 1,
                            "sequence": 1523,
                            "temperature_c": 24.8,
                            "humidity": 41.6,
                            "battery_mv": 4058,
                            "status_flags": status_flags,
                        }
                    ),
                    max_payload_bytes=4096,
                )

                self.assertEqual(reading.status_flags, status_flags)
                self.assertEqual(reading.fields["status_flags"], status_flags)

    def test_sensor_message_without_status_flags_is_accepted_as_unavailable(self) -> None:
        reading = reading_from_mqtt_message(
            "home/sensors/1",
            payload(
                {
                    "node_id": 1,
                    "sequence": 1523,
                    "temperature_c": 24.8,
                    "humidity": 41.6,
                    "battery_mv": 4058,
                }
            ),
            max_payload_bytes=4096,
        )

        self.assertIsNone(reading.status_flags)
        self.assertNotIn("status_flags", reading.fields)
        self.assertNotIn("battery_mv", reading.fields)

    def test_zero_battery_without_ok_flag_is_not_stored_as_a_measurement(self) -> None:
        reading = reading_from_mqtt_message(
            "home/sensors/1",
            payload(
                {
                    "node_id": 1,
                    "sequence": 1523,
                    "temperature_c": 24.8,
                    "humidity": 41.6,
                    "battery_mv": 0,
                    "status_flags": 0,
                }
            ),
            max_payload_bytes=4096,
        )

        self.assertEqual(reading.fields["status_flags"], 0)
        self.assertNotIn("battery_mv", reading.fields)

    def test_sensor_topic_and_payload_node_must_match(self) -> None:
        with self.assertRaises(ValidationError):
            reading_from_mqtt_message(
                "home/sensors/2",
                payload(
                    {
                        "node_id": 1,
                        "sequence": 1523,
                        "temperature_c": 24.8,
                        "humidity": 41.6,
                        "battery_mv": 4058,
                        "status_flags": STATUS_BATTERY_OK,
                    }
                ),
                max_payload_bytes=4096,
            )

    def test_sensor_humidity_range_is_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            reading_from_mqtt_message(
                "home/sensors/1",
                payload(
                    {
                        "node_id": 1,
                        "sequence": 1523,
                        "temperature_c": 24.8,
                        "humidity": 141.6,
                        "battery_mv": 4058,
                        "status_flags": STATUS_BATTERY_OK,
                    }
                ),
                max_payload_bytes=4096,
            )

    def test_air_quality_message_is_validated(self) -> None:
        firmware_payload = json.loads(SEN66_FIXTURE.read_text(encoding="utf-8"))
        reading = reading_from_mqtt_message(
            "home/air/sen66_test",
            payload(firmware_payload),
            max_payload_bytes=4096,
        )

        self.assertIsInstance(reading, AirQualityReading)
        self.assertEqual(reading.measurement, "air_quality_reading")
        self.assertEqual(reading.tags["location"], "sen66_test")
        self.assertEqual(
            reading.fields,
            {
                "co2": 612,
                "pm1": 1.2,
                "pm25": 2.4,
                "pm4": 3.1,
                "pm10": 4.8,
                "voc_index": 87,
                "nox_index": 2,
                "temperature_c": 24.5,
                "humidity": 42.1,
            },
        )

    def test_air_quality_message_requires_complete_sen66_measurements(self) -> None:
        incomplete_payload = {
            "co2": 721,
            "pm1": 1.1,
            "pm25": 2.8,
            "pm4": 3.5,
            "pm10": 5.2,
            "voc_index": 88,
            "temperature_c": 24.5,
            "humidity": 42.3,
        }

        with self.assertRaises(ValidationError):
            reading_from_mqtt_message(
                "home/air/printer_room",
                payload(incomplete_payload),
                max_payload_bytes=4096,
            )

    def test_air_quality_location_must_be_slug(self) -> None:
        with self.assertRaises(ValidationError):
            reading_from_mqtt_message(
                "home/air/printer room",
                payload(
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
                    }
                ),
                max_payload_bytes=4096,
            )

    def test_oversized_payload_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            reading_from_mqtt_message(
                "home/sensors/1",
                b'{"node_id": 1}',
                max_payload_bytes=4,
            )

    def test_unsupported_topic_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            reading_from_mqtt_message(
                "home/other/1",
                payload({"node_id": 1}),
                max_payload_bytes=4096,
            )


if __name__ == "__main__":
    unittest.main()
