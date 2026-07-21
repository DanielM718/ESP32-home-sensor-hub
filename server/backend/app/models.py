"""Typed reading models used by the bridge and API layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Union

from app.battery_status import STATUS_BATTERY_OK


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
    co2: int
    pm1: float
    pm25: float
    pm4: float
    pm10: float
    voc_index: int
    nox_index: int
    temperature_c: float
    humidity: float
    received_at: datetime

    @property
    def measurement(self) -> str:
        return "air_quality_reading"

    @property
    def tags(self) -> dict[str, str]:
        return {
            "location": self.location,
            "topic": self.topic,
            "sensor_type": "air_quality",
        }

    @property
    def fields(self) -> dict[str, float | int]:
        return {
            "co2": self.co2,
            "pm1": self.pm1,
            "pm25": self.pm25,
            "pm4": self.pm4,
            "pm10": self.pm10,
            "voc_index": self.voc_index,
            "nox_index": self.nox_index,
            "temperature_c": self.temperature_c,
            "humidity": self.humidity,
        }


Reading = Union[SensorReading, AirQualityReading]


def utc_now() -> datetime:
    """Return an aware UTC timestamp for database writes."""

    return datetime.now(timezone.utc)
