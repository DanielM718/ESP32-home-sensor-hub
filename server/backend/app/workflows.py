"""Shared validation and presentation definitions for monitoring and exports."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

SENSOR_TYPE_ENVIRONMENT = "environment"
SENSOR_TYPE_AIR_QUALITY = "air_quality"
SENSOR_TYPE_PRINTER = "printer"
SENSOR_TYPE_AMS = "ams"
SENSOR_TYPES = (
    SENSOR_TYPE_ENVIRONMENT,
    SENSOR_TYPE_AIR_QUALITY,
    SENSOR_TYPE_PRINTER,
    SENSOR_TYPE_AMS,
)

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
PRINTER_FIELDS = (
    "chamber_temperature_c",
    "chamber_target_c",
    "bed_temperature_c",
    "bed_target_c",
    "nozzle_1_temperature_c",
    "nozzle_1_target_c",
    "nozzle_2_temperature_c",
    "nozzle_2_target_c",
    "cooling_fan_percent",
    "auxiliary_fan_percent",
    "chamber_fan_percent",
    "heatbreak_fan_percent",
    "wifi_signal_dbm",
    "print_progress_percent",
    "remaining_print_seconds",
    "online",
    "printer_is_printing",
    "session_active",
)
AMS_FIELDS = (
    "ams_humidity",
    "ams_temperature_c",
    "ams_humidity_index",
    "ams_drying",
    "ams_remaining_drying_seconds",
    "ams_active",
)
FIELDS_BY_SENSOR_TYPE = {
    SENSOR_TYPE_ENVIRONMENT: ENVIRONMENT_FIELDS,
    SENSOR_TYPE_AIR_QUALITY: AIR_QUALITY_FIELDS,
    SENSOR_TYPE_PRINTER: PRINTER_FIELDS,
    SENSOR_TYPE_AMS: AMS_FIELDS,
}
SUPPORTED_FIELDS = tuple(
    dict.fromkeys(ENVIRONMENT_FIELDS + AIR_QUALITY_FIELDS + PRINTER_FIELDS + AMS_FIELDS)
)
BOOLEAN_FIELDS = frozenset(
    {"online", "printer_is_printing", "session_active", "ams_drying", "ams_active"}
)
NUMERIC_FIELDS = frozenset(SUPPORTED_FIELDS) - BOOLEAN_FIELDS

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
    "chamber_temperature_c": "degC",
    "chamber_target_c": "degC",
    "bed_temperature_c": "degC",
    "bed_target_c": "degC",
    "nozzle_1_temperature_c": "degC",
    "nozzle_1_target_c": "degC",
    "nozzle_2_temperature_c": "degC",
    "nozzle_2_target_c": "degC",
    "cooling_fan_percent": "percent",
    "auxiliary_fan_percent": "percent",
    "chamber_fan_percent": "percent",
    "heatbreak_fan_percent": "percent",
    "wifi_signal_dbm": "dBm",
    "print_progress_percent": "percent",
    "remaining_print_seconds": "seconds",
    "online": "boolean",
    "printer_is_printing": "boolean",
    "session_active": "boolean",
    "ams_humidity": "percent",
    "ams_temperature_c": "degC",
    "ams_humidity_index": "index",
    "ams_drying": "boolean",
    "ams_remaining_drying_seconds": "seconds",
    "ams_active": "boolean",
}
FIELD_LABELS = {
    "temperature_c": "Temperature",
    "humidity": "Humidity",
    "battery_mv": "Battery voltage",
    "co2": "CO₂",
    "pm1": "PM1.0",
    "pm25": "PM2.5",
    "pm4": "PM4.0",
    "pm10": "PM10",
    "voc_index": "VOC Index",
    "nox_index": "NOx Index",
    "chamber_temperature_c": "Chamber temperature",
    "chamber_target_c": "Chamber target",
    "bed_temperature_c": "Bed temperature",
    "bed_target_c": "Bed target",
    "nozzle_1_temperature_c": "Nozzle 1 temperature",
    "nozzle_1_target_c": "Nozzle 1 target",
    "nozzle_2_temperature_c": "Nozzle 2 temperature",
    "nozzle_2_target_c": "Nozzle 2 target",
    "cooling_fan_percent": "Cooling fan",
    "auxiliary_fan_percent": "Auxiliary fan",
    "chamber_fan_percent": "Chamber fan",
    "heatbreak_fan_percent": "Heatbreak fan",
    "wifi_signal_dbm": "Wi-Fi signal",
    "print_progress_percent": "Print progress",
    "remaining_print_seconds": "Remaining print time",
    "online": "Online",
    "printer_is_printing": "Printing",
    "session_active": "Print session active",
    "ams_humidity": "AMS humidity",
    "ams_temperature_c": "AMS temperature",
    "ams_humidity_index": "AMS humidity index",
    "ams_drying": "AMS drying",
    "ams_remaining_drying_seconds": "AMS drying time remaining",
    "ams_active": "AMS active",
}
FIELD_DISPLAY_UNITS = {
    "temperature_c": "°C",
    "humidity": "%",
    "battery_mv": "mV",
    "co2": "ppm",
    "pm1": "µg/m³",
    "pm25": "µg/m³",
    "pm4": "µg/m³",
    "pm10": "µg/m³",
    "voc_index": "index",
    "nox_index": "index",
    "chamber_temperature_c": "°C",
    "chamber_target_c": "°C",
    "bed_temperature_c": "°C",
    "bed_target_c": "°C",
    "nozzle_1_temperature_c": "°C",
    "nozzle_1_target_c": "°C",
    "nozzle_2_temperature_c": "°C",
    "nozzle_2_target_c": "°C",
    "cooling_fan_percent": "%",
    "auxiliary_fan_percent": "%",
    "chamber_fan_percent": "%",
    "heatbreak_fan_percent": "%",
    "wifi_signal_dbm": "dBm",
    "print_progress_percent": "%",
    "remaining_print_seconds": "s",
    "online": "on/off",
    "printer_is_printing": "on/off",
    "session_active": "on/off",
    "ams_humidity": "% RH",
    "ams_temperature_c": "°C",
    "ams_humidity_index": "index",
    "ams_drying": "on/off",
    "ams_remaining_drying_seconds": "s",
    "ams_active": "on/off",
}
FIELD_GROUPS = {
    "temperature_c": "Climate",
    "humidity": "Climate",
    "battery_mv": "Battery",
    "co2": "Gas and indices",
    "pm1": "Particulate matter",
    "pm25": "Particulate matter",
    "pm4": "Particulate matter",
    "pm10": "Particulate matter",
    "voc_index": "Gas and indices",
    "nox_index": "Gas and indices",
    "chamber_temperature_c": "Printer Thermal",
    "chamber_target_c": "Printer Thermal",
    "bed_temperature_c": "Printer Thermal",
    "bed_target_c": "Printer Thermal",
    "nozzle_1_temperature_c": "Printer Thermal",
    "nozzle_1_target_c": "Printer Thermal",
    "nozzle_2_temperature_c": "Printer Thermal",
    "nozzle_2_target_c": "Printer Thermal",
    "cooling_fan_percent": "Cooling / Diagnostics",
    "auxiliary_fan_percent": "Cooling / Diagnostics",
    "chamber_fan_percent": "Cooling / Diagnostics",
    "heatbreak_fan_percent": "Cooling / Diagnostics",
    "wifi_signal_dbm": "Cooling / Diagnostics",
    "print_progress_percent": "Print Context",
    "remaining_print_seconds": "Print Context",
    "online": "Print Context",
    "printer_is_printing": "Print Context",
    "session_active": "Print Context",
    "ams_humidity": "AMS Climate",
    "ams_temperature_c": "AMS Climate",
    "ams_humidity_index": "AMS State",
    "ams_drying": "AMS State",
    "ams_remaining_drying_seconds": "AMS State",
    "ams_active": "AMS State",
}

RESOLUTION_OPTIONS = (
    {
        "value": "raw",
        "label": "Raw samples",
        "window_seconds": None,
        "aggregation": "none",
    },
    {
        "value": "1m",
        "label": "1-minute mean",
        "window_seconds": 60,
        "aggregation": "mean",
    },
    {
        "value": "5m",
        "label": "5-minute mean",
        "window_seconds": 5 * 60,
        "aggregation": "mean",
    },
    {
        "value": "15m",
        "label": "15-minute mean",
        "window_seconds": 15 * 60,
        "aggregation": "mean",
    },
    {
        "value": "1h",
        "label": "1-hour mean",
        "window_seconds": 60 * 60,
        "aggregation": "mean",
    },
)
RESOLUTIONS = tuple(option["value"] for option in RESOLUTION_OPTIONS)
RESOLUTION_WINDOWS = {
    str(option["value"]): option["window_seconds"] for option in RESOLUTION_OPTIONS
}
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
    printer_id: str | None = None
    ams_id: str | None = None

    @property
    def source_id(self) -> str:
        if self.sensor_type == SENSOR_TYPE_ENVIRONMENT:
            return str(self.node_id)
        if self.sensor_type == SENSOR_TYPE_AIR_QUALITY:
            return str(self.location)
        if self.sensor_type == SENSOR_TYPE_PRINTER:
            return str(self.printer_id)
        return f"{self.printer_id}/{self.ams_id}"

    @property
    def key(self) -> tuple[str, str]:
        return self.sensor_type, self.source_id

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"sensor_type": self.sensor_type}
        if self.sensor_type == SENSOR_TYPE_ENVIRONMENT:
            result["node_id"] = self.node_id
        elif self.sensor_type == SENSOR_TYPE_AIR_QUALITY:
            result["location"] = self.location
        elif self.sensor_type == SENSOR_TYPE_PRINTER:
            result["printer_id"] = self.printer_id
        else:
            result["printer_id"] = self.printer_id
            result["ams_id"] = self.ams_id
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
    csv_format = _choice(data.get("csv_format", "wide"), "csv_format", CSV_FORMATS)
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
    csv_format = _choice(data.get("csv_format", "wide"), "csv_format", CSV_FORMATS)
    _validate_supported_selection(
        sources,
        fields,
        resolution,
        stored_aggregate=resolution == "15m",
    )

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
    if resolution in {"raw", "1m", "5m", "1h"} and any(
        source.sensor_type == SENSOR_TYPE_AIR_QUALITY for source in sources
    ):
        reference = _aware_utc(now or datetime.now(timezone.utc))
        retained_after = reference.timestamp() - raw_retention_seconds
        if start.timestamp() < retained_after:
            boundary = datetime.fromtimestamp(retained_after, tz=timezone.utc)
            warnings.append(
                "Raw air-quality data used for this resolution is retained for approximately "
                f"{raw_retention_seconds // 3600} hours; data before {iso_utc(boundary)} "
                "may have expired. Aggregates are not substituted."
            )
    bambu_selected = any(
        source.sensor_type in {SENSOR_TYPE_PRINTER, SENSOR_TYPE_AMS}
        for source in sources
    )
    if bambu_selected:
        reference = _aware_utc(now or datetime.now(timezone.utc))
        retained_after = reference.timestamp() - raw_retention_seconds
        if start.timestamp() < retained_after and resolution in {"raw", "1m"}:
            boundary = datetime.fromtimestamp(retained_after, tz=timezone.utc)
            warnings.append(
                "High-resolution Bambu telemetry is retained for approximately "
                f"{raw_retention_seconds // 3600} hours; data before "
                f"{iso_utc(boundary)} may have expired. Durable five-minute samples "
                "are not substituted for raw or 1-minute exports."
            )
        elif start.timestamp() < retained_after and resolution in {"5m", "15m", "1h"}:
            warnings.append(
                "Bambu telemetry outside live retention uses permanent five-minute "
                "samples and reports that durable source tier in the CSV."
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
        allowed_keys = {
            SENSOR_TYPE_ENVIRONMENT: {"sensor_type", "node_id"},
            SENSOR_TYPE_AIR_QUALITY: {"sensor_type", "location"},
            SENSOR_TYPE_PRINTER: {"sensor_type", "printer_id"},
            SENSOR_TYPE_AMS: {"sensor_type", "printer_id", "ams_id"},
        }[sensor_type]
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
        elif sensor_type == SENSOR_TYPE_AIR_QUALITY:
            location = item.get("location")
            if not isinstance(location, str) or not LOCATION_RE.fullmatch(location):
                raise WorkflowValidationError(
                    f"sources[{index}].location must be a stable 1-64 character slug"
                )
            source = Source(sensor_type=sensor_type, location=location)
        else:
            printer_id = item.get("printer_id")
            if not isinstance(printer_id, str) or not LOCATION_RE.fullmatch(printer_id):
                raise WorkflowValidationError(
                    f"sources[{index}].printer_id must be a stable 1-64 character slug"
                )
            if sensor_type == SENSOR_TYPE_PRINTER:
                source = Source(sensor_type=sensor_type, printer_id=printer_id)
            else:
                ams_id = item.get("ams_id")
                if not isinstance(ams_id, str) or not LOCATION_RE.fullmatch(ams_id):
                    raise WorkflowValidationError(
                        f"sources[{index}].ams_id must be a stable 1-64 character slug"
                    )
                source = Source(
                    sensor_type=sensor_type,
                    printer_id=printer_id,
                    ams_id=ams_id,
                )
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
    source: Source,
    fields: Sequence[str],
    resolution: str,
    *,
    stored_aggregate: bool = False,
) -> tuple[str, ...]:
    if stored_aggregate and source.sensor_type == SENSOR_TYPE_ENVIRONMENT:
        return ()
    supported = FIELDS_BY_SENSOR_TYPE[source.sensor_type]
    return tuple(
        field
        for field in fields
        if field in supported and (resolution == "raw" or field in NUMERIC_FIELDS)
    )


def field_supports_aggregation(field: str) -> bool:
    return field in NUMERIC_FIELDS


def aggregate_field(field: str) -> str:
    if field not in AIR_QUALITY_FIELDS:
        raise WorkflowValidationError(f"field has no 15-minute aggregate: {field}")
    return f"{field}_mean"


def unit_for_field(field: str) -> str:
    base = field.removesuffix("_mean")
    return FIELD_UNITS.get(base, "")


def resolution_window_seconds(resolution: str) -> int | None:
    """Return the mean window for a supported resolution, or ``None`` for raw."""

    if resolution not in RESOLUTION_WINDOWS:
        raise WorkflowValidationError(f"unsupported resolution: {resolution}")
    value = RESOLUTION_WINDOWS[resolution]
    return int(value) if value is not None else None


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
    sources: Sequence[Source],
    fields: Sequence[str],
    resolution: str,
    *,
    stored_aggregate: bool = False,
) -> None:
    if resolution != "raw":
        boolean_fields = [field for field in fields if field in BOOLEAN_FIELDS]
        if boolean_fields:
            raise WorkflowValidationError(
                "boolean/status measurements are raw-only and cannot be averaged: "
                + ", ".join(boolean_fields)
            )
    if not any(
        fields_for_source(
            source,
            fields,
            resolution,
            stored_aggregate=stored_aggregate,
        )
        for source in sources
    ):
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
