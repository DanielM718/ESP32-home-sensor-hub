"""Shared validation and presentation definitions for monitoring and exports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID


SENSOR_TYPE_ENVIRONMENT = "environment"
SENSOR_TYPE_AIR_QUALITY = "air_quality"
SENSOR_TYPES = (SENSOR_TYPE_ENVIRONMENT, SENSOR_TYPE_AIR_QUALITY)

ENVIRONMENT_FIELDS = ("temperature_c", "humidity", "battery_mv")
AIR_QUALITY_FIELDS = (
    "temperature_c",
    "humidity",
    "co2",
    "pm1",
    "pm25",
    "pm4",
    "pm10",
    "voc_index",
    "nox_index",
)
SUPPORTED_FIELDS = tuple(dict.fromkeys(ENVIRONMENT_FIELDS + AIR_QUALITY_FIELDS))

FIELD_UNITS = {
    "temperature_c": "degC",
    "humidity": "percent",
    "battery_mv": "mV",
    "co2": "ppm",
    "pm1": "ug/m3",
    "pm25": "ug/m3",
    "pm4": "ug/m3",
    "pm10": "ug/m3",
    "voc_index": "index",
    "nox_index": "index",
}

RESOLUTIONS = ("raw", "15m")
CSV_FORMATS = ("long", "wide")
SESSION_STATUSES = ("running", "completed", "stopped")
EXPORT_STATUSES = (
    "queued",
    "running",
    "completed",
    "failed",
    "cancel_requested",
    "cancelled",
)
FINAL_EXPORT_STATUSES = ("completed", "failed", "cancelled")

NAME_MAX_LENGTH = 120
NOTES_MAX_LENGTH = 2000
MAX_SOURCES = 100
MIN_MONITORING_SECONDS = 10
DEFAULT_RAW_RETENTION_SECONDS = 72 * 60 * 60
LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class WorkflowValidationError(ValueError):
    """Raised when monitoring/export input violates the public contract."""


class WorkflowNotFoundError(LookupError):
    """Raised when a requested persistent object does not exist."""


class WorkflowConflictError(RuntimeError):
    """Raised for a valid request that conflicts with persistent state."""


@dataclass(frozen=True)
class Source:
    sensor_type: str
    node_id: int | None = None
    location: str | None = None

    @property
    def source_id(self) -> str:
        if self.sensor_type == SENSOR_TYPE_ENVIRONMENT:
            return str(self.node_id)
        return str(self.location)

    @property
    def key(self) -> tuple[str, str]:
        return self.sensor_type, self.source_id

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"sensor_type": self.sensor_type}
        if self.sensor_type == SENSOR_TYPE_ENVIRONMENT:
            result["node_id"] = self.node_id
        else:
            result["location"] = self.location
        return result


@dataclass(frozen=True)
class ExportRequest:
    name: str
    start_time: datetime
    end_time: datetime
    sources: tuple[Source, ...]
    fields: tuple[str, ...]
    resolution: str
    csv_format: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitoringRequest:
    name: str
    notes: str
    duration_seconds: int
    sources: tuple[Source, ...]
    fields: tuple[str, ...]
    resolution: str
    csv_format: str


def validate_monitoring_request(
    payload: Any,
    *,
    max_duration_seconds: int = DEFAULT_RAW_RETENTION_SECONDS,
) -> MonitoringRequest:
    data = _object(payload)
    name = validate_name(data.get("name"))
    notes = validate_notes(data.get("notes"))
    duration = data.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise WorkflowValidationError("duration_seconds must be an integer")
    if duration < MIN_MONITORING_SECONDS:
        raise WorkflowValidationError(
            f"duration_seconds must be at least {MIN_MONITORING_SECONDS}"
        )
    if duration > max_duration_seconds:
        raise WorkflowValidationError(
            f"duration_seconds must not exceed {max_duration_seconds} (raw retention)"
        )
    sources = validate_sources(data.get("sources"))
    fields = validate_fields(data.get("fields"))
    resolution = _choice(data.get("resolution", "raw"), "resolution", RESOLUTIONS)
    if resolution != "raw":
        raise WorkflowValidationError(
            "active monitoring currently supports raw resolution only"
        )
    csv_format = _choice(data.get("csv_format", "long"), "csv_format", CSV_FORMATS)
    _validate_supported_selection(sources, fields, resolution)
    return MonitoringRequest(
        name=name,
        notes=notes,
        duration_seconds=duration,
        sources=sources,
        fields=fields,
        resolution=resolution,
        csv_format=csv_format,
    )


def validate_export_request(
    payload: Any,
    *,
    now: datetime | None = None,
    raw_retention_seconds: int = DEFAULT_RAW_RETENTION_SECONDS,
) -> ExportRequest:
    data = _object(payload)
    name = validate_name(data.get("name"))
    start = parse_client_time(data.get("start_time"), "start_time")
    end = parse_client_time(data.get("end_time"), "end_time")
    if end == start:
        raise WorkflowValidationError("end_time must be later than start_time")
    if end < start:
        raise WorkflowValidationError("end_time must be later than start_time")
    sources = validate_sources(data.get("sources"))
    fields = validate_fields(data.get("fields"))
    resolution = _choice(data.get("resolution", "raw"), "resolution", RESOLUTIONS)
    csv_format = _choice(data.get("csv_format", "long"), "csv_format", CSV_FORMATS)
    _validate_supported_selection(sources, fields, resolution)

    warnings: list[str] = []
    if resolution == "15m":
        unsupported = [
            source.source_id
            for source in sources
            if source.sensor_type == SENSOR_TYPE_ENVIRONMENT
        ]
        if unsupported:
            warnings.append(
                "Environment nodes do not have a stored 15-minute aggregate tier and "
                "will report zero rows: " + ", ".join(unsupported)
            )
    if resolution == "raw" and any(
        source.sensor_type == SENSOR_TYPE_AIR_QUALITY for source in sources
    ):
        reference = _aware_utc(now or datetime.now(timezone.utc))
        retained_after = reference.timestamp() - raw_retention_seconds
        if start.timestamp() < retained_after:
            boundary = datetime.fromtimestamp(retained_after, tz=timezone.utc)
            warnings.append(
                "Raw air-quality data is retained for approximately "
                f"{raw_retention_seconds // 3600} hours; data before {iso_utc(boundary)} "
                "may have expired. Aggregates are not substituted."
            )
    return ExportRequest(
        name=name,
        start_time=start,
        end_time=end,
        sources=sources,
        fields=fields,
        resolution=resolution,
        csv_format=csv_format,
        warnings=tuple(warnings),
    )


def validate_name(value: Any) -> str:
    if not isinstance(value, str):
        raise WorkflowValidationError("name must be a string")
    result = value.strip()
    if not result:
        raise WorkflowValidationError("name must not be empty")
    if len(result) > NAME_MAX_LENGTH:
        raise WorkflowValidationError(
            f"name must be at most {NAME_MAX_LENGTH} characters"
        )
    if any(ord(character) < 32 and character not in "\t" for character in result):
        raise WorkflowValidationError("name contains unsupported control characters")
    return result


def validate_notes(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise WorkflowValidationError("notes must be a string")
    result = value.strip()
    if len(result) > NOTES_MAX_LENGTH:
        raise WorkflowValidationError(
            f"notes must be at most {NOTES_MAX_LENGTH} characters"
        )
    if any(ord(character) < 32 and character not in "\n\r\t" for character in result):
        raise WorkflowValidationError("notes contains unsupported control characters")
    return result


def validate_sources(value: Any) -> tuple[Source, ...]:
    if not isinstance(value, list) or not value:
        raise WorkflowValidationError("sources must be a non-empty array")
    if len(value) > MAX_SOURCES:
        raise WorkflowValidationError(
            f"sources must contain at most {MAX_SOURCES} items"
        )

    result: list[Source] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise WorkflowValidationError(f"sources[{index}] must be an object")
        sensor_type = item.get("sensor_type")
        if sensor_type not in SENSOR_TYPES:
            raise WorkflowValidationError(
                f"sources[{index}].sensor_type must be one of: {', '.join(SENSOR_TYPES)}"
            )
        allowed_keys = (
            {"sensor_type", "node_id"}
            if sensor_type == SENSOR_TYPE_ENVIRONMENT
            else {"sensor_type", "location"}
        )
        unknown = set(item) - allowed_keys
        if unknown:
            raise WorkflowValidationError(
                f"sources[{index}] contains unsupported keys: {', '.join(sorted(unknown))}"
            )
        if sensor_type == SENSOR_TYPE_ENVIRONMENT:
            node_id = item.get("node_id")
            if isinstance(node_id, bool) or not isinstance(node_id, int) or node_id < 1:
                raise WorkflowValidationError(
                    f"sources[{index}].node_id must be an integer >= 1"
                )
            source = Source(sensor_type=sensor_type, node_id=node_id)
        else:
            location = item.get("location")
            if not isinstance(location, str) or not LOCATION_RE.fullmatch(location):
                raise WorkflowValidationError(
                    f"sources[{index}].location must be a stable 1-64 character slug"
                )
            source = Source(sensor_type=sensor_type, location=location)
        if source.key not in seen:
            seen.add(source.key)
            result.append(source)
    return tuple(result)


def validate_fields(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise WorkflowValidationError("fields must be a non-empty array")
    result: list[str] = []
    seen: set[str] = set()
    for index, field in enumerate(value):
        if not isinstance(field, str) or field not in SUPPORTED_FIELDS:
            raise WorkflowValidationError(
                f"fields[{index}] must be one of: {', '.join(SUPPORTED_FIELDS)}"
            )
        if field not in seen:
            seen.add(field)
            result.append(field)
    return tuple(result)


def fields_for_source(
    source: Source, fields: Sequence[str], resolution: str
) -> tuple[str, ...]:
    if resolution == "15m" and source.sensor_type != SENSOR_TYPE_AIR_QUALITY:
        return ()
    supported = (
        ENVIRONMENT_FIELDS
        if source.sensor_type == SENSOR_TYPE_ENVIRONMENT
        else AIR_QUALITY_FIELDS
    )
    return tuple(field for field in fields if field in supported)


def aggregate_field(field: str) -> str:
    if field not in AIR_QUALITY_FIELDS:
        raise WorkflowValidationError(f"field has no 15-minute aggregate: {field}")
    return f"{field}_mean"


def unit_for_field(field: str) -> str:
    base = field[:-5] if field.endswith("_mean") else field
    return FIELD_UNITS.get(base, "")


def parse_client_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowValidationError(
            f"{name} must be an ISO 8601 timestamp with timezone"
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowValidationError(
            f"{name} is not a valid ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkflowValidationError(f"{name} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def parse_stored_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)


def iso_utc(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_uuid(value: Any, object_name: str = "id") -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkflowNotFoundError(f"unknown {object_name}") from exc


def json_sources(value: Iterable[Source]) -> list[dict[str, Any]]:
    return [source.as_dict() for source in value]


def sources_from_json(value: Any) -> tuple[Source, ...]:
    return validate_sources(value)


def finite_csv_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if isinstance(value, int):
        return value
    return number


def _validate_supported_selection(
    sources: Sequence[Source], fields: Sequence[str], resolution: str
) -> None:
    if not any(fields_for_source(source, fields, resolution) for source in sources):
        raise WorkflowValidationError(
            "no selected source supports any requested field at the selected resolution"
        )


def _choice(value: Any, name: str, choices: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise WorkflowValidationError(f"{name} must be one of: {', '.join(choices)}")
    return value


def _object(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise WorkflowValidationError("request body must be a JSON object")
    return payload


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
