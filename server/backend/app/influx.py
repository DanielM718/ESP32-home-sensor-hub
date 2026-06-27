"""InfluxDB write helpers for sensor readings."""

from __future__ import annotations

from typing import Iterable

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from app.config import InfluxSettings
from app.models import Reading


class InfluxWriter:
    """Synchronous InfluxDB writer used by the MQTT bridge."""

    def __init__(self, settings: InfluxSettings) -> None:
        self._settings = settings
        self._client = InfluxDBClient(
            url=settings.url,
            token=settings.write_token,
            org=settings.org,
        )
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    def write_reading(self, reading: Reading) -> None:
        """Write one validated reading to InfluxDB."""

        point = reading_to_point(reading)
        self._write_api.write(
            bucket=self._settings.bucket,
            org=self._settings.org,
            record=point,
        )

    def write_readings(self, readings: Iterable[Reading]) -> None:
        """Write multiple validated readings to InfluxDB."""

        points = [reading_to_point(reading) for reading in readings]
        if not points:
            return

        self._write_api.write(
            bucket=self._settings.bucket,
            org=self._settings.org,
            record=points,
        )

    def close(self) -> None:
        """Close the underlying InfluxDB client."""

        self._client.close()

    def __enter__(self) -> "InfluxWriter":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def reading_to_point(reading: Reading) -> Point:
    """Convert a typed reading into an InfluxDB point."""

    point = Point(reading.measurement)
    for key, value in reading.tags.items():
        point = point.tag(key, value)
    for key, value in reading.fields.items():
        point = point.field(key, value)
    return point.time(reading.received_at)
