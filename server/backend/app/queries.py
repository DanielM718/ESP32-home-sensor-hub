"""InfluxDB query helpers for the Flask REST API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol

if TYPE_CHECKING:
    from app.config import InfluxSettings


SUPPORTED_RANGES: dict[str, tuple[str, str]] = {
    "1h": ("-1h", "1m"),
    "24h": ("-24h", "10m"),
    "7d": ("-7d", "1h"),
    "30d": ("-30d", "6h"),
}
DEFAULT_RANGE = "24h"
LATEST_LOOKBACK = "-30d"
SENSOR_TYPE_ALL = "all"
SENSOR_TYPE_ENVIRONMENT = "environment"
SENSOR_TYPE_AIR_QUALITY = "air_quality"
SENSOR_TYPES = {
    SENSOR_TYPE_ALL,
    SENSOR_TYPE_ENVIRONMENT,
    SENSOR_TYPE_AIR_QUALITY,
}

ENVIRONMENT_MEASUREMENT = "environment_reading"
AIR_QUALITY_MEASUREMENT = "air_quality_reading"

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

LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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

    def get_field(self) -> str:
        ...

    def get_measurement(self) -> str:
        ...

    def get_time(self) -> datetime:
        ...

    def get_value(self) -> Any:
        ...


class InfluxReadRepository:
    """Read repository used by the Flask API."""

    def __init__(self, settings: "InfluxSettings") -> None:
        from influxdb_client import InfluxDBClient

        token = settings.read_token or settings.write_token
        self._settings = settings
        self._client = InfluxDBClient(
            url=settings.url,
            token=token,
            org=settings.org,
        )
        self._query_api = self._client.query_api()

    def latest(self) -> dict[str, Any]:
        records = self._query(latest_flux(self._settings.bucket))
        return latest_response(records)

    def readings(self, query: ReadingsQuery) -> dict[str, Any]:
        records = self._query(readings_flux(self._settings.bucket, query))
        return readings_response(records, query)

    def nodes(self, *, stale_after_seconds: int) -> dict[str, Any]:
        latest = self.latest()
        return nodes_response(latest, stale_after_seconds=stale_after_seconds)

    def close(self) -> None:
        self._client.close()

    def _query(self, flux: str) -> list[RecordLike]:
        tables = self._query_api.query(query=flux, org=self._settings.org)
        return [record for table in tables for record in table.records]


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


def latest_flux(bucket: str) -> str:
    fields = ENVIRONMENT_LATEST_FIELDS + AIR_QUALITY_FIELDS
    return f"""from(bucket: {_flux_string(bucket)})
  |> range(start: {LATEST_LOOKBACK})
  |> filter(fn: (r) => r._measurement == {_flux_string(ENVIRONMENT_MEASUREMENT)} or r._measurement == {_flux_string(AIR_QUALITY_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(fields)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()
"""


def readings_flux(bucket: str, query: ReadingsQuery) -> str:
    fields = _history_fields(query.sensor_type)
    filters = [
        f"r._measurement == {_flux_string(measurement)}"
        for measurement in _measurements(query.sensor_type)
    ]
    lines = [
        f"from(bucket: {_flux_string(bucket)})",
        f"  |> range(start: {query.flux_start})",
        f"  |> filter(fn: (r) => {' or '.join(filters)})",
        f"  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(fields)}))",
    ]

    if query.node_id is not None:
        lines.append(f"  |> filter(fn: (r) => r.node_id == {_flux_string(str(query.node_id))})")
    if query.location is not None:
        lines.append(f"  |> filter(fn: (r) => r.location == {_flux_string(query.location)})")

    lines.extend(
        [
            f"  |> aggregateWindow(every: {query.window_every}, fn: mean, createEmpty: false)",
            '  |> yield(name: "mean")',
            "",
        ]
    )
    return "\n".join(lines)


def latest_response(records: Iterable[RecordLike]) -> dict[str, Any]:
    entities: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        measurement = _measurement(record)
        sensor_type = _sensor_type_for_measurement(measurement)
        if sensor_type is None:
            continue

        identity = _entity_identity(record, sensor_type)
        if identity is None:
            continue

        key = (sensor_type, identity)
        item = entities.setdefault(key, _base_entity(record, sensor_type, identity))
        item[_field(record)] = _json_value(_value(record))
        item["last_seen"] = _max_iso_time(item.get("last_seen"), _time(record))

    return {
        "generated_at": _now_iso(),
        "environment": _sorted_entities(
            item for (sensor_type, _), item in entities.items()
            if sensor_type == SENSOR_TYPE_ENVIRONMENT
        ),
        "air_quality": _sorted_entities(
            item for (sensor_type, _), item in entities.items()
            if sensor_type == SENSOR_TYPE_AIR_QUALITY
        ),
    }


def readings_response(
    records: Iterable[RecordLike],
    query: ReadingsQuery,
) -> dict[str, Any]:
    series: dict[tuple[str, str], dict[str, Any]] = {}
    points: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}

    for record in records:
        measurement = _measurement(record)
        sensor_type = _sensor_type_for_measurement(measurement)
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
        item["points"] = sorted(points.get(key, {}).values(), key=lambda row: row["time"])
        response_series.append(item)

    return {
        "generated_at": _now_iso(),
        "range": query.range_key,
        "window": query.window_every,
        "sensor_type": query.sensor_type,
        "series": _sorted_entities(response_series),
    }


def nodes_response(
    latest: Mapping[str, Any],
    *,
    stale_after_seconds: int,
) -> dict[str, Any]:
    generated_at = str(latest.get("generated_at") or _now_iso())
    now = _parse_iso_time(generated_at) or datetime.now(timezone.utc)
    nodes = []

    for item in latest.get("environment", []):
        nodes.append(_node_status(dict(item), now, stale_after_seconds))
    for item in latest.get("air_quality", []):
        nodes.append(_node_status(dict(item), now, stale_after_seconds))

    return {
        "generated_at": generated_at,
        "stale_after_seconds": stale_after_seconds,
        "nodes": _sorted_entities(nodes),
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
        status = "online" if age_seconds <= stale_after_seconds else "stale"

    result = {
        "id": item.get("id"),
        "sensor_type": item.get("sensor_type"),
        "topic": item.get("topic"),
        "last_seen": item.get("last_seen"),
        "age_seconds": age_seconds,
        "status": status,
    }

    for key in ("node_id", "location", "battery_mv", "status_flags", "sequence"):
        if key in item:
            result[key] = item[key]

    return result


def _base_entity(record: RecordLike, sensor_type: str, identity: str) -> dict[str, Any]:
    values = _values(record)
    item: dict[str, Any] = {
        "id": identity,
        "sensor_type": sensor_type,
        "topic": values.get("topic"),
    }
    if sensor_type == SENSOR_TYPE_ENVIRONMENT:
        item["node_id"] = _int_or_string(identity)
    else:
        item["location"] = identity
    return item


def _measurements(sensor_type: str) -> tuple[str, ...]:
    if sensor_type == SENSOR_TYPE_ENVIRONMENT:
        return (ENVIRONMENT_MEASUREMENT,)
    if sensor_type == SENSOR_TYPE_AIR_QUALITY:
        return (AIR_QUALITY_MEASUREMENT,)
    return (ENVIRONMENT_MEASUREMENT, AIR_QUALITY_MEASUREMENT)


def _history_fields(sensor_type: str) -> tuple[str, ...]:
    if sensor_type == SENSOR_TYPE_ENVIRONMENT:
        return ENVIRONMENT_HISTORY_FIELDS
    if sensor_type == SENSOR_TYPE_AIR_QUALITY:
        return AIR_QUALITY_FIELDS
    return ENVIRONMENT_HISTORY_FIELDS + AIR_QUALITY_FIELDS


def _sensor_type_for_measurement(measurement: str) -> str | None:
    if measurement == ENVIRONMENT_MEASUREMENT:
        return SENSOR_TYPE_ENVIRONMENT
    if measurement == AIR_QUALITY_MEASUREMENT:
        return SENSOR_TYPE_AIR_QUALITY
    return None


def _entity_identity(record: RecordLike, sensor_type: str) -> str | None:
    values = _values(record)
    if sensor_type == SENSOR_TYPE_ENVIRONMENT:
        node_id = values.get("node_id")
        return str(node_id) if node_id not in (None, "") else None
    location = values.get("location")
    return str(location) if location not in (None, "") else None


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
    return sorted(items, key=lambda item: (str(item.get("sensor_type")), str(item.get("id"))))


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


def _flux_string(value: str) -> str:
    return json.dumps(value)


def _flux_array(values: Iterable[str]) -> str:
    return "[" + ", ".join(_flux_string(value) for value in values) + "]"
