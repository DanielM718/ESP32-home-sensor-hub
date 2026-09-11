"""Allowlisted, bounded InfluxDB queries used by monitoring and CSV exports."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import chain
from typing import TYPE_CHECKING, Any

from app.workflows import (
    AIR_QUALITY_FIELDS,
    AMS_FIELDS,
    BOOLEAN_FIELDS,
    ENVIRONMENT_FIELDS,
    PRINTER_FIELDS,
    SENSOR_TYPE_AIR_QUALITY,
    SENSOR_TYPE_AMS,
    SENSOR_TYPE_ENVIRONMENT,
    SENSOR_TYPE_PRINTER,
    Source,
    aggregate_field,
    fields_for_source,
    finite_csv_number,
    iso_utc,
    resolution_window_seconds,
    unit_for_field,
)

if TYPE_CHECKING:
    from app.config import InfluxSettings


ENVIRONMENT_MEASUREMENT = "environment_reading"
AIR_QUALITY_MEASUREMENT = "air_quality_reading"
AIR_QUALITY_AGGREGATE_MEASUREMENT = "air_quality_15m"
PRINTER_TELEMETRY_MEASUREMENT = "printer_telemetry"


@dataclass(frozen=True)
class ExportPoint:
    timestamp_utc: str
    sensor_type: str
    source_id: str
    node_id: int | None
    location: str | None
    field: str
    value: bool | int | float
    unit: str
    data_tier: str
    printer_id: str | None = None
    ams_id: str | None = None

    @property
    def source_key(self) -> tuple[str, str]:
        return self.sensor_type, self.source_id

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return self.timestamp_utc, self.sensor_type, self.source_id, self.field


class InfluxExportQueryRepository:
    """Query raw or stored aggregate data one bounded source/time chunk at a time."""

    def __init__(
        self,
        settings: InfluxSettings,
        *,
        query_api: Any | None = None,
        raw_retention_seconds: int = 72 * 60 * 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._client: Any | None = None
        if query_api is None:
            from influxdb_client import InfluxDBClient

            self._client = InfluxDBClient(
                url=settings.url,
                token=settings.read_token or settings.write_token,
                org=settings.org,
            )
            query_api = self._client.query_api()
        self._query_api = query_api
        self._raw_retention_seconds = raw_retention_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def query_source_type(
        self,
        *,
        sensor_type: str,
        start: datetime,
        stop: datetime,
        sources: Sequence[Source],
        fields: Sequence[str],
        resolution: str,
        use_stored_aggregate: bool = False,
    ) -> Iterable[ExportPoint]:
        matching = [source for source in sources if source.sensor_type == sensor_type]
        if not matching:
            return ()
        if sensor_type == SENSOR_TYPE_ENVIRONMENT:
            if use_stored_aggregate:
                return ()
            flux = environment_export_flux(
                self._settings.bucket, start, stop, matching, fields
            )
            points = self._environment_points(self._records(flux), matching, fields)
            return (
                points
                if resolution == "raw"
                else _downsample_points(points, start=start, resolution=resolution)
            )
        if sensor_type == SENSOR_TYPE_AIR_QUALITY:
            if not use_stored_aggregate:
                flux = air_quality_raw_export_flux(
                    self._settings.live_bucket, start, stop, matching, fields
                )
                points = self._air_raw_points(self._records(flux), matching, fields)
                return (
                    points
                    if resolution == "raw"
                    else _downsample_points(points, start=start, resolution=resolution)
                )
            flux = air_quality_aggregate_export_flux(
                self._settings.bucket, start, stop, matching, fields
            )
            return self._air_aggregate_points(self._records(flux), matching, fields)
        if sensor_type in {SENSOR_TYPE_PRINTER, SENSOR_TYPE_AMS}:
            return self._printer_points_for_interval(
                start=start,
                stop=stop,
                sources=matching,
                fields=fields,
                resolution=resolution,
                use_stored_aggregate=use_stored_aggregate,
            )
        return ()

    def _printer_points_for_interval(
        self,
        *,
        start: datetime,
        stop: datetime,
        sources: Sequence[Source],
        fields: Sequence[str],
        resolution: str,
        use_stored_aggregate: bool,
    ) -> Iterable[ExportPoint]:
        fields = tuple(
            dict.fromkeys(
                field
                for source in sources
                for field in fields_for_source(
                    source,
                    fields,
                    resolution,
                    stored_aggregate=use_stored_aggregate,
                )
            )
        )
        if not fields:
            return ()
        if use_stored_aggregate:
            points = self._printer_tier_points(
                self._settings.bucket,
                start,
                stop,
                sources,
                fields,
                data_tier="durable_5m",
            )
            return _downsample_points(points, start=start, resolution=resolution)

        if resolution in {"raw", "1m"}:
            points = self._printer_tier_points(
                self._settings.live_bucket,
                start,
                stop,
                sources,
                fields,
                data_tier="live_raw",
            )
            return (
                points
                if resolution == "raw"
                else _downsample_points(points, start=start, resolution=resolution)
            )

        boundary = _aligned_tier_boundary(
            self._clock().astimezone(timezone.utc)
            - timedelta(seconds=self._raw_retention_seconds),
            start=start,
            resolution=resolution,
        )
        tier_streams: list[Iterable[ExportPoint]] = []
        if start < boundary:
            durable_stop = min(stop, boundary)
            if durable_stop > start:
                tier_streams.append(
                    self._printer_tier_points(
                        self._settings.bucket,
                        start,
                        durable_stop,
                        sources,
                        fields,
                        data_tier="durable_5m",
                    )
                )
        if stop > boundary:
            live_start = max(start, boundary)
            if stop > live_start:
                tier_streams.append(
                    self._printer_tier_points(
                        self._settings.live_bucket,
                        live_start,
                        stop,
                        sources,
                        fields,
                        data_tier="live_raw",
                    )
                )
        return _downsample_points(
            chain.from_iterable(tier_streams), start=start, resolution=resolution
        )

    def _printer_tier_points(
        self,
        bucket: str,
        start: datetime,
        stop: datetime,
        sources: Sequence[Source],
        fields: Sequence[str],
        *,
        data_tier: str,
    ) -> Iterator[ExportPoint]:
        flux = printer_telemetry_export_flux(bucket, start, stop, sources, fields)
        return self._printer_points(
            self._records(flux), sources, fields, data_tier=data_tier
        )

    def monitoring_preview(
        self,
        *,
        start: datetime,
        stop: datetime,
        sources: Sequence[Source],
        fields: Sequence[str],
        limit: int = 20,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        source_results: dict[tuple[str, str], dict[str, Any]] = {
            source.key: {
                **source.as_dict(),
                "source_id": source.source_id,
                "measurement_values": 0,
                "first_sample_timestamp": None,
                "latest_sample_timestamp": None,
                "has_data": False,
            }
            for source in sources
        }
        recent: list[ExportPoint] = []
        for sensor_type in (
            SENSOR_TYPE_ENVIRONMENT,
            SENSOR_TYPE_AIR_QUALITY,
            SENSOR_TYPE_PRINTER,
            SENSOR_TYPE_AMS,
        ):
            matching = [
                source for source in sources if source.sensor_type == sensor_type
            ]
            if not matching:
                continue
            summary_flux = preview_summary_flux(
                self._settings.bucket
                if sensor_type == SENSOR_TYPE_ENVIRONMENT
                else self._settings.live_bucket,
                sensor_type,
                start,
                stop,
                matching,
                fields,
            )
            for record in self._records(summary_flux):
                values = _values(record)
                key = _source_key(values, sensor_type)
                if key not in source_results:
                    continue
                marker = str(values.get("_field") or _record_field(record))
                target = source_results[key]
                if marker == "__count":
                    count = values.get("_value", _record_value(record))
                    if isinstance(count, int) and not isinstance(count, bool):
                        target["measurement_values"] += max(0, count)
                        target["has_data"] = target["measurement_values"] > 0
                elif marker == "__first":
                    timestamp = _record_time(record)
                    current = target["first_sample_timestamp"]
                    target["first_sample_timestamp"] = (
                        timestamp if current is None else min(current, timestamp)
                    )
                elif marker == "__last":
                    timestamp = _record_time(record)
                    current = target["latest_sample_timestamp"]
                    target["latest_sample_timestamp"] = (
                        timestamp if current is None else max(current, timestamp)
                    )

            recent_flux = preview_recent_flux(
                self._settings.bucket
                if sensor_type == SENSOR_TYPE_ENVIRONMENT
                else self._settings.live_bucket,
                sensor_type,
                start,
                stop,
                matching,
                fields,
                limit=limit,
            )
            recent.extend(
                self._long_records_to_points(
                    self._records(recent_flux), sensor_type=sensor_type, data_tier="raw"
                )
            )

        recent.sort(key=lambda point: point.sort_key, reverse=True)
        recent = recent[:limit]
        nonempty = [item for item in source_results.values() if item["has_data"]]
        firsts = [
            item["first_sample_timestamp"]
            for item in nonempty
            if item["first_sample_timestamp"]
        ]
        lasts = [
            item["latest_sample_timestamp"]
            for item in nonempty
            if item["latest_sample_timestamp"]
        ]
        return {
            "row_count": sum(
                item["measurement_values"] for item in source_results.values()
            ),
            "row_count_is_approximate": True,
            "row_count_kind": "selected measurement values",
            "first_sample_timestamp": min(firsts) if firsts else None,
            "latest_sample_timestamp": max(lasts) if lasts else None,
            "source_presence": list(source_results.values()),
            "recent_samples": [point_to_dict(point) for point in recent],
            "recent_sample_limit": limit,
            "warnings": [],
        }

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _records(self, flux: str) -> Iterable[Any]:
        stream = getattr(self._query_api, "query_stream", None)
        if callable(stream):
            return stream(query=flux, org=self._settings.org)
        tables = self._query_api.query(query=flux, org=self._settings.org)
        return (record for table in tables for record in table.records)

    def _environment_points(
        self,
        records: Iterable[Any],
        sources: Sequence[Source],
        requested_fields: Sequence[str],
    ) -> Iterator[ExportPoint]:
        allowed_ids = {str(source.node_id) for source in sources}
        selected = tuple(
            field for field in requested_fields if field in ENVIRONMENT_FIELDS
        )
        for record in records:
            values = _values(record)
            node_text = str(values.get("node_id") or "")
            if node_text not in allowed_ids:
                continue
            try:
                node_id = int(node_text)
            except ValueError:
                continue
            timestamp = _record_time(record)
            status_flags = values.get("status_flags")
            battery_ok = (
                isinstance(status_flags, int)
                and not isinstance(status_flags, bool)
                and status_flags & 4
            )
            for field in selected:
                if field == "battery_mv" and not battery_ok:
                    continue
                number = finite_csv_number(values.get(field))
                if number is None:
                    continue
                yield ExportPoint(
                    timestamp,
                    SENSOR_TYPE_ENVIRONMENT,
                    node_text,
                    node_id,
                    None,
                    field,
                    number,
                    unit_for_field(field),
                    "raw",
                )

    def _air_raw_points(
        self,
        records: Iterable[Any],
        sources: Sequence[Source],
        requested_fields: Sequence[str],
    ) -> Iterator[ExportPoint]:
        allowed_locations = {str(source.location) for source in sources}
        selected = tuple(
            field for field in requested_fields if field in AIR_QUALITY_FIELDS
        )
        for record in records:
            values = _values(record)
            location = str(values.get("location") or "")
            if location not in allowed_locations or values.get("sample_valid") is False:
                continue
            timestamp = _record_time(record)
            node_id = _optional_int(values.get("node_id"))
            for field in selected:
                number = finite_csv_number(values.get(field))
                if number is None:
                    continue
                yield ExportPoint(
                    timestamp,
                    SENSOR_TYPE_AIR_QUALITY,
                    location,
                    node_id,
                    location,
                    field,
                    number,
                    unit_for_field(field),
                    "raw",
                )

    def _air_aggregate_points(
        self,
        records: Iterable[Any],
        sources: Sequence[Source],
        requested_fields: Sequence[str],
    ) -> Iterator[ExportPoint]:
        allowed_locations = {str(source.location) for source in sources}
        selected = tuple(
            field for field in requested_fields if field in AIR_QUALITY_FIELDS
        )
        for record in records:
            values = _values(record)
            location = str(values.get("location") or "")
            if location not in allowed_locations:
                continue
            timestamp = _record_time(record)
            node_id = _optional_int(values.get("node_id"))
            for field in selected:
                number = finite_csv_number(values.get(aggregate_field(field)))
                if number is None:
                    continue
                yield ExportPoint(
                    timestamp,
                    SENSOR_TYPE_AIR_QUALITY,
                    location,
                    node_id,
                    location,
                    field,
                    number,
                    unit_for_field(field),
                    "stored_15m",
                )

    def _printer_points(
        self,
        records: Iterable[Any],
        sources: Sequence[Source],
        requested_fields: Sequence[str],
        *,
        data_tier: str,
    ) -> Iterator[ExportPoint]:
        allowed = {source.key: source for source in sources}
        for record in records:
            values = _values(record)
            sensor_type = str(values.get("component_type") or "")
            key = _source_key(values, sensor_type)
            source = allowed.get(key)
            if source is None:
                continue
            selected = fields_for_source(source, requested_fields, "raw")
            timestamp = _record_time(record)
            for field in selected:
                value = _telemetry_value(values.get(field), field)
                if value is None:
                    continue
                yield ExportPoint(
                    timestamp,
                    sensor_type,
                    source.source_id,
                    None,
                    None,
                    field,
                    value,
                    unit_for_field(field),
                    data_tier,
                    source.printer_id,
                    source.ams_id,
                )

    def _long_records_to_points(
        self, records: Iterable[Any], *, sensor_type: str, data_tier: str
    ) -> Iterator[ExportPoint]:
        for record in records:
            values = _values(record)
            field = str(values.get("_field") or _record_field(record))
            value = _telemetry_value(values.get("_value", _record_value(record)), field)
            if (
                field
                not in ENVIRONMENT_FIELDS
                + AIR_QUALITY_FIELDS
                + PRINTER_FIELDS
                + AMS_FIELDS
                or value is None
            ):
                continue
            if sensor_type == SENSOR_TYPE_ENVIRONMENT:
                source_id = str(values.get("node_id") or "")
                node_id = _optional_int(source_id)
                location = None
                printer_id = None
                ams_id = None
            elif sensor_type == SENSOR_TYPE_AIR_QUALITY:
                source_id = str(values.get("location") or "")
                node_id = _optional_int(values.get("node_id"))
                location = source_id
                printer_id = None
                ams_id = None
            else:
                printer_id = str(values.get("printer_id") or "")
                component_id = str(values.get("component_id") or "")
                source_id = (
                    printer_id
                    if sensor_type == SENSOR_TYPE_PRINTER
                    else f"{printer_id}/{component_id}"
                )
                node_id = None
                location = None
                ams_id = component_id if sensor_type == SENSOR_TYPE_AMS else None
            if not source_id:
                continue
            yield ExportPoint(
                _record_time(record),
                sensor_type,
                source_id,
                node_id,
                location,
                field,
                value,
                unit_for_field(field),
                data_tier,
                printer_id,
                ams_id,
            )


def environment_export_flux(
    bucket: str,
    start: datetime,
    stop: datetime,
    sources: Sequence[Source],
    fields: Sequence[str],
) -> str:
    selected = tuple(
        dict.fromkeys(
            field
            for source in sources
            for field in fields_for_source(source, fields, "raw")
        )
    )
    query_fields = selected + (("status_flags",) if "battery_mv" in selected else ())
    return _pivot_flux(
        bucket=bucket,
        start=start,
        stop=stop,
        measurement=ENVIRONMENT_MEASUREMENT,
        fields=query_fields,
        identity_column="node_id",
        identities=[source.source_id for source in sources],
    )


def air_quality_raw_export_flux(
    bucket: str,
    start: datetime,
    stop: datetime,
    sources: Sequence[Source],
    fields: Sequence[str],
) -> str:
    selected = tuple(
        dict.fromkeys(
            field
            for source in sources
            for field in fields_for_source(source, fields, "raw")
        )
    )
    return _pivot_flux(
        bucket=bucket,
        start=start,
        stop=stop,
        measurement=AIR_QUALITY_MEASUREMENT,
        fields=selected + ("sample_valid",),
        identity_column="location",
        identities=[source.source_id for source in sources],
    )


def air_quality_aggregate_export_flux(
    bucket: str,
    start: datetime,
    stop: datetime,
    sources: Sequence[Source],
    fields: Sequence[str],
) -> str:
    selected = tuple(
        dict.fromkeys(
            aggregate_field(field)
            for source in sources
            for field in fields_for_source(source, fields, "15m")
        )
    )
    return _pivot_flux(
        bucket=bucket,
        start=start,
        stop=stop,
        measurement=AIR_QUALITY_AGGREGATE_MEASUREMENT,
        fields=selected,
        identity_column="location",
        identities=[source.source_id for source in sources],
    )


def printer_telemetry_export_flux(
    bucket: str,
    start: datetime,
    stop: datetime,
    sources: Sequence[Source],
    fields: Sequence[str],
) -> str:
    selected = tuple(
        dict.fromkeys(
            field
            for source in sources
            for field in fields_for_source(source, fields, "raw")
        )
    )
    source_filters = []
    for source in sources:
        component_id = (
            "main" if source.sensor_type == SENSOR_TYPE_PRINTER else source.ams_id
        )
        source_filters.append(
            "(r.component_type == "
            + _flux_string(source.sensor_type)
            + " and r.printer_id == "
            + _flux_string(str(source.printer_id))
            + " and r.component_id == "
            + _flux_string(str(component_id))
            + ")"
        )
    source_filter = " or ".join(source_filters) or "false"
    return f"""from(bucket: {_flux_string(bucket)})
  |> range(start: time(v: {_flux_string(iso_utc(start))}), stop: time(v: {_flux_string(iso_utc(stop))}))
  |> filter(fn: (r) => r._measurement == {_flux_string(PRINTER_TELEMETRY_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(selected)}))
  |> filter(fn: (r) => {source_filter})
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time", "printer_id", "component_type", "component_id"])
"""


def preview_summary_flux(
    bucket: str,
    sensor_type: str,
    start: datetime,
    stop: datetime,
    sources: Sequence[Source],
    fields: Sequence[str],
) -> str:
    base = _long_base_flux(bucket, sensor_type, start, stop, sources, fields)
    identity = (
        ["node_id"]
        if sensor_type == SENSOR_TYPE_ENVIRONMENT
        else ["location"]
        if sensor_type == SENSOR_TYPE_AIR_QUALITY
        else ["printer_id", "component_type", "component_id"]
    )
    group_columns = ", ".join(_flux_string(column) for column in identity + ["_field"])
    return f"""{base}

counts = data
  |> group(columns: [{group_columns}])
  |> count(column: "_value")
  |> map(fn: (r) => ({{r with _field: "__count"}}))

firsts = data
  |> group(columns: [{group_columns}])
  |> first()
  |> map(fn: (r) => ({{r with _field: "__first"}}))

lasts = data
  |> group(columns: [{group_columns}])
  |> last()
  |> map(fn: (r) => ({{r with _field: "__last"}}))

union(tables: [counts, firsts, lasts])
"""


def preview_recent_flux(
    bucket: str,
    sensor_type: str,
    start: datetime,
    stop: datetime,
    sources: Sequence[Source],
    fields: Sequence[str],
    *,
    limit: int,
) -> str:
    base = _long_base_flux(bucket, sensor_type, start, stop, sources, fields)
    return f"""{base}

data
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: {int(limit)})
"""


def _long_base_flux(
    bucket: str,
    sensor_type: str,
    start: datetime,
    stop: datetime,
    sources: Sequence[Source],
    fields: Sequence[str],
) -> str:
    if sensor_type == SENSOR_TYPE_ENVIRONMENT:
        measurement = ENVIRONMENT_MEASUREMENT
        identity = "node_id"
        supported = ENVIRONMENT_FIELDS
        identity_filter = f"contains(value: r.{identity}, set: {_flux_array([source.source_id for source in sources])})"
    elif sensor_type == SENSOR_TYPE_AIR_QUALITY:
        measurement = AIR_QUALITY_MEASUREMENT
        identity = "location"
        supported = AIR_QUALITY_FIELDS
        identity_filter = f"contains(value: r.{identity}, set: {_flux_array([source.source_id for source in sources])})"
    else:
        measurement = PRINTER_TELEMETRY_MEASUREMENT
        supported = PRINTER_FIELDS if sensor_type == SENSOR_TYPE_PRINTER else AMS_FIELDS
        filters = []
        for source in sources:
            component_id = (
                "main" if sensor_type == SENSOR_TYPE_PRINTER else source.ams_id
            )
            filters.append(
                f"(r.printer_id == {_flux_string(str(source.printer_id))} and "
                f"r.component_type == {_flux_string(sensor_type)} and "
                f"r.component_id == {_flux_string(str(component_id))})"
            )
        identity_filter = " or ".join(filters) or "false"
    selected = [field for field in fields if field in supported]
    return f"""data = from(bucket: {_flux_string(bucket)})
  |> range(start: time(v: {_flux_string(iso_utc(start))}), stop: time(v: {_flux_string(iso_utc(stop))}))
  |> filter(fn: (r) => r._measurement == {_flux_string(measurement)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(selected)}))
  |> filter(fn: (r) => {identity_filter})"""


def _pivot_flux(
    *,
    bucket: str,
    start: datetime,
    stop: datetime,
    measurement: str,
    fields: Sequence[str],
    identity_column: str,
    identities: Sequence[str],
) -> str:
    return f'''from(bucket: {_flux_string(bucket)})
  |> range(start: time(v: {_flux_string(iso_utc(start))}), stop: time(v: {_flux_string(iso_utc(stop))}))
  |> filter(fn: (r) => r._measurement == {_flux_string(measurement)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(fields)}))
  |> filter(fn: (r) => contains(value: r.{identity_column}, set: {_flux_array(identities)}))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time", "{identity_column}"])
'''


def point_to_dict(point: ExportPoint) -> dict[str, Any]:
    return {
        "timestamp_utc": point.timestamp_utc,
        "sensor_type": point.sensor_type,
        "source_id": point.source_id,
        "node_id": point.node_id,
        "location": point.location,
        "printer_id": point.printer_id,
        "ams_id": point.ams_id,
        "field": point.field,
        "value": point.value,
        "unit": point.unit,
        "data_tier": point.data_tier,
    }


def _downsample_points(
    points: Iterable[ExportPoint],
    *,
    start: datetime,
    resolution: str,
) -> Iterator[ExportPoint]:
    """Mean numeric raw values into source/field buckets anchored at chunk start."""

    window_seconds = resolution_window_seconds(resolution)
    if window_seconds is None:
        yield from points
        return

    buckets: dict[
        tuple[
            datetime,
            str,
            str,
            int | None,
            str | None,
            str | None,
            str | None,
            str,
            str,
        ],
        list[float],
    ] = {}
    for point in points:
        parsed = datetime.fromisoformat(point.timestamp_utc.replace("Z", "+00:00"))
        offset_seconds = max(0, (parsed - start).total_seconds())
        bucket_time = start + timedelta(
            seconds=int(offset_seconds // window_seconds) * window_seconds
        )
        # Field sorts last so that every field of one
        # (time, source, tier) row is contiguous for the wide writer's groupby.
        key = (
            bucket_time,
            point.sensor_type,
            point.source_id,
            point.node_id,
            point.location,
            point.printer_id,
            point.ams_id,
            point.data_tier,
            point.field,
        )
        buckets.setdefault(key, []).append(float(point.value))

    for key in sorted(buckets):
        (
            bucket_time,
            sensor_type,
            source_id,
            node_id,
            location,
            printer_id,
            ams_id,
            source_tier,
            field,
        ) = key
        values = buckets[key]
        data_tier = (
            f"{resolution}_mean"
            if source_tier == "raw"
            else f"{resolution}_mean_from_{source_tier}"
        )
        yield ExportPoint(
            timestamp_utc=iso_utc(bucket_time),
            sensor_type=sensor_type,
            source_id=source_id,
            node_id=node_id,
            location=location,
            field=field,
            value=sum(values) / len(values),
            unit=unit_for_field(field),
            data_tier=data_tier,
            printer_id=printer_id,
            ams_id=ams_id,
        )


def _aligned_tier_boundary(
    boundary: datetime, *, start: datetime, resolution: str
) -> datetime:
    """Round the live-retention boundary up onto the downsample grid.

    ``_downsample_points`` anchors its windows at ``start``. If the tier
    boundary fell inside a window, that window would be averaged twice - once
    per tier - and the wide writer would emit one partially populated row per
    tier. Rounding up keeps every emitted bucket whole and single-tier; the
    straddling window resolves to durable data, which is the tier guaranteed to
    still cover it.
    """

    window_seconds = resolution_window_seconds(resolution)
    if window_seconds is None or boundary <= start:
        return boundary
    offset_seconds = (boundary - start).total_seconds()
    windows = int(-(-offset_seconds // window_seconds))
    return start + timedelta(seconds=windows * window_seconds)


def _values(record: Any) -> Mapping[str, Any]:
    values = getattr(record, "values", {})
    return values if isinstance(values, Mapping) else {}


def _record_field(record: Any) -> str:
    method = getattr(record, "get_field", None)
    return (
        str(method()) if callable(method) else str(_values(record).get("_field") or "")
    )


def _record_value(record: Any) -> Any:
    method = getattr(record, "get_value", None)
    return method() if callable(method) else _values(record).get("_value")


def _record_time(record: Any) -> str:
    method = getattr(record, "get_time", None)
    value = method() if callable(method) else _values(record).get("_time")
    if isinstance(value, datetime):
        return iso_utc(value)
    return str(value)


def _source_key(values: Mapping[str, Any], sensor_type: str) -> tuple[str, str]:
    if sensor_type == SENSOR_TYPE_ENVIRONMENT:
        identity = values.get("node_id")
    elif sensor_type == SENSOR_TYPE_AIR_QUALITY:
        identity = values.get("location")
    elif sensor_type == SENSOR_TYPE_PRINTER:
        identity = values.get("printer_id")
    else:
        printer_id = values.get("printer_id")
        component_id = values.get("component_id")
        identity = (
            f"{printer_id}/{component_id}"
            if printer_id not in (None, "") and component_id not in (None, "")
            else ""
        )
    return sensor_type, str(identity or "")


def _telemetry_value(value: Any, field: str) -> bool | int | float | None:
    if field in BOOLEAN_FIELDS:
        return value if isinstance(value, bool) else None
    return finite_csv_number(value)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flux_string(value: str) -> str:
    return json.dumps(value)


def _flux_array(values: Sequence[str]) -> str:
    return "[" + ", ".join(_flux_string(value) for value in values) + "]"
