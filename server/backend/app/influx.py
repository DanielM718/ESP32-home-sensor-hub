"""InfluxDB write helpers for sensor readings."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Iterable

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from app.config import InfluxSettings
from app.models import AirQualityReading, Reading


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
        self._read_client = InfluxDBClient(
            url=settings.url,
            token=settings.read_token or settings.write_token,
            org=settings.org,
        )
        self._query_api = self._read_client.query_api()

    def write_reading(self, reading: Reading, *, bucket: str | None = None) -> None:
        """Write one validated reading to InfluxDB."""

        point = reading_to_point(reading)
        self._write_api.write(
            bucket=bucket or self._settings.bucket,
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

    def write_point_data(self, point_data: Any, *, bucket: str | None = None) -> None:
        """Write an aggregate/event ``PointData`` without coupling app models to it."""

        point = Point(point_data.measurement)
        for key, value in point_data.tags.items():
            point = point.tag(key, value)
        for key, value in point_data.fields.items():
            point = point.field(key, value)
        point = point.time(point_data.timestamp)
        self._write_api.write(
            bucket=bucket or self._settings.bucket,
            org=self._settings.org,
            record=point,
        )

    def write_point_data_many(
        self, point_data: Iterable[Any], *, bucket: str | None = None
    ) -> None:
        points = []
        for item in point_data:
            point = Point(item.measurement)
            for key, value in item.tags.items():
                point = point.tag(key, value)
            for key, value in item.fields.items():
                point = point.field(key, value)
            points.append(point.time(item.timestamp))
        if points:
            self._write_api.write(
                bucket=bucket or self._settings.bucket,
                org=self._settings.org,
                record=points,
            )

    def recent_air_quality_readings(self, *, lookback_minutes: int) -> list[AirQualityReading]:
        """Read bounded live samples so an interrupted 15-minute window can resume."""

        fields = (
            "co2", "pm1", "pm25", "pm4", "pm10", "voc_index", "nox_index",
            "temperature_c", "humidity", "sraw_voc", "sraw_nox", "node_id",
            "sequence", "status_flags", "firmware_version", "schema_version",
            "boot_id", "sensor_uptime_s", "reset_reason", "sample_valid",
        )
        field_list = json.dumps(list(fields))
        flux = f'''from(bucket: {json.dumps(self._settings.live_bucket)})
  |> range(start: -{int(lookback_minutes)}m)
  |> filter(fn: (r) => r._measurement == "air_quality_reading")
  |> filter(fn: (r) => contains(value: r._field, set: {field_list}))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
        tables = self._query_api.query(query=flux, org=self._settings.org)
        readings = []
        for table in tables:
            for record in table.records:
                values = record.values
                location = values.get("location")
                topic = values.get("topic")
                received_at = values.get("_time")
                if not location or not topic or not isinstance(received_at, datetime):
                    continue
                readings.append(
                    AirQualityReading(
                        topic=str(topic),
                        location=str(location),
                        co2=_optional_int(values.get("co2")),
                        pm1=_optional_float(values.get("pm1")),
                        pm25=_optional_float(values.get("pm25")),
                        pm4=_optional_float(values.get("pm4")),
                        pm10=_optional_float(values.get("pm10")),
                        voc_index=_optional_int(values.get("voc_index")),
                        nox_index=_optional_int(values.get("nox_index")),
                        temperature_c=_optional_float(values.get("temperature_c")),
                        humidity=_optional_float(values.get("humidity")),
                        received_at=_aware_utc(received_at),
                        node_id=_optional_int(values.get("node_id") or values.get("node_id_1")),
                        sequence=_optional_int(values.get("sequence")),
                        status_flags=_optional_int(values.get("status_flags")),
                        firmware_version=_optional_string(values.get("firmware_version")),
                        schema_version=_optional_int(values.get("schema_version")),
                        boot_id=_optional_int(values.get("boot_id")),
                        sensor_uptime_s=_optional_int(values.get("sensor_uptime_s")),
                        reset_reason=_optional_int(values.get("reset_reason")),
                        sraw_voc=_optional_int(values.get("sraw_voc")),
                        sraw_nox=_optional_int(values.get("sraw_nox")),
                    )
                )
        return sorted(readings, key=lambda row: row.received_at)

    def active_air_quality_events(self) -> list[dict[str, Any]]:
        """Return permanent event points whose most recent state is active."""

        flux = f'''from(bucket: {json.dumps(self._settings.bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "air_quality_event")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => exists r.state and r.state == "active")
  |> sort(columns: ["_time"])
'''
        tables = self._query_api.query(query=flux, org=self._settings.org)
        return [dict(record.values) for table in tables for record in table.records]

    def close(self) -> None:
        """Close the underlying InfluxDB client."""

        self._client.close()
        self._read_client.close()

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


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
