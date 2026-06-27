"""Route MQTT topics and payloads into typed reading models."""

from __future__ import annotations

import re
from typing import Any

from app.models import AirQualityReading, Reading, SensorReading, utc_now
from app.validation import (
    ValidationError,
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
        status_flags=required_int(
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
        co2=required_int(data, "co2", min_value=0, max_value=100_000),
        pm1=required_float(data, "pm1", min_value=0.0, max_value=100_000.0),
        pm25=required_float(data, "pm25", min_value=0.0, max_value=100_000.0),
        pm4=required_float(data, "pm4", min_value=0.0, max_value=100_000.0),
        pm10=required_float(data, "pm10", min_value=0.0, max_value=100_000.0),
        voc_index=required_int(data, "voc_index", min_value=0, max_value=500),
        nox_index=required_int(data, "nox_index", min_value=0, max_value=500),
        temperature_c=required_float(
            data, "temperature_c", min_value=-80.0, max_value=125.0
        ),
        humidity=required_float(data, "humidity", min_value=0.0, max_value=100.0),
        received_at=utc_now(),
    )
