"""InfluxDB query helpers for the Flask REST API."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

from app.air_quality_policy import interpret_station, rolling_24h_status
from app.battery_status import decode_battery_status
from app.workflows import AMS_FIELDS, PRINTER_FIELDS

if TYPE_CHECKING:
    from app.config import InfluxSettings


SUPPORTED_RANGES: dict[str, tuple[str, str]] = {
    "1h": ("-1h", "1m"),
    "24h": ("-24h", "15m"),
    "7d": ("-7d", "1h"),
    "30d": ("-30d", "6h"),
}
DEFAULT_RANGE = "24h"
ENVIRONMENT_LATEST_LOOKBACK = "-7d"
AIR_QUALITY_LATEST_LOOKBACK = "-30m"
SENSOR_TYPE_ALL = "all"
SENSOR_TYPE_ENVIRONMENT = "environment"
SENSOR_TYPE_AIR_QUALITY = "air_quality"
SENSOR_TYPE_PRINTER = "printer"
SENSOR_TYPE_AMS = "ams"
SENSOR_TYPES = {
    SENSOR_TYPE_ALL,
    SENSOR_TYPE_ENVIRONMENT,
    SENSOR_TYPE_AIR_QUALITY,
}

ENVIRONMENT_MEASUREMENT = "environment_reading"
AIR_QUALITY_MEASUREMENT = "air_quality_reading"
AIR_QUALITY_AGGREGATE_MEASUREMENT = "air_quality_15m"
AIR_QUALITY_EVENT_MEASUREMENT = "air_quality_event"
PRINTER_TELEMETRY_MEASUREMENT = "printer_telemetry"
PRINTER_TELEMETRY_LATEST_LOOKBACK = "-30m"

ENVIRONMENT_LATEST_FIELDS = (
    "sequence",
    "temperature_c",
    "humidity",
    "battery_mv",
    "status_flags",
)
ENVIRONMENT_HISTORY_FIELDS = (
    "temperature_c",
    "humidity",
    "battery_mv",
    "status_flags",
)
AIR_QUALITY_FIELDS = (
    "co2",
    "pm1",
    "pm25",
    "pm4",
    "pm10",
    "voc_index",
    "nox_index",
    "temperature_c",
    "humidity",
)
AIR_QUALITY_RAW_FIELDS = ("sraw_voc", "sraw_nox")
AIR_QUALITY_METADATA_FIELDS = (
    "sample_valid",
    "sequence",
    "status_flags",
    "schema_version",
    "boot_id",
    "sensor_uptime_s",
    "reset_reason",
    "firmware_version",
)
AIR_QUALITY_LATEST_FIELDS = (
    AIR_QUALITY_FIELDS + AIR_QUALITY_RAW_FIELDS + AIR_QUALITY_METADATA_FIELDS
)
AIR_QUALITY_MAX_FIELDS = ("co2", "pm1", "pm25", "pm4", "pm10", "voc_index", "nox_index")
AIR_QUALITY_P95_FIELDS = ("co2", "pm25", "pm10", "voc_index", "nox_index")

LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INVENTORY_REFRESH_SECONDS = 15 * 60
LOGGER = logging.getLogger("home_sensor.queries")


class QueryValidationError(ValueError):
    """Raised when API query parameters are invalid."""


@dataclass(frozen=True)
class ReadingsQuery:
    range_key: str
    flux_start: str
    window_every: str
    sensor_type: str
    node_id: int | None = None
    location: str | None = None


class RecordLike(Protocol):
    values: Mapping[str, Any]

    def get_field(self) -> str: ...

    def get_measurement(self) -> str: ...

    def get_time(self) -> datetime: ...

    def get_value(self) -> Any: ...


class InfluxReadRepository:
    """Read repository used by the Flask API."""

    def __init__(
        self,
        settings: InfluxSettings,
        *,
        expected_publish_seconds: int = 5,
        minimum_coverage_percent: int = 75,
        inventory_refresh_seconds: float = INVENTORY_REFRESH_SECONDS,
    ) -> None:
        from influxdb_client import InfluxDBClient

        token = settings.read_token or settings.write_token
        self._settings = settings
        self._client = InfluxDBClient(
            url=settings.url,
            token=token,
            org=settings.org,
        )
        self._query_api = self._client.query_api()
        self._expected_publish_seconds = expected_publish_seconds
        self._minimum_coverage_percent = minimum_coverage_percent
        try:
            inventory = self._load_inventory()
        except Exception:
            self._client.close()
            LOGGER.exception(
                "Durable sensor inventory reconstruction failed; refusing to "
                "start with an empty inventory"
            )
            raise
        self._inventory = DurableInventoryCache(
            inventory,
            refresh_seconds=inventory_refresh_seconds,
        )

    def latest(self) -> dict[str, Any]:
        records = self._query(
            latest_flux(self._settings.bucket, self._settings.live_bucket)
        )
        latest = self._inventory.observe(
            latest_response(records, include_internal=True)
        )
        self._inventory.refresh_if_due(self._load_inventory)
        return latest

    def _load_inventory(self) -> dict[str, Any]:
        records = self._query(durable_inventory_flux(self._settings.bucket))
        return latest_response(records, include_internal=True)

    def readings(self, query: ReadingsQuery) -> dict[str, Any]:
        records = self._query(
            readings_flux(
                self._settings.bucket,
                query,
                live_bucket=self._settings.live_bucket,
            )
        )
        events = []
        if query.sensor_type != SENSOR_TYPE_ENVIRONMENT:
            events = self._query(events_flux(self._settings.bucket, query))
        return readings_response(records, query, event_records=events)

    def air_quality_context(self) -> dict[str, Any]:
        records = self._query(
            air_quality_context_flux(
                self._settings.bucket,
                self._settings.live_bucket,
            )
        )
        return air_quality_context_response(
            records,
            expected_publish_seconds=self._expected_publish_seconds,
            minimum_coverage_percent=self._minimum_coverage_percent,
        )

    def nodes(
        self,
        *,
        stale_after_seconds: int,
        air_quality_stale_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        latest = self.latest()
        return nodes_response(
            latest,
            stale_after_seconds=stale_after_seconds,
            air_quality_stale_after_seconds=air_quality_stale_after_seconds,
        )

    def close(self) -> None:
        self._inventory.close()
        self._client.close()

    def _query(self, flux: str) -> list[RecordLike]:
        tables = self._query_api.query(query=flux, org=self._settings.org)
        return [record for table in tables for record in table.records]


class DurableInventoryCache:
    """Thread-safe permanent inventory plus identities observed by this process."""

    def __init__(
        self,
        durable: Mapping[str, Any],
        *,
        refresh_seconds: float = INVENTORY_REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        self._clock = clock
        self._refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._durable = deepcopy(dict(durable))
        self._observed = _empty_latest_payload()
        self._refresh_baseline: dict[str, Any] | None = None
        self._next_refresh = float(clock()) + refresh_seconds
        self._refreshing = False
        self._closed = False
        self._refresh_thread: threading.Thread | None = None

    def observe(self, latest: Mapping[str, Any]) -> dict[str, Any]:
        """Record bounded observations and return the merged inventory snapshot."""

        with self._lock:
            self._observed = merge_latest_payloads(
                self._observed,
                latest,
                include_internal=True,
            )
            return self._snapshot_locked(
                generated_at=str(latest.get("generated_at") or _now_iso())
            )

    def snapshot(self) -> dict[str, Any]:
        """Return known-good state without mutating the cache."""

        with self._lock:
            return self._snapshot_locked(generated_at=_now_iso())

    def refresh_if_due(self, loader: Callable[[], Mapping[str, Any]]) -> bool:
        """Start at most one non-blocking durable refresh when its interval expires."""

        with self._lock:
            if (
                self._closed
                or self._refreshing
                or float(self._clock()) < self._next_refresh
            ):
                return False
            self._refreshing = True
            self._refresh_baseline = self._observed
            self._observed = _empty_latest_payload()
            thread = threading.Thread(
                target=self._refresh,
                args=(loader,),
                name="durable-inventory-refresh",
                daemon=True,
            )
            self._refresh_thread = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._restore_refresh_baseline_locked()
                self._refreshing = False
                self._next_refresh = float(self._clock()) + self._refresh_seconds
            raise
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            thread = self._refresh_thread
        if thread is not None and thread.is_alive():
            thread.join()

    def _refresh(self, loader: Callable[[], Mapping[str, Any]]) -> None:
        refreshed: dict[str, Any] | None = None
        try:
            refreshed = deepcopy(dict(loader()))
        except Exception:
            LOGGER.exception(
                "Durable sensor inventory refresh failed; retaining known-good state"
            )
        finally:
            with self._lock:
                if refreshed is not None and not self._closed:
                    self._durable = refreshed
                    # Only observations made before this refresh began are now
                    # superseded. Preserve anything observed while it was running.
                    self._refresh_baseline = None
                else:
                    self._restore_refresh_baseline_locked()
                self._refreshing = False
                self._next_refresh = float(self._clock()) + self._refresh_seconds

    def _snapshot_locked(self, *, generated_at: str) -> dict[str, Any]:
        combined = self._durable
        if self._refresh_baseline is not None:
            combined = merge_latest_payloads(
                combined,
                self._refresh_baseline,
                include_internal=True,
            )
        return merge_latest_payloads(
            combined,
            self._observed,
            generated_at=generated_at,
        )

    def _restore_refresh_baseline_locked(self) -> None:
        if self._refresh_baseline is None:
            return
        self._observed = merge_latest_payloads(
            self._refresh_baseline,
            self._observed,
            include_internal=True,
        )
        self._refresh_baseline = None


def readings_query_from_params(params: Mapping[str, str | None]) -> ReadingsQuery:
    """Validate `/api/readings` query parameters."""

    range_key = _param(params, "range", DEFAULT_RANGE)
    if range_key not in SUPPORTED_RANGES:
        allowed = ", ".join(SUPPORTED_RANGES)
        raise QueryValidationError(f"range must be one of: {allowed}")

    sensor_type = _param(params, "sensor_type", _param(params, "type", SENSOR_TYPE_ALL))
    if sensor_type not in SENSOR_TYPES:
        allowed = ", ".join(sorted(SENSOR_TYPES))
        raise QueryValidationError(f"sensor_type must be one of: {allowed}")

    node_id = _optional_node_id(_param(params, "node_id", ""))
    location = _optional_location(_param(params, "location", ""))

    if node_id is not None and location is not None:
        raise QueryValidationError("node_id and location cannot be combined")
    if sensor_type == SENSOR_TYPE_ENVIRONMENT and location is not None:
        raise QueryValidationError("location is only valid for air_quality readings")
    if sensor_type == SENSOR_TYPE_AIR_QUALITY and node_id is not None:
        raise QueryValidationError("node_id is only valid for environment readings")

    flux_start, window_every = SUPPORTED_RANGES[range_key]
    return ReadingsQuery(
        range_key=range_key,
        flux_start=flux_start,
        window_every=window_every,
        sensor_type=sensor_type,
        node_id=node_id,
        location=location,
    )


def latest_flux(bucket: str, live_bucket: str | None = None) -> str:
    live_bucket = live_bucket or bucket
    return f"""environmentLatest = from(bucket: {_flux_string(bucket)})
  |> range(start: {ENVIRONMENT_LATEST_LOOKBACK})
  |> filter(fn: (r) => r._measurement == {_flux_string(ENVIRONMENT_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(ENVIRONMENT_LATEST_FIELDS)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()

airQualityLatest = from(bucket: {_flux_string(live_bucket)})
  |> range(start: {AIR_QUALITY_LATEST_LOOKBACK})
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(AIR_QUALITY_LATEST_FIELDS)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()

printerTelemetryLatest = from(bucket: {_flux_string(live_bucket)})
  |> range(start: {PRINTER_TELEMETRY_LATEST_LOOKBACK})
  |> filter(fn: (r) => r._measurement == {_flux_string(PRINTER_TELEMETRY_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(PRINTER_FIELDS + AMS_FIELDS)}))
  |> group(columns: ["_measurement", "printer_id", "component_type", "component_id", "source", "_field"])
  |> last(column: "_value")

union(tables: [environmentLatest, airQualityLatest, printerTelemetryLatest])
"""


def durable_inventory_flux(bucket: str) -> str:
    """Reconstruct durable identities and last-known values from permanent data."""

    return f"""environmentInventory = from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(ENVIRONMENT_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(ENVIRONMENT_LATEST_FIELDS)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()

airQualityInventory = from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_AGGREGATE_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(tuple(f"{field}_mean" for field in AIR_QUALITY_FIELDS))}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()

printerTelemetryInventory = from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(PRINTER_TELEMETRY_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(PRINTER_FIELDS + AMS_FIELDS)}))
  |> group(columns: ["_measurement", "printer_id", "component_type", "component_id", "source", "_field"])
  |> last()

union(tables: [environmentInventory, airQualityInventory, printerTelemetryInventory])
"""


def readings_flux(
    bucket: str,
    query: ReadingsQuery,
    *,
    live_bucket: str | None = None,
) -> str:
    lines: list[str] = []
    streams: list[str] = []
    include_environment = query.sensor_type == SENSOR_TYPE_ENVIRONMENT or (
        query.sensor_type == SENSOR_TYPE_ALL and query.location is None
    )
    include_air_quality = query.sensor_type == SENSOR_TYPE_AIR_QUALITY or (
        query.sensor_type == SENSOR_TYPE_ALL and query.node_id is None
    )

    if include_environment:
        lines.extend(_environment_history_flux(bucket, query))
        streams.extend(("environmentMetrics", "environmentBattery"))

    if include_air_quality:
        if lines:
            lines.append("")
        air_lines, air_streams = _air_quality_history_flux(
            bucket, query, live_bucket=live_bucket or bucket
        )
        lines.extend(air_lines)
        streams.extend(air_streams)

    lines.append("")
    if len(streams) == 1:
        lines.append(streams[0])
    else:
        lines.append(f"union(tables: [{', '.join(streams)}])")
    lines.extend(['  |> yield(name: "mean")', ""])
    return "\n".join(lines)


def _environment_history_flux(bucket: str, query: ReadingsQuery) -> list[str]:
    lines = [
        'import "bitwise"',
        "",
        f"environment = from(bucket: {_flux_string(bucket)})",
        f"  |> range(start: {query.flux_start})",
        f"  |> filter(fn: (r) => r._measurement == {_flux_string(ENVIRONMENT_MEASUREMENT)})",
        f"  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(ENVIRONMENT_HISTORY_FIELDS)}))",
    ]
    if query.node_id is not None:
        lines.append(
            f"  |> filter(fn: (r) => r.node_id == {_flux_string(str(query.node_id))})"
        )

    lines.extend(
        [
            "",
            "environmentMetrics = environment",
            '  |> filter(fn: (r) => r._field == "temperature_c" or r._field == "humidity")',
            f"  |> aggregateWindow(every: {query.window_every}, fn: mean, createEmpty: false)",
            "",
            "environmentBattery = environment",
            '  |> filter(fn: (r) => r._field == "battery_mv" or r._field == "status_flags")',
            '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")',
            "  |> filter(fn: (r) =>",
            "    exists r.battery_mv and",
            "    exists r.status_flags and",
            "    bitwise.sand(a: r.status_flags, b: 4) > 0",
            "  )",
            "  |> map(fn: (r) => ({r with _value: float(v: r.battery_mv)}))",
            f"  |> aggregateWindow(every: {query.window_every}, fn: mean, createEmpty: false)",
            # aggregateWindow drops non-group-key columns added after pivot, so
            # restore _field after aggregation for the Python record parser.
            '  |> map(fn: (r) => ({r with _field: "battery_mv"}))',
        ]
    )
    return lines


def _air_quality_history_flux(
    bucket: str,
    query: ReadingsQuery,
    *,
    live_bucket: str,
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    streams: list[str] = []
    location_filter = (
        f"\n  |> filter(fn: (r) => r.location == {_flux_string(query.location)})"
        if query.location is not None
        else ""
    )

    if query.range_key == "1h":
        lines.extend(
            _raw_air_streams(
                "liveAir",
                live_bucket,
                query,
                location_filter,
            )
        )
        streams.extend(("liveAirMean", "liveAirMax"))
    else:
        aggregate_mean_fields = tuple(f"{field}_mean" for field in AIR_QUALITY_FIELDS)
        aggregate_max_fields = tuple(f"{field}_max" for field in AIR_QUALITY_MAX_FIELDS)
        aggregate_p95_fields = tuple(f"{field}_p95" for field in AIR_QUALITY_P95_FIELDS)
        lines.extend(
            [
                f"airAggregate = from(bucket: {_flux_string(bucket)})",
                f"  |> range(start: {query.flux_start})",
                f"  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_AGGREGATE_MEASUREMENT)})",
                f"  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(aggregate_mean_fields + aggregate_max_fields + aggregate_p95_fields)}))"
                + location_filter,
                "",
                "airAggregateMean = airAggregate",
                f"  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(aggregate_mean_fields)}))",
                f"  |> aggregateWindow(every: {query.window_every}, fn: mean, createEmpty: false)",
                f"  |> map(fn: (r) => ({{r with _field: {_aggregate_field_map('_mean', '')}}}))",
                "",
                "airAggregateMax = airAggregate",
                f"  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(aggregate_max_fields)}))",
                f"  |> aggregateWindow(every: {query.window_every}, fn: max, createEmpty: false)",
                f"  |> map(fn: (r) => ({{r with _field: {_aggregate_field_map('_max', '_max')}}}))",
                "",
                "airAggregateP95 = airAggregate",
                f"  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(aggregate_p95_fields)}))",
                f"  |> aggregateWindow(every: {query.window_every}, fn: max, createEmpty: false)",
                f"  |> map(fn: (r) => ({{r with _field: {_aggregate_field_map('_p95', '_p95')}}}))",
            ]
        )
        streams.extend(("airAggregateMean", "airAggregateMax", "airAggregateP95"))

    return lines, streams


def _raw_air_streams(
    prefix: str,
    bucket: str,
    query: ReadingsQuery,
    location_filter: str,
) -> list[str]:
    max_fields = AIR_QUALITY_MAX_FIELDS
    return [
        f"{prefix} = from(bucket: {_flux_string(bucket)})",
        f"  |> range(start: {query.flux_start})",
        f"  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_MEASUREMENT)})",
        f"  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(AIR_QUALITY_FIELDS)}))"
        + location_filter,
        "",
        f"{prefix}Mean = {prefix}",
        f"  |> aggregateWindow(every: {query.window_every}, fn: mean, createEmpty: false)",
        "",
        f"{prefix}Max = {prefix}",
        f"  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(max_fields)}))",
        f"  |> aggregateWindow(every: {query.window_every}, fn: max, createEmpty: false)",
        '  |> map(fn: (r) => ({r with _field: r._field + "_max"}))',
    ]


def _aggregate_field_map(remove_suffix: str, add_suffix: str) -> str:
    expression = "r._field"
    for field in reversed(AIR_QUALITY_FIELDS + AIR_QUALITY_RAW_FIELDS):
        source = f"{field}{remove_suffix}"
        destination = f"{field}{add_suffix}"
        expression = f'if r._field == "{source}" then "{destination}" else {expression}'
    return expression


def events_flux(bucket: str, query: ReadingsQuery) -> str:
    lines = [
        f"from(bucket: {_flux_string(bucket)})",
        f"  |> range(start: {query.flux_start})",
        f"  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_EVENT_MEASUREMENT)})",
    ]
    if query.location is not None:
        lines.append(
            f"  |> filter(fn: (r) => r.location == {_flux_string(query.location)})"
        )
    lines.extend(
        [
            # Event fields intentionally mix strings (for example, state) and
            # numbers (for example, peak). Keep each field in a separate Flux
            # table so mixed _value types never collide. Python orders the
            # reconstructed event objects, so no database sort is needed.
            '  |> group(columns: ["location", "topic", "event_type", "metric", "_field"])',
        ]
    )
    return "\n".join(lines) + "\n"


def air_quality_context_flux(bucket: str, live_bucket: str) -> str:
    aggregate_fields = (
        "window_start",
        "window_end",
        "sample_count",
        "valid_sample_count",
        "invalid_sample_count",
        "expected_sample_count",
        "data_coverage",
        "is_partial",
    ) + tuple(
        f"{field}_{stat}"
        for field in AIR_QUALITY_FIELDS + AIR_QUALITY_RAW_FIELDS
        for stat in ("mean", "min", "max", "p95")
    )
    return f"""import "date"

live = from(bucket: {_flux_string(live_bucket)})
  |> range(start: date.truncate(t: now(), unit: 15m))
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(AIR_QUALITY_LATEST_FIELDS)}))

aggregates = from(bucket: {_flux_string(bucket)})
  |> range(start: -25h)
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_AGGREGATE_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(aggregate_fields)}))

activeEventStates = from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_EVENT_MEASUREMENT)})
  |> filter(fn: (r) => r._field == "state")
  |> group(columns: ["location", "event_type"])
  |> last()
  |> filter(fn: (r) => r._value == "active")

union(tables: [live, aggregates, activeEventStates])
"""


def air_quality_context_response(
    records: Iterable[RecordLike],
    *,
    expected_publish_seconds: int,
    minimum_coverage_percent: int,
) -> dict[str, Any]:
    live: dict[str, dict[str, dict[str, Any]]] = {}
    aggregates: dict[str, dict[str, dict[str, Any]]] = {}
    events: dict[str, dict[str, dict[str, Any]]] = {}
    topics: dict[str, str | None] = {}
    for record in records:
        measurement = _measurement(record)
        values = _values(record)
        location = values.get("location")
        if location in (None, ""):
            continue
        location = str(location)
        topics[location] = _string_or_none(values.get("topic"))
        time_key = _time(record)
        if measurement == AIR_QUALITY_MEASUREMENT:
            target = live
        elif measurement == AIR_QUALITY_AGGREGATE_MEASUREMENT:
            target = aggregates
        elif measurement == AIR_QUALITY_EVENT_MEASUREMENT:
            target = events
        else:
            continue
        if measurement == AIR_QUALITY_EVENT_MEASUREMENT:
            event_type = str(values.get("event_type") or "")
            if not event_type:
                continue
            # Several detectors can trigger on the same sample. Keep their
            # fields separate instead of collapsing same-timestamp events.
            point_key = f"{time_key}|{event_type}"
            point = target.setdefault(location, {}).setdefault(
                point_key, {"time": time_key}
            )
            point["event_type"] = event_type
            point["metric"] = values.get("metric")
        else:
            point = target.setdefault(location, {}).setdefault(
                time_key, {"time": time_key}
            )
        point[_field(record)] = _json_value(_value(record))

    locations = sorted(set(live) | set(aggregates) | set(events))
    result: dict[str, Any] = {}
    for location in locations:
        live_points = sorted(
            live.get(location, {}).values(), key=lambda row: row["time"]
        )
        aggregate_points = sorted(
            aggregates.get(location, {}).values(), key=lambda row: row["time"]
        )
        current_summary = _current_15m_summary(
            live_points, expected_publish_seconds=expected_publish_seconds
        )
        completed = [
            point for point in aggregate_points if point.get("is_partial") is False
        ]
        previous = completed[-1] if completed else None
        previous_previous = completed[-2] if len(completed) >= 2 else None
        if current_summary and previous:
            for metric in ("voc_index", "nox_index", "co2", "pm25", "pm10"):
                current = _number_or_none(current_summary.get(f"{metric}_mean"))
                old = _number_or_none(previous.get(f"{metric}_mean"))
                if current is not None and old is not None:
                    current_summary[f"{metric}_change_from_previous_window"] = round(
                        current - old, 3
                    )
        if previous and previous_previous:
            for metric in ("voc_index", "nox_index"):
                new = _number_or_none(previous.get(f"{metric}_mean"))
                old = _number_or_none(previous_previous.get(f"{metric}_mean"))
                if new is not None and old is not None:
                    previous[f"{metric}_change_from_previous_window"] = round(
                        new - old, 3
                    )

        rolling = rolling_24h_status(
            completed,
            expected_publish_seconds=expected_publish_seconds,
            minimum_coverage_percent=float(minimum_coverage_percent),
        )
        latest_event_states: dict[str, dict[str, Any]] = {}
        for point in sorted(
            events.get(location, {}).values(), key=lambda row: row["time"]
        ):
            event_type = str(point.get("event_type") or "")
            if event_type:
                latest_event_states[event_type] = point

        result[location] = {
            "location": location,
            "topic": topics.get(location),
            "current_15m": current_summary,
            "previous_15m": previous,
            "rolling_24h": rolling,
            "active_events": [
                point
                for point in latest_event_states.values()
                if point.get("state") == "active"
            ],
        }
    return {"generated_at": _now_iso(), "locations": result}


def _current_15m_summary(
    points: Sequence[Mapping[str, Any]], *, expected_publish_seconds: int
) -> dict[str, Any] | None:
    if not points:
        return None
    latest_time = _parse_iso_time(str(points[-1].get("time", "")))
    if latest_time is None:
        return None
    start = latest_time.replace(
        minute=(latest_time.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    current = [
        point
        for point in points
        if (
            _parse_iso_time(str(point.get("time", "")))
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        >= start
    ]
    expected = max(
        1,
        int((latest_time - start).total_seconds() // expected_publish_seconds) + 1,
    )
    valid_points = [
        point
        for point in current
        if point.get("sample_valid") is True
        or (
            "sample_valid" not in point
            and all(
                _number_or_none(point.get(field)) is not None
                for field in AIR_QUALITY_FIELDS
            )
        )
    ]
    valid = len(valid_points)
    summary: dict[str, Any] = {
        "window_start": _iso_time(start),
        "window_end": _iso_time(start + timedelta(minutes=15)),
        "sample_count": len(current),
        "valid_sample_count": valid,
        "invalid_sample_count": len(current) - valid,
        "expected_sample_count": expected,
        "data_coverage": round(min(100.0, valid / expected * 100.0), 1),
        "is_partial": True,
    }
    for metric in AIR_QUALITY_FIELDS + AIR_QUALITY_RAW_FIELDS:
        values = [
            value
            for value in (_number_or_none(point.get(metric)) for point in valid_points)
            if value is not None
        ]
        if not values:
            continue
        summary[f"{metric}_mean"] = round(sum(values) / len(values), 3)
        summary[f"{metric}_min"] = round(min(values), 3)
        summary[f"{metric}_max"] = round(max(values), 3)
        if metric in AIR_QUALITY_P95_FIELDS:
            summary[f"{metric}_p95"] = round(_percentile(values, 95.0), 3)
        if metric in {"co2", "pm25", "pm10", "voc_index", "nox_index"}:
            summary[f"{metric}_trend"] = _trend(values)
    summary["voc_duration_above_150_seconds"] = expected_publish_seconds * sum(
        1
        for point in valid_points
        if (_number_or_none(point.get("voc_index")) or 0.0) >= 150.0
    )
    summary["nox_duration_above_20_seconds"] = expected_publish_seconds * sum(
        1
        for point in valid_points
        if (_number_or_none(point.get("nox_index")) or 0.0) >= 20.0
    )
    return summary


def _trend(values: Sequence[float]) -> str:
    if len(values) < 2:
        return "insufficient"
    delta = values[-1] - values[0]
    tolerance = max(1.0, abs(values[0]) * 0.05)
    if delta > tolerance:
        return "rising"
    if delta < -tolerance:
        return "falling"
    return "stable"


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def latest_response(
    records: Iterable[RecordLike],
    *,
    include_internal: bool = False,
) -> dict[str, Any]:
    entities: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        measurement = _measurement(record)
        sensor_type = _sensor_type_for_measurement(measurement, _values(record))
        if sensor_type is None:
            continue

        identity = _entity_identity(record, sensor_type)
        if identity is None:
            continue

        key = (sensor_type, identity)
        item = entities.setdefault(key, _base_entity(record, sensor_type, identity))
        record_time = _time(record)
        _update_entity_metadata(item, record, sensor_type, record_time)
        field = _latest_field(record, sensor_type)
        if field is None:
            continue
        field_times = item.setdefault("_field_times", {})
        previous_time = field_times.get(field)
        if (
            previous_time is None
            or _max_iso_time(previous_time, record_time) == record_time
        ):
            item[field] = _json_value(_value(record))
            field_times[field] = record_time
        item["last_seen"] = _max_iso_time(item.get("last_seen"), record_time)

    if not include_internal:
        for item in entities.values():
            _finalize_latest_item(item)

    return {
        "generated_at": _now_iso(),
        "environment": _sorted_entities(
            item
            for (sensor_type, _), item in entities.items()
            if sensor_type == SENSOR_TYPE_ENVIRONMENT
        ),
        "air_quality": _sorted_entities(
            item
            for (sensor_type, _), item in entities.items()
            if sensor_type == SENSOR_TYPE_AIR_QUALITY
        ),
        "printer": _sorted_entities(
            item
            for (sensor_type, _), item in entities.items()
            if sensor_type == SENSOR_TYPE_PRINTER
        ),
        "ams": _sorted_entities(
            item
            for (sensor_type, _), item in entities.items()
            if sensor_type == SENSOR_TYPE_AMS
        ),
    }


def merge_latest_payloads(
    durable: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    generated_at: str | None = None,
    include_internal: bool = False,
) -> dict[str, Any]:
    """Merge inventories using per-field time when internal state is available."""

    response: dict[str, Any] = {
        "generated_at": generated_at
        or str(
            observed.get("generated_at") or durable.get("generated_at") or _now_iso()
        )
    }
    for sensor_type in (
        SENSOR_TYPE_ENVIRONMENT,
        SENSOR_TYPE_AIR_QUALITY,
        SENSOR_TYPE_PRINTER,
        SENSOR_TYPE_AMS,
    ):
        entities: dict[str, dict[str, Any]] = {}
        for source in (durable, observed):
            for original in source.get(sensor_type, []):
                candidate = deepcopy(dict(original))
                identity = str(candidate.get("id") or "")
                if not identity:
                    continue
                existing = entities.get(identity)
                if existing is None:
                    entities[identity] = candidate
                else:
                    entities[identity] = _merge_latest_entities(existing, candidate)
        if not include_internal:
            for item in entities.values():
                if "_field_times" in item:
                    _finalize_latest_item(item)
        response[sensor_type] = _sorted_entities(entities.values())
    return response


def _empty_latest_payload() -> dict[str, Any]:
    return {
        "generated_at": _now_iso(),
        "environment": [],
        "air_quality": [],
        "printer": [],
        "ams": [],
    }


def _merge_latest_entities(
    left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any]:
    if "_field_times" not in left or "_field_times" not in right:
        return _newest_entity(left, right)

    result = deepcopy(left)
    result_field_times = result.setdefault("_field_times", {})
    for field, right_time in right["_field_times"].items():
        left_time = result_field_times.get(field)
        if left_time is None or _max_iso_time(left_time, right_time) == right_time:
            if field in right:
                result[field] = deepcopy(right[field])
            else:
                result.pop(field, None)
            result_field_times[field] = right_time

    result_metadata_times = result.setdefault("_metadata_times", {})
    for key, right_time in right.get("_metadata_times", {}).items():
        left_time = result_metadata_times.get(key)
        if (
            left_time is None or _max_iso_time(left_time, right_time) == right_time
        ) and right.get(key) not in (None, ""):
            result[key] = deepcopy(right[key])
            result_metadata_times[key] = right_time

    result["last_seen"] = _max_iso_time(
        result.get("last_seen"), str(right.get("last_seen") or "")
    )
    for key in (
        "node_id",
        "location",
        "printer_id",
        "ams_id",
        "component_id",
        "source_id",
        "label",
        "topic",
        "source",
        "sensor_type",
        "id",
    ):
        if result.get(key) in (None, "") and right.get(key) not in (None, ""):
            result[key] = deepcopy(right[key])
    return result


def _newest_entity(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_seen = str(left.get("last_seen") or "")
    right_seen = str(right.get("last_seen") or "")
    newest = right if _max_iso_time(left_seen, right_seen) == right_seen else left
    other = left if newest is right else right
    result = deepcopy(newest)
    for key in (
        "node_id",
        "location",
        "printer_id",
        "ams_id",
        "component_id",
        "source_id",
        "label",
        "topic",
        "source",
        "sensor_type",
        "id",
    ):
        if result.get(key) in (None, "") and other.get(key) not in (None, ""):
            result[key] = deepcopy(other[key])
    return result


def readings_response(
    records: Iterable[RecordLike],
    query: ReadingsQuery,
    *,
    event_records: Iterable[RecordLike] = (),
) -> dict[str, Any]:
    series: dict[tuple[str, str], dict[str, Any]] = {}
    points: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}

    for record in records:
        measurement = _measurement(record)
        sensor_type = _sensor_type_for_measurement(measurement, _values(record))
        if sensor_type is None:
            continue

        identity = _entity_identity(record, sensor_type)
        if identity is None:
            continue

        key = (sensor_type, identity)
        series.setdefault(key, _base_entity(record, sensor_type, identity))
        time_key = _time(record)
        point = points.setdefault(key, {}).setdefault(time_key, {"time": time_key})
        point[_field(record)] = _json_value(_value(record))

    response_series = []
    for key, item in series.items():
        item = dict(item)
        item["points"] = sorted(
            points.get(key, {}).values(), key=lambda row: row["time"]
        )
        response_series.append(item)

    return {
        "generated_at": _now_iso(),
        "range": query.range_key,
        "window": query.window_every,
        "sensor_type": query.sensor_type,
        "data_tier": "live_1m" if query.range_key == "1h" else "15m_aggregate",
        "series": _sorted_entities(response_series),
        "events": _events_response(event_records),
    }


def _events_response(records: Iterable[RecordLike]) -> list[dict[str, Any]]:
    events: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        values = _values(record)
        location = str(values.get("location") or "")
        event_type = str(values.get("event_type") or "")
        time_key = _time(record)
        if not location or not event_type:
            continue
        key = (location, event_type, time_key)
        item = events.setdefault(
            key,
            {
                "location": location,
                "event_type": event_type,
                "metric": values.get("metric"),
                "time": time_key,
            },
        )
        item[_field(record)] = _json_value(_value(record))
    completed: list[dict[str, Any]] = []
    latest_active: dict[tuple[str, str], dict[str, Any]] = {}
    for item in sorted(events.values(), key=lambda row: str(row.get("time"))):
        if item.get("state") == "active":
            # The detector invariant permits only one active episode for a
            # location/event type. Retain the newest snapshot if an earlier
            # restart left orphaned active records behind.
            latest_active[(str(item["location"]), str(item["event_type"]))] = item
        else:
            completed.append(item)
    return sorted(
        completed + list(latest_active.values()),
        key=lambda item: str(item.get("time")),
    )


def nodes_response(
    latest: Mapping[str, Any],
    *,
    stale_after_seconds: int,
    air_quality_stale_after_seconds: int | None = None,
) -> dict[str, Any]:
    generated_at = str(latest.get("generated_at") or _now_iso())
    now = _parse_iso_time(generated_at) or datetime.now(timezone.utc)
    nodes = []

    for item in latest.get("environment", []):
        nodes.append(_node_status(dict(item), now, stale_after_seconds))
    for item in latest.get("air_quality", []):
        nodes.append(
            _node_status(
                dict(item),
                now,
                air_quality_stale_after_seconds or stale_after_seconds,
            )
        )
    for sensor_type in (SENSOR_TYPE_PRINTER, SENSOR_TYPE_AMS):
        for item in latest.get(sensor_type, []):
            nodes.append(_node_status(dict(item), now, stale_after_seconds))

    return {
        "generated_at": generated_at,
        "stale_after_seconds": stale_after_seconds,
        "air_quality_stale_after_seconds": (
            air_quality_stale_after_seconds or stale_after_seconds
        ),
        "nodes": _sorted_entities(nodes),
    }


def air_quality_sensor_status(
    latest: Mapping[str, Any],
    *,
    location: str,
    stale_after_seconds: int,
    observed_at: datetime,
) -> dict[str, Any]:
    """Resolve one required station with the dashboard's canonical freshness rule."""

    snapshot = dict(latest)
    snapshot["generated_at"] = _iso_time(observed_at)
    nodes = nodes_response(
        snapshot,
        stale_after_seconds=stale_after_seconds,
        air_quality_stale_after_seconds=stale_after_seconds,
    )["nodes"]
    for node in nodes:
        if (
            node.get("sensor_type") == SENSOR_TYPE_AIR_QUALITY
            and str(node.get("location") or node.get("id")) == location
        ):
            return node
    return {
        "id": location,
        "location": location,
        "sensor_type": SENSOR_TYPE_AIR_QUALITY,
        "status": "unknown",
        "last_seen": None,
        "age_seconds": None,
        "stale_reason": "never_seen",
    }


def latest_with_node_status(
    latest: Mapping[str, Any],
    *,
    stale_after_seconds: int,
    air_quality_stale_after_seconds: int | None = None,
) -> dict[str, Any]:
    """Attach node status derived from an existing latest-value snapshot."""

    response = dict(latest)
    node_payload = nodes_response(
        latest,
        stale_after_seconds=stale_after_seconds,
        air_quality_stale_after_seconds=air_quality_stale_after_seconds,
    )
    response["stale_after_seconds"] = stale_after_seconds
    response["air_quality_stale_after_seconds"] = node_payload[
        "air_quality_stale_after_seconds"
    ]
    response["nodes"] = node_payload["nodes"]
    status_by_identity = {
        (str(node.get("sensor_type")), str(node.get("id"))): node
        for node in node_payload["nodes"]
    }
    for sensor_type in (
        SENSOR_TYPE_ENVIRONMENT,
        SENSOR_TYPE_AIR_QUALITY,
        SENSOR_TYPE_PRINTER,
        SENSOR_TYPE_AMS,
    ):
        enriched = []
        for original in response.get(sensor_type, []):
            item = dict(original)
            node = status_by_identity.get((sensor_type, str(item.get("id"))))
            if node is not None:
                item["status"] = node.get("status")
                item["age_seconds"] = node.get("age_seconds")
                item["stale_reason"] = node.get("stale_reason")
                item["values_are_current"] = node.get("status") == "online"
            enriched.append(item)
        response[sensor_type] = enriched
    return response


def latest_with_air_quality_context(
    latest: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    stale_after_seconds: int,
) -> dict[str, Any]:
    """Attach source-backed interpretations and aggregate context to latest data."""

    response = dict(latest)
    locations = context.get("locations", {})
    stations = []
    for original in latest.get("air_quality", []):
        station = dict(original)
        location = str(station.get("location") or station.get("id") or "")
        station_context = (
            locations.get(location, {}) if isinstance(locations, Mapping) else {}
        )
        current_15m = station_context.get("current_15m")
        rolling_24h = station_context.get("rolling_24h")
        station["summary_15m"] = current_15m
        station["previous_15m"] = station_context.get("previous_15m")
        station["rolling_24h"] = rolling_24h
        station["active_events"] = station_context.get("active_events", [])
        station["interpretations"] = interpret_station(
            station,
            summary_15m=current_15m if isinstance(current_15m, Mapping) else None,
            rolling_24h=rolling_24h if isinstance(rolling_24h, Mapping) else None,
            stale_after_seconds=stale_after_seconds,
        )
        station["overall_status"] = _overall_air_quality_status(
            station["interpretations"]
        )
        stations.append(station)
    response["air_quality"] = stations
    response["air_quality_stale_after_seconds"] = stale_after_seconds
    return response


def _overall_air_quality_status(
    interpretations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    severity_rank = {
        "excellent": 0,
        "good": 1,
        "informational": 1,
        "moderate": 2,
        "poor": 3,
        "very_poor": 4,
        "hazardous": 5,
    }
    pollutant_keys = (
        "co2",
        "co2_occupational",
        "pm25_current",
        "pm10_current",
        "voc_index",
        "nox_index",
    )
    candidates = []
    for key in pollutant_keys:
        item = interpretations.get(key, {})
        severity = str(item.get("severity") or "unavailable")
        if key == "co2_occupational" and severity == "informational":
            continue
        if severity not in severity_rank or item.get("is_stale"):
            continue
        candidates.append(
            (severity_rank[severity], key, severity, item.get("category"))
        )
    if not candidates:
        return {
            "severity": "unavailable",
            "driving_metric": None,
            "category": "No valid current pollutant status",
            "framework": "Transparent worst-current-pollutant dashboard summary",
            "is_official": False,
        }
    _, key, severity, category = max(candidates)
    return {
        "severity": severity,
        "driving_metric": key,
        "category": category,
        "framework": "Transparent worst-current-pollutant dashboard summary",
        "is_official": False,
    }


def _node_status(
    item: dict[str, Any],
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, Any]:
    last_seen = _parse_iso_time(str(item.get("last_seen", "")))
    if last_seen is None:
        status = "unknown"
        age_seconds = None
    else:
        age_seconds = max(0, int((now - last_seen).total_seconds()))
        if age_seconds <= stale_after_seconds:
            status = "online"
        elif age_seconds <= stale_after_seconds * 4:
            status = "stale"
        else:
            status = "offline"
    if item.get("sensor_type") == SENSOR_TYPE_PRINTER and item.get("online") is False:
        status = "offline"

    result = {
        "id": item.get("id"),
        "sensor_type": item.get("sensor_type"),
        "topic": item.get("topic"),
        "last_seen": item.get("last_seen"),
        "age_seconds": age_seconds,
        "status": status,
    }

    for key in (
        "node_id",
        "location",
        "printer_id",
        "ams_id",
        "component_id",
        "source_id",
        "label",
        "battery_mv",
        "status_flags",
        "battery_measurement_ok",
        "battery_low",
        "battery_shutdown",
        "sequence",
        "available_fields",
        "online",
    ):
        if key in item:
            result[key] = item[key]

    if status in {"stale", "offline"}:
        result["stale_reason"] = (
            "battery_shutdown"
            if item.get("battery_shutdown") is True
            else "no_recent_reading"
        )
    else:
        result["stale_reason"] = None

    return result


def _finalize_latest_item(item: dict[str, Any]) -> None:
    """Add battery semantics without attaching an older flag to a newer packet."""

    field_times = item.pop("_field_times", {})
    item.pop("_metadata_times", None)
    supported_fields = {
        SENSOR_TYPE_ENVIRONMENT: ENVIRONMENT_HISTORY_FIELDS,
        SENSOR_TYPE_AIR_QUALITY: AIR_QUALITY_FIELDS,
        SENSOR_TYPE_PRINTER: PRINTER_FIELDS,
        SENSOR_TYPE_AMS: AMS_FIELDS,
    }.get(str(item.get("sensor_type")), ())
    item["available_fields"] = [
        field for field in supported_fields if field in field_times
    ]
    if item.get("sensor_type") == SENSOR_TYPE_AIR_QUALITY:
        sample_valid = (
            item.get("sample_valid")
            if _field_is_current(item, field_times, "sample_valid")
            else None
        )
        item["sample_valid"] = sample_valid
        if sample_valid is False:
            for field in AIR_QUALITY_FIELDS + AIR_QUALITY_RAW_FIELDS:
                item[field] = None
        else:
            for field in AIR_QUALITY_FIELDS + AIR_QUALITY_RAW_FIELDS:
                if field in item and not _field_is_current(item, field_times, field):
                    item[field] = None
        return
    if item.get("sensor_type") != SENSOR_TYPE_ENVIRONMENT:
        return

    status_flags: int | None = None
    if _field_is_current(item, field_times, "status_flags"):
        candidate = item.get("status_flags")
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and 0 <= candidate <= 4_294_967_295
        ):
            status_flags = candidate

    item["status_flags"] = status_flags
    battery_status = decode_battery_status(status_flags)
    item.update(battery_status)

    battery_is_current = _field_is_current(item, field_times, "battery_mv")
    if not battery_is_current or battery_status["battery_measurement_ok"] is not True:
        item["battery_mv"] = None


def _field_is_current(
    item: Mapping[str, Any],
    field_times: Mapping[str, Any],
    field: str,
) -> bool:
    field_time = field_times.get(field)
    last_seen = item.get("last_seen")
    if field_time is None or last_seen is None:
        return False

    field_dt = _parse_iso_time(str(field_time))
    last_seen_dt = _parse_iso_time(str(last_seen))
    if field_dt is not None and last_seen_dt is not None:
        return field_dt == last_seen_dt
    return str(field_time) == str(last_seen)


def _base_entity(record: RecordLike, sensor_type: str, identity: str) -> dict[str, Any]:
    values = _values(record)
    item: dict[str, Any] = {
        "id": identity,
        "sensor_type": sensor_type,
        "topic": values.get("topic"),
    }
    if sensor_type == SENSOR_TYPE_ENVIRONMENT:
        item["node_id"] = _int_or_string(identity)
    elif sensor_type == SENSOR_TYPE_AIR_QUALITY:
        item["location"] = identity
        node_id = values.get("node_id")
        if node_id not in (None, ""):
            item["node_id"] = _int_or_string(str(node_id))
    else:
        printer_id = str(values.get("printer_id") or "")
        component_id = str(values.get("component_id") or "")
        item["printer_id"] = printer_id
        item["component_id"] = component_id
        item["source_id"] = identity
        if sensor_type == SENSOR_TYPE_AMS:
            item["ams_id"] = component_id
            item["label"] = f"{printer_id} · {component_id}"
        else:
            item["label"] = f"Printer · {printer_id}"
    return item


def _update_entity_metadata(
    item: dict[str, Any],
    record: RecordLike,
    sensor_type: str,
    record_time: str,
) -> None:
    """Prefer the newest non-empty metadata when legacy series share an identity."""

    values = _values(record)
    metadata_times = item.setdefault("_metadata_times", {})
    keys = (
        ("topic",)
        if sensor_type == SENSOR_TYPE_ENVIRONMENT
        else ("topic", "node_id")
        if sensor_type == SENSOR_TYPE_AIR_QUALITY
        else ("source", "printer_id", "component_id")
    )
    for key in keys:
        value = values.get(key)
        if value in (None, ""):
            continue
        previous_time = metadata_times.get(key)
        if (
            previous_time is None
            or _max_iso_time(previous_time, record_time) == record_time
        ):
            item[key] = _int_or_string(str(value)) if key == "node_id" else str(value)
            metadata_times[key] = record_time


def _sensor_type_for_measurement(
    measurement: str, values: Mapping[str, Any] | None = None
) -> str | None:
    if measurement == ENVIRONMENT_MEASUREMENT:
        return SENSOR_TYPE_ENVIRONMENT
    if measurement in {AIR_QUALITY_MEASUREMENT, AIR_QUALITY_AGGREGATE_MEASUREMENT}:
        return SENSOR_TYPE_AIR_QUALITY
    if measurement == PRINTER_TELEMETRY_MEASUREMENT:
        component_type = str((values or {}).get("component_type") or "")
        if component_type in {SENSOR_TYPE_PRINTER, SENSOR_TYPE_AMS}:
            return component_type
    return None


def _latest_field(record: RecordLike, sensor_type: str) -> str | None:
    """Normalize permanent aggregate capabilities to their live field names."""

    field = _field(record)
    if (
        sensor_type == SENSOR_TYPE_AIR_QUALITY
        and _measurement(record) == AIR_QUALITY_AGGREGATE_MEASUREMENT
        and field.endswith("_mean")
    ):
        candidate = field[: -len("_mean")]
        return candidate if candidate in AIR_QUALITY_FIELDS else None
    return field


def _entity_identity(record: RecordLike, sensor_type: str) -> str | None:
    values = _values(record)
    if sensor_type == SENSOR_TYPE_ENVIRONMENT:
        node_id = values.get("node_id")
        return str(node_id) if node_id not in (None, "") else None
    if sensor_type == SENSOR_TYPE_AIR_QUALITY:
        location = values.get("location")
        return str(location) if location not in (None, "") else None
    printer_id = values.get("printer_id")
    if printer_id in (None, ""):
        return None
    if sensor_type == SENSOR_TYPE_PRINTER:
        return str(printer_id)
    component_id = values.get("component_id")
    if component_id in (None, ""):
        return None
    return f"{printer_id}/{component_id}"


def _values(record: RecordLike) -> Mapping[str, Any]:
    return getattr(record, "values", {})


def _measurement(record: RecordLike) -> str:
    if hasattr(record, "get_measurement"):
        return str(record.get_measurement())
    return str(_values(record).get("_measurement", ""))


def _field(record: RecordLike) -> str:
    if hasattr(record, "get_field"):
        return str(record.get_field())
    return str(_values(record).get("_field", ""))


def _value(record: RecordLike) -> Any:
    if hasattr(record, "get_value"):
        return record.get_value()
    return _values(record).get("_value")


def _time(record: RecordLike) -> str:
    value: Any
    if hasattr(record, "get_time"):
        value = record.get_time()
    else:
        value = _values(record).get("_time")

    if isinstance(value, datetime):
        return _iso_time(value)
    return str(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso_time(value)
    return value


def _max_iso_time(left: Any, right: str) -> str:
    if not left:
        return right
    left_dt = _parse_iso_time(str(left))
    right_dt = _parse_iso_time(right)
    if left_dt is None or right_dt is None:
        return str(max(str(left), right))
    return _iso_time(max(left_dt, right_dt))


def _parse_iso_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return _iso_time(datetime.now(timezone.utc))


def _sorted_entities(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items, key=lambda item: (str(item.get("sensor_type")), str(item.get("id")))
    )


def _optional_node_id(value: str) -> int | None:
    if not value:
        return None
    try:
        node_id = int(value)
    except ValueError as exc:
        raise QueryValidationError("node_id must be an integer") from exc
    if node_id < 1:
        raise QueryValidationError("node_id must be >= 1")
    return node_id


def _optional_location(value: str) -> str | None:
    if not value:
        return None
    if not LOCATION_RE.fullmatch(value):
        raise QueryValidationError("location must be a stable slug")
    return value


def _param(params: Mapping[str, str | None], name: str, default: str) -> str:
    value = params.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def _int_or_string(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


def _string_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _flux_string(value: str) -> str:
    return json.dumps(value)


def _flux_array(values: Iterable[str]) -> str:
    return "[" + ", ".join(_flux_string(value) for value in values) + "]"
