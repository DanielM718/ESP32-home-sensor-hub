"""Route MQTT topics and payloads into typed reading models."""

from __future__ import annotations

import re
import math
from typing import Any

from app.models import AirQualityReading, Reading, SensorReading, utc_now
from app.validation import (
    ValidationError,
    optional_int,
    parse_json_object,
    required_float,
    required_int,
)


LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def reading_from_mqtt_message(
    topic: str,
    payload: bytes,
    *,
    max_payload_bytes: int,
) -> Reading:
    """Parse a MQTT topic/payload pair into a validated reading."""

    data = parse_json_object(payload, max_payload_bytes=max_payload_bytes)
    parts = topic.split("/")

    if len(parts) == 3 and parts[:2] == ["home", "sensors"]:
        return _sensor_reading(topic, parts[2], data)

    if len(parts) == 3 and parts[:2] == ["home", "air"]:
        return _air_quality_reading(topic, parts[2], data)

    raise ValidationError(f"unsupported topic: {topic}")


def _sensor_reading(
    topic: str,
    node_id_from_topic: str,
    data: dict[str, Any],
) -> SensorReading:
    try:
        topic_node_id = int(node_id_from_topic)
    except ValueError as exc:
        raise ValidationError("sensor topic node_id must be an integer") from exc

    node_id = required_int(data, "node_id", min_value=1, max_value=4_294_967_295)
    if node_id != topic_node_id:
        raise ValidationError(
            f"topic node_id {topic_node_id} does not match payload node_id {node_id}"
        )

    return SensorReading(
        topic=topic,
        node_id=node_id,
        sequence=required_int(data, "sequence", min_value=0, max_value=4_294_967_295),
        temperature_c=required_float(
            data, "temperature_c", min_value=-80.0, max_value=125.0
        ),
        humidity=required_float(data, "humidity", min_value=0.0, max_value=100.0),
        battery_mv=required_int(data, "battery_mv", min_value=0, max_value=20_000),
        status_flags=optional_int(
            data, "status_flags", min_value=0, max_value=4_294_967_295
        ),
        received_at=utc_now(),
    )


def _air_quality_reading(
    topic: str,
    location: str,
    data: dict[str, Any],
) -> AirQualityReading:
    if not LOCATION_RE.fullmatch(location):
        raise ValidationError("air-quality topic location must be a stable slug")

    return AirQualityReading(
        topic=topic,
        location=location,
        co2=_sensor_int(data, "co2", 0, 40_000),
        pm1=_sensor_float(data, "pm1", 0.0, 1_000.0),
        pm25=_sensor_float(data, "pm25", 0.0, 1_000.0),
        pm4=_sensor_float(data, "pm4", 0.0, 1_000.0),
        pm10=_sensor_float(data, "pm10", 0.0, 1_000.0),
        voc_index=_sensor_int(data, "voc_index", 1, 500),
        nox_index=_sensor_int(data, "nox_index", 1, 500),
        temperature_c=_sensor_float(data, "temperature_c", -10.0, 50.0),
        humidity=_sensor_float(data, "humidity", 0.0, 90.0),
        received_at=utc_now(),
        node_id=_metadata_int(data, "node_id", 1, 4_294_967_295),
        sequence=_metadata_int(data, "sequence", 0, 4_294_967_295),
        status_flags=_metadata_int(data, "status_flags", 0, 4_294_967_295),
        firmware_version=_metadata_string(data, "firmware_version", 64),
        schema_version=_metadata_int(data, "schema_version", 1, 65_535),
        boot_id=_metadata_int(data, "boot_id", 0, 4_294_967_295),
        sensor_uptime_s=_metadata_int(data, "sensor_uptime_s", 0, 4_294_967_295),
        reset_reason=_metadata_int(data, "reset_reason", 0, 255),
        sraw_voc=_metadata_int(data, "sraw_voc", 0, 65_534),
        sraw_nox=_metadata_int(data, "sraw_nox", 0, 65_534),
    )


def _sensor_float(
    data: dict[str, Any], key: str, minimum: float, maximum: float
) -> float | None:
    """Return a valid sensor value, preserving bad/missing samples as unavailable."""

    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        return None
    return result


def _sensor_int(
    data: dict[str, Any], key: str, minimum: int, maximum: int
) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _metadata_int(
    data: dict[str, Any], key: str, minimum: int, maximum: int
) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _metadata_string(data: dict[str, Any], key: str, maximum_length: int) -> str | None:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum_length:
        return None
    return value
