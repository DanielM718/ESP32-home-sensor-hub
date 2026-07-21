"""Typed reading models used by the bridge and API layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Union

from app.battery_status import STATUS_BATTERY_OK


SEN66_STATUS_DEVICE_STATUS_NONZERO = 1 << 5


@dataclass(frozen=True)
class SensorReading:
    """Environmental reading from an ESP32-C3 battery sensor node."""

    topic: str
    node_id: int
    sequence: int
    temperature_c: float
    humidity: float
    battery_mv: int
    status_flags: int | None
    received_at: datetime

    @property
    def measurement(self) -> str:
        return "environment_reading"

    @property
    def tags(self) -> dict[str, str]:
        return {
            "node_id": str(self.node_id),
            "topic": self.topic,
            "sensor_type": "environment",
        }

    @property
    def fields(self) -> dict[str, float | int]:
        fields: dict[str, float | int] = {
            "sequence": self.sequence,
            "temperature_c": self.temperature_c,
            "humidity": self.humidity,
        }

        if self.status_flags is not None:
            fields["status_flags"] = self.status_flags
            if self.status_flags & STATUS_BATTERY_OK:
                fields["battery_mv"] = self.battery_mv

        return fields


@dataclass(frozen=True)
class AirQualityReading:
    """Validated room-level air-quality reading."""

    topic: str
    location: str
    co2: int | None
    pm1: float | None
    pm25: float | None
    pm4: float | None
    pm10: float | None
    voc_index: int | None
    nox_index: int | None
    temperature_c: float | None
    humidity: float | None
    received_at: datetime
    node_id: int | None = None
    sequence: int | None = None
    status_flags: int | None = None
    firmware_version: str | None = None
    schema_version: int | None = None
    boot_id: int | None = None
    sensor_uptime_s: int | None = None
    reset_reason: int | None = None
    sraw_voc: int | None = None
    sraw_nox: int | None = None

    @property
    def measurement(self) -> str:
        return "air_quality_reading"

    @property
    def tags(self) -> dict[str, str]:
        tags = {
            "location": self.location,
            "topic": self.topic,
            "sensor_type": "air_quality",
        }
        if self.node_id is not None:
            tags["node_id"] = str(self.node_id)
        return tags

    @property
    def sample_valid(self) -> bool:
        values_are_valid = all(
            value is not None
            for value in (
                self.co2,
                self.pm1,
                self.pm25,
                self.pm4,
                self.pm10,
                self.voc_index,
                self.nox_index,
                self.temperature_c,
                self.humidity,
            )
        )
        device_status_is_clear = (
            self.status_flags is None
            or not (self.status_flags & SEN66_STATUS_DEVICE_STATUS_NONZERO)
        )
        return values_are_valid and device_status_is_clear

    @property
    def fields(self) -> dict[str, float | int | bool | str]:
        fields: dict[str, float | int | bool | str] = {"sample_valid": self.sample_valid}
        measurements = {
            "co2": self.co2,
            "pm1": self.pm1,
            "pm25": self.pm25,
            "pm4": self.pm4,
            "pm10": self.pm10,
            "voc_index": self.voc_index,
            "nox_index": self.nox_index,
            "temperature_c": self.temperature_c,
            "humidity": self.humidity,
            "sraw_voc": self.sraw_voc,
            "sraw_nox": self.sraw_nox,
            "sequence": self.sequence,
            "status_flags": self.status_flags,
            "schema_version": self.schema_version,
            "boot_id": self.boot_id,
            "sensor_uptime_s": self.sensor_uptime_s,
            "reset_reason": self.reset_reason,
            "firmware_version": self.firmware_version,
        }
        fields.update({key: value for key, value in measurements.items() if value is not None})
        return fields


Reading = Union[SensorReading, AirQualityReading]


def utc_now() -> datetime:
    """Return an aware UTC timestamp for database writes."""

    return datetime.now(timezone.utc)
