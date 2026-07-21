"""Pure validation and Home Assistant discovery-config generation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import __version__


SHT41_TOPIC_RE = re.compile(r"^home/sensors/([0-9]+)$")
SEN66_TOPIC_RE = re.compile(r"^home/air/([A-Za-z0-9_-]{1,64})$")
UINT32_MAX = 4_294_967_295
SERVICE_AVAILABILITY_SUFFIX = "home_sensor/discovery/availability"


class PayloadError(ValueError):
    """Raised when a source MQTT message is not safe to expose."""


@dataclass
class DeviceRecord:
    """Minimal persistent identity and health state for one MQTT device."""

    kind: str
    source_id: str
    topic: str
    last_seen: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        if self.kind == "sht41":
            return f"sht41_node_{self.source_id}"
        return f"sen66_{slug(self.source_id)}"

    @property
    def device_name(self) -> str:
        if self.kind == "sht41":
            return f"SHT41 node {self.source_id}"
        return f"SEN66 {self.source_id.replace('_', ' ').replace('-', ' ').title()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "topic": self.topic,
            "last_seen": self.last_seen,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeviceRecord":
        record = cls(
            kind=str(value["kind"]),
            source_id=str(value["source_id"]),
            topic=str(value["topic"]),
            last_seen=str(value["last_seen"]),
            metadata=dict(value.get("metadata", {})),
        )
        if record.kind not in {"sht41", "sen66"}:
            raise PayloadError(f"unsupported registry kind: {record.kind}")
        return record


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not result:
        raise PayloadError("device identifier cannot be converted to an entity slug")
    return result


def parse_message(topic: str, payload: bytes, *, received_at: str | None = None) -> DeviceRecord:
    """Validate a current source message and return its persistent identity."""

    try:
        decoded = payload.decode("utf-8")
        data = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadError(f"invalid UTF-8 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise PayloadError("payload must be a JSON object")

    seen_at = received_at or utc_now_iso()
    sht_match = SHT41_TOPIC_RE.fullmatch(topic)
    if sht_match:
        node_id = _required_int(data, "node_id", 1, UINT32_MAX)
        topic_node = int(sht_match.group(1))
        if node_id != topic_node:
            raise PayloadError(f"topic node {topic_node} does not match payload node {node_id}")
        _required_int(data, "sequence", 0, UINT32_MAX)
        _required_number(data, "temperature_c", -80, 125)
        _required_number(data, "humidity", 0, 100)
        _required_int(data, "battery_mv", 0, 20_000)
        if "status_flags" in data:
            _required_int(data, "status_flags", 0, UINT32_MAX)
        return DeviceRecord(
            kind="sht41",
            source_id=str(node_id),
            topic=topic,
            last_seen=seen_at,
        )

    sen_match = SEN66_TOPIC_RE.fullmatch(topic)
    if sen_match:
        _required_int(data, "co2", 0, 100_000)
        for key in ("pm1", "pm25", "pm4", "pm10"):
            _required_number(data, key, 0, 100_000)
        _required_int(data, "voc_index", 0, 500)
        _required_int(data, "nox_index", 0, 500)
        _required_number(data, "temperature_c", -80, 125)
        _required_number(data, "humidity", 0, 100)
        for key in ("node_id", "sequence", "status_flags", "schema_version"):
            if key in data:
                _required_int(data, key, 0, UINT32_MAX)
        metadata = {
            key: data[key]
            for key in ("firmware_version", "schema_version", "node_id")
            if key in data
        }
        return DeviceRecord(
            kind="sen66",
            source_id=sen_match.group(1),
            topic=topic,
            last_seen=seen_at,
            metadata=metadata,
        )

    raise PayloadError(f"unsupported topic: {topic}")


def discovery_messages(
    record: DeviceRecord,
    *,
    discovery_prefix: str = "homeassistant",
    stale_after_seconds: int = 1800,
) -> dict[str, str]:
    """Return stable retained configuration topic/payload pairs for a device."""

    if stale_after_seconds < 1:
        raise ValueError("stale_after_seconds must be positive")

    configs: dict[str, dict[str, Any]] = {}
    if record.kind == "sht41":
        configs.update(_sht41_configs(record))
    elif record.kind == "sen66":
        configs.update(_sen66_configs(record))
    else:
        raise ValueError(f"unsupported record kind: {record.kind}")

    configs["last_packet"] = {
        "platform": "sensor",
        "name": "Last packet",
        "device_class": "timestamp",
        "state_topic": last_seen_topic(discovery_prefix, record),
        "entity_category": "diagnostic",
        "availability_topic": service_availability_topic(discovery_prefix),
    }
    configs["online"] = {
        "platform": "binary_sensor",
        "name": "Online",
        "device_class": "connectivity",
        "state_topic": device_availability_topic(discovery_prefix, record),
        "payload_on": "online",
        "payload_off": "offline",
        "entity_category": "diagnostic",
        "availability_topic": service_availability_topic(discovery_prefix),
    }

    messages: dict[str, str] = {}
    for component_id, component in configs.items():
        component = dict(component)
        platform = str(component.pop("platform"))
        component.setdefault("unique_id", f"home_sensor_{record.key}_{component_id}")
        component.setdefault(
            "default_entity_id", f"{platform}.{record.key}_{entity_slug(component_id)}"
        )
        component.setdefault("qos", 1)
        component.setdefault("device", _device_info(record))
        component.setdefault(
            "origin",
            {"name": "Sensor Home MQTT discovery", "sw_version": __version__},
        )

        if component_id not in {"last_packet", "online"}:
            component.setdefault(
                "availability",
                [
                    {"topic": service_availability_topic(discovery_prefix)},
                    {"topic": device_availability_topic(discovery_prefix, record)},
                ],
            )
            component.setdefault("availability_mode", "all")
            component.setdefault("expire_after", stale_after_seconds)

        topic = (
            f"{discovery_prefix}/{platform}/{record.key}/{component_id}/config"
        )
        messages[topic] = json.dumps(component, sort_keys=True, separators=(",", ":"))
    return messages


def service_availability_topic(discovery_prefix: str) -> str:
    return f"{discovery_prefix}/{SERVICE_AVAILABILITY_SUFFIX}"


def device_availability_topic(discovery_prefix: str, record: DeviceRecord) -> str:
    return f"{discovery_prefix}/home_sensor/{record.key}/availability"


def last_seen_topic(discovery_prefix: str, record: DeviceRecord) -> str:
    return f"{discovery_prefix}/home_sensor/{record.key}/last_seen"


def is_stale(record: DeviceRecord, *, now: datetime, stale_after_seconds: int) -> bool:
    try:
        seen_at = datetime.fromisoformat(record.last_seen.replace("Z", "+00:00"))
    except ValueError:
        return True
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - seen_at.astimezone(timezone.utc)).total_seconds() >= stale_after_seconds


def registry_key(record: DeviceRecord) -> str:
    return f"{record.kind}:{record.source_id}"


def _sht41_configs(record: DeviceRecord) -> dict[str, dict[str, Any]]:
    source = record.topic
    return {
        "temperature": _measurement(
            source, "Temperature", "temperature_c", "temperature", "°C", precision=2
        ),
        "humidity": _measurement(
            source, "Relative humidity", "humidity", "humidity", "%", precision=1
        ),
        "battery_voltage": {
            **_measurement(
                source, "Battery voltage", "battery_mv", "voltage", "V", precision=3
            ),
            "value_template": (
                "{{ ((value_json.battery_mv | float) / 1000) | round(3) "
                "if value_json.status_flags is defined and "
                "((value_json.status_flags | int) | bitwise_and(4)) else none }}"
            ),
        },
        "status_flags": _diagnostic_json_sensor(source, "Status flags", "status_flags"),
        "sequence": _diagnostic_json_sensor(source, "Sequence", "sequence"),
        "battery_low": {
            "platform": "binary_sensor",
            "name": "Battery low",
            "device_class": "battery",
            "state_topic": source,
            "value_template": (
                "{{ 'ON' if value_json.status_flags is defined and "
                "((value_json.status_flags | int) | bitwise_and(8)) else 'OFF' }}"
            ),
            "payload_on": "ON",
            "payload_off": "OFF",
            "entity_category": "diagnostic",
        },
        "battery_shutdown": {
            "platform": "binary_sensor",
            "name": "Battery shutdown threshold",
            "device_class": "problem",
            "state_topic": source,
            "value_template": (
                "{{ 'ON' if value_json.status_flags is defined and "
                "((value_json.status_flags | int) | bitwise_and(16)) else 'OFF' }}"
            ),
            "payload_on": "ON",
            "payload_off": "OFF",
            "entity_category": "diagnostic",
        },
    }


def _sen66_configs(record: DeviceRecord) -> dict[str, dict[str, Any]]:
    source = record.topic
    result = {
        "temperature": _measurement(
            source, "Temperature", "temperature_c", "temperature", "°C", precision=2
        ),
        "humidity": _measurement(
            source, "Relative humidity", "humidity", "humidity", "%", precision=1
        ),
        "carbon_dioxide": _measurement(
            source, "Carbon dioxide", "co2", "carbon_dioxide", "ppm", precision=0
        ),
        "pm1": _measurement(source, "PM1.0", "pm1", "pm1", "µg/m³", precision=1),
        "pm25": _measurement(source, "PM2.5", "pm25", "pm25", "µg/m³", precision=1),
        "pm4": _measurement(source, "PM4.0", "pm4", "pm4", "µg/m³", precision=1),
        "pm10": _measurement(source, "PM10", "pm10", "pm10", "µg/m³", precision=1),
        "voc_index": _measurement(source, "VOC Index", "voc_index", None, "index", precision=0),
        "nox_index": _measurement(source, "NOx Index", "nox_index", None, "index", precision=0),
        "status_flags": _diagnostic_json_sensor(source, "Status flags", "status_flags"),
        "sequence": _diagnostic_json_sensor(source, "Sequence", "sequence"),
        "schema_version": _diagnostic_json_sensor(source, "Schema version", "schema_version"),
        "firmware_version": _diagnostic_json_sensor(source, "Firmware version", "firmware_version"),
        "device_status_warning": {
            "platform": "binary_sensor",
            "name": "Device status warning",
            "device_class": "problem",
            "state_topic": source,
            "value_template": (
                "{{ 'ON' if value_json.status_flags is defined and "
                "((value_json.status_flags | int) | bitwise_and(32)) else 'OFF' }}"
            ),
            "payload_on": "ON",
            "payload_off": "OFF",
            "entity_category": "diagnostic",
        },
    }
    return result


def _measurement(
    topic: str,
    name: str,
    field_name: str,
    device_class: str | None,
    unit: str,
    *,
    precision: int,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "platform": "sensor",
        "name": name,
        "state_topic": topic,
        "value_template": f"{{{{ value_json.{field_name} }}}}",
        "state_class": "measurement",
        "unit_of_measurement": unit,
        "suggested_display_precision": precision,
    }
    if device_class is not None:
        config["device_class"] = device_class
    return config


def _diagnostic_json_sensor(topic: str, name: str, field_name: str) -> dict[str, Any]:
    return {
        "platform": "sensor",
        "name": name,
        "state_topic": topic,
        "value_template": (
            f"{{{{ value_json.{field_name} if value_json.{field_name} is defined else none }}}}"
        ),
        "entity_category": "diagnostic",
    }


def _device_info(record: DeviceRecord) -> dict[str, Any]:
    info: dict[str, Any] = {
        "identifiers": [f"home_sensor_{record.key}"],
        "name": record.device_name,
        "manufacturer": "Sensor Home",
        "model": "SHT41 ESP-NOW node" if record.kind == "sht41" else "SEN66 Wi-Fi node",
    }
    firmware = record.metadata.get("firmware_version")
    if isinstance(firmware, str) and firmware:
        info["sw_version"] = firmware
    return info


def entity_slug(component_id: str) -> str:
    return {"pm25": "pm2_5"}.get(component_id, component_id)


def _required_int(data: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayloadError(f"{key} must be an integer")
    if value < minimum or value > maximum:
        raise PayloadError(f"{key} must be between {minimum} and {maximum}")
    return value


def _required_number(
    data: dict[str, Any], key: str, minimum: float, maximum: float
) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadError(f"{key} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise PayloadError(f"{key} must be between {minimum} and {maximum}")
    return number


def stable_unique_ids(messages: Iterable[str]) -> set[str]:
    """Test/diagnostic helper returning all unique IDs in serialized configs."""

    return {json.loads(payload)["unique_id"] for payload in messages}
