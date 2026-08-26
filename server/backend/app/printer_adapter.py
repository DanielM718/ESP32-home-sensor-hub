"""Read-only Home Assistant adapter and centralized printer normalization."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.printer_config import PrinterObserverSettings
from app.printer_model import (
    AmsTrayState,
    AmsUnitState,
    NormalizedPrinterState,
    PrinterState,
    ValueProvenance,
)

UNAVAILABLE_STATES = frozenset({"", "unknown", "unavailable", "none", "null", "empty"})
SAFE_DISCOVERY_ATTRIBUTES = frozenset(
    {
        "friendly_name",
        "device_class",
        "unit_of_measurement",
        "state_class",
        "icon",
        "percentage",
        "current_layer",
        "total_layers",
        "task_name",
        "project_name",
        "name",
        "type",
        "material",
        "tray_type",
        "tray_name",
        "active",
        "temperature",
        "target_temperature",
    }
)

STATE_MAP: dict[str, NormalizedPrinterState] = {
    "offline": NormalizedPrinterState.OFFLINE,
    "idle": NormalizedPrinterState.IDLE,
    "ready": NormalizedPrinterState.IDLE,
    "init": NormalizedPrinterState.PREPARING,
    "initializing": NormalizedPrinterState.PREPARING,
    "prepare": NormalizedPrinterState.PREPARING,
    "preparing": NormalizedPrinterState.PREPARING,
    "slicing": NormalizedPrinterState.PREPARING,
    "running": NormalizedPrinterState.PRINTING,
    "printing": NormalizedPrinterState.PRINTING,
    "pause": NormalizedPrinterState.PAUSED,
    "paused": NormalizedPrinterState.PAUSED,
    "finishing": NormalizedPrinterState.FINISHING,
    "cooling": NormalizedPrinterState.FINISHING,
    "finish": NormalizedPrinterState.COMPLETED,
    "finished": NormalizedPrinterState.COMPLETED,
    "complete": NormalizedPrinterState.COMPLETED,
    "completed": NormalizedPrinterState.COMPLETED,
    "failed": NormalizedPrinterState.FAILED,
    "error": NormalizedPrinterState.FAILED,
    "cancel": NormalizedPrinterState.CANCELLED,
    "canceled": NormalizedPrinterState.CANCELLED,
    "cancelled": NormalizedPrinterState.CANCELLED,
    "stopped": NormalizedPrinterState.CANCELLED,
    "unknown": NormalizedPrinterState.UNKNOWN,
}


class PrinterAdapterError(RuntimeError):
    """Credential-safe adapter failure."""


def normalize_printer_state(
    raw_state: object, *, online: bool
) -> NormalizedPrinterState:
    if not online:
        return NormalizedPrinterState.OFFLINE
    key = re.sub(r"[\s-]+", "_", str(raw_state or "").strip().lower())
    return STATE_MAP.get(key, NormalizedPrinterState.UNKNOWN)


class HomeAssistantPrinterAdapter:
    """Fetch one bounded HA state snapshot, retaining only configured entities."""

    def __init__(
        self,
        settings: PrinterObserverSettings,
        token: str,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not token.strip():
            raise PrinterAdapterError("Home Assistant token is not configured")
        self.settings = settings
        self._token = token.strip()
        self._opener = opener

    def fetch(self, *, observed_at: datetime | None = None) -> PrinterState:
        request = Request(
            f"{self.settings.home_assistant_url}/api/states",
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with self._opener(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                if getattr(response, "status", 200) != 200:
                    raise PrinterAdapterError(
                        "Home Assistant returned a non-success status"
                    )
                raw = response.read(self.settings.max_response_bytes + 1)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise PrinterAdapterError(
                    "Home Assistant authentication was denied"
                ) from None
            raise PrinterAdapterError(
                "Home Assistant returned a non-success status"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise PrinterAdapterError("Home Assistant is unavailable") from None
        if len(raw) > self.settings.max_response_bytes:
            raise PrinterAdapterError("Home Assistant response exceeded the size limit")
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PrinterAdapterError(
                "Home Assistant returned malformed JSON"
            ) from None
        if not isinstance(decoded, list):
            raise PrinterAdapterError(
                "Home Assistant state response has an invalid shape"
            )
        wanted = set(self.settings.entities.values())
        for unit in self.settings.ams_units:
            wanted.update(unit.entities.values())
            wanted.update(unit.tray_entities)
        states = {
            str(item.get("entity_id")): item
            for item in decoded
            if isinstance(item, dict) and item.get("entity_id") in wanted
        }
        return printer_state_from_home_assistant(
            states,
            self.settings,
            observed_at=observed_at or datetime.now(timezone.utc),
        )


def printer_state_from_home_assistant(
    states: Mapping[str, Mapping[str, Any]],
    settings: PrinterObserverSettings,
    *,
    observed_at: datetime,
) -> PrinterState:
    """Normalize only explicit entity mappings; absent values remain absent."""

    mapped = {
        key: states.get(entity_id) for key, entity_id in settings.entities.items()
    }
    online_entity = mapped.get("online")
    status_entity = mapped.get("print_status")
    online = _online(online_entity)
    normalized = normalize_printer_state(_state(status_entity), online=online)

    timestamps = [
        timestamp
        for entity in mapped.values()
        if entity is not None
        for timestamp in (_entity_timestamp(entity),)
        if timestamp is not None
    ]
    # Retained for provenance and diagnostics only. Entity age must never
    # override availability: Home Assistant reports printer entities on change,
    # so a quiet, settled printer legitimately stops updating them for minutes
    # at a time. The mapped online entity is the authoritative availability
    # signal, and a genuinely unreachable printer is reported through it (or
    # through the adapter error path when Home Assistant itself fails).
    # Deriving OFFLINE from timestamp age produced false offline transitions
    # while the printer was idle and reachable.
    source_timestamp = max(timestamps) if timestamps else None

    provenance: dict[str, ValueProvenance] = {}
    material = _text_entity(mapped.get("active_material"))
    filament = _text_entity(mapped.get("active_filament"))
    if material is not None:
        provenance["active_material"] = ValueProvenance.OBSERVED
    if filament is not None:
        provenance["active_filament"] = ValueProvenance.OBSERVED

    # Infer only from the specifically mapped active tray/slot entity, never
    # from arbitrary AMS inventory contents.
    active_tray = mapped.get("ams_slot")
    if active_tray is not None:
        attributes = _attributes(active_tray)
        if material is None:
            material = _attribute_text(attributes, "type", "material", "tray_type")
            if material is not None:
                provenance["active_material"] = ValueProvenance.INFERRED_ACTIVE_AMS_TRAY
        if filament is None:
            filament = _attribute_text(attributes, "name", "filament", "tray_name")
            if filament is not None:
                provenance["active_filament"] = ValueProvenance.INFERRED_ACTIVE_AMS_TRAY

    started_at = _datetime_entity(mapped.get("print_started_at"))
    finished_at = _datetime_entity(mapped.get("print_finished_at"))
    expected_finished_at = _datetime_entity(mapped.get("expected_finished_at"))
    if started_at is not None:
        provenance["print_started_at"] = ValueProvenance.OBSERVED
    if finished_at is not None:
        provenance["print_finished_at"] = ValueProvenance.OBSERVED

    ams_units = _ams_units(states, settings)
    ams_slot, ams_slot_provenance = _active_ams_slot(active_tray, ams_units)
    if ams_slot is not None:
        provenance["ams_slot"] = ams_slot_provenance

    return PrinterState(
        printer_id=settings.printer_id,
        printer_model=settings.printer_model,
        online=online,
        normalized_state=normalized,
        source="home_assistant",
        source_timestamp=source_timestamp,
        observed_at=_aware_utc(observed_at),
        unavailable_reason=(None if online else _offline_reason(online_entity)),
        current_stage=_text_entity(mapped.get("current_stage")),
        job_id=_text_entity(mapped.get("job_id")),
        job_name=_text_entity(mapped.get("job_name")),
        progress_percent=_number_entity(mapped.get("progress_percent"), 0, 100),
        remaining_seconds=_duration_seconds(mapped.get("remaining_time")),
        current_layer=_integer_entity(mapped.get("current_layer"), 0),
        total_layers=_integer_entity(mapped.get("total_layers"), 0),
        print_started_at=started_at,
        print_finished_at=finished_at,
        expected_finished_at=expected_finished_at,
        nozzle_1_temperature=_temperature(mapped.get("nozzle_1_temperature")),
        nozzle_1_target=_temperature(mapped.get("nozzle_1_target")),
        nozzle_2_temperature=_temperature(mapped.get("nozzle_2_temperature")),
        nozzle_2_target=_temperature(mapped.get("nozzle_2_target")),
        bed_temperature=_temperature(mapped.get("bed_temperature")),
        bed_target=_temperature(mapped.get("bed_target")),
        chamber_temperature=_temperature(mapped.get("chamber_temperature")),
        chamber_target=_temperature(mapped.get("chamber_target")),
        active_tool=_text_entity(mapped.get("active_tool")),
        active_material=material,
        active_filament=filament,
        ams_state=_text_entity(mapped.get("ams_state")),
        ams_slot=ams_slot,
        print_source=_text_entity(mapped.get("print_source")),
        firmware_version=(
            _text_entity(mapped.get("firmware_version")) or settings.firmware_version
        ),
        gcode_filename=_text_entity(mapped.get("gcode_filename")),
        print_bed_type=_text_entity(mapped.get("print_bed_type")),
        print_weight_grams=_number_entity(
            mapped.get("print_weight_grams"), 0, 1_000_000
        ),
        print_length_meters=_number_entity(
            mapped.get("print_length_meters"), 0, 1_000_000
        ),
        nozzle_1_type=_text_entity(mapped.get("nozzle_1_type")),
        nozzle_1_size_mm=_number_entity(mapped.get("nozzle_1_size_mm"), 0, 10),
        nozzle_2_type=_text_entity(mapped.get("nozzle_2_type")),
        nozzle_2_size_mm=_number_entity(mapped.get("nozzle_2_size_mm"), 0, 10),
        cooling_fan_percent=_number_entity(mapped.get("cooling_fan_percent"), 0, 100),
        auxiliary_fan_percent=_number_entity(
            mapped.get("auxiliary_fan_percent"), 0, 100
        ),
        chamber_fan_percent=_number_entity(mapped.get("chamber_fan_percent"), 0, 100),
        heatbreak_fan_percent=_number_entity(
            mapped.get("heatbreak_fan_percent"), 0, 100
        ),
        wifi_signal_dbm=_number_entity(mapped.get("wifi_signal_dbm"), -200, 0),
        mqtt_connection_mode=_text_entity(mapped.get("mqtt_connection_mode")),
        mqtt_encryption=_boolean_entity(mapped.get("mqtt_encryption")),
        hybrid_mqtt_control_blocked=_boolean_entity(
            mapped.get("hybrid_mqtt_control_blocked")
        ),
        developer_lan_mode=_boolean_entity(mapped.get("developer_lan_mode")),
        ha_bambulab_estimated_usage_hours=_number_entity(
            mapped.get("ha_bambulab_estimated_usage_hours"), 0, 10_000_000
        ),
        printer_reported_lifetime_hours=_number_entity(
            mapped.get("printer_reported_lifetime_hours"), 0, 10_000_000
        ),
        cover_available=_available_entity(mapped.get("cover_image")),
        ams_units=ams_units,
        provenance=provenance,
    )


def unavailable_printer_state(
    settings: PrinterObserverSettings,
    *,
    reason: str,
    observed_at: datetime | None = None,
) -> PrinterState:
    return PrinterState(
        printer_id=settings.printer_id,
        printer_model=settings.printer_model,
        online=False,
        normalized_state=NormalizedPrinterState.OFFLINE,
        source="home_assistant",
        source_timestamp=None,
        observed_at=_aware_utc(observed_at or datetime.now(timezone.utc)),
        unavailable_reason=reason,
    )


def discover_bambu_entities(
    states: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return redacted discovery candidates without exposing sensitive attrs."""

    candidates = []
    for entity in states:
        entity_id = str(entity.get("entity_id", ""))
        attributes = _attributes(entity)
        friendly = str(attributes.get("friendly_name", ""))
        haystack = f"{entity_id} {friendly}".lower()
        if not any(word in haystack for word in ("bambu", "x2d", "printer")):
            continue
        safe_attributes = {
            str(key): value
            for key, value in attributes.items()
            if str(key).lower() in SAFE_DISCOVERY_ATTRIBUTES
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        sensitive_state = any(
            name in haystack
            for name in (
                "access",
                "code",
                "token",
                "password",
                "secret",
                "ip address",
                "ip_address",
                "serial",
                "device id",
                "device_id",
                "identifier",
                "mac address",
                "mac_address",
                "ssid",
                "cloud",
            )
        )
        candidates.append(
            {
                "entity_id": entity_id,
                "state": "<redacted>"
                if sensitive_state
                else _safe_scalar(_state(entity)),
                "last_updated": entity.get("last_updated"),
                "attributes": safe_attributes,
            }
        )
    return sorted(candidates, key=lambda item: item["entity_id"])


def _state(entity: Mapping[str, Any] | None) -> Any:
    return None if entity is None else entity.get("state")


def _attributes(entity: Mapping[str, Any] | None) -> Mapping[str, Any]:
    value = {} if entity is None else entity.get("attributes", {})
    return value if isinstance(value, Mapping) else {}


def _online(entity: Mapping[str, Any] | None) -> bool:
    state = str(_state(entity) or "").strip().lower()
    return state in {"on", "true", "1", "online", "connected", "available"}


def _boolean_entity(entity: Mapping[str, Any] | None) -> bool | None:
    if entity is None:
        return None
    state = str(_state(entity) or "").strip().lower()
    if state in {"on", "true", "1", "yes", "enabled"}:
        return True
    if state in {"off", "false", "0", "no", "disabled"}:
        return False
    return None


def _available_entity(entity: Mapping[str, Any] | None) -> bool | None:
    if entity is None:
        return None
    return str(_state(entity) or "").strip().lower() not in UNAVAILABLE_STATES


def _ams_units(
    states: Mapping[str, Mapping[str, Any]], settings: PrinterObserverSettings
) -> tuple[AmsUnitState, ...]:
    units = []
    for configured in settings.ams_units:
        mapped = {
            key: states.get(entity_id) for key, entity_id in configured.entities.items()
        }
        trays = []
        for index, entity_id in enumerate(configured.tray_entities, start=1):
            entity = states.get(entity_id)
            attributes = _attributes(entity)
            remain = attributes.get("remain")
            trays.append(
                AmsTrayState(
                    slot=str(attributes.get("slot") or index),
                    name=_attribute_text(attributes, "name") or _text_entity(entity),
                    material=_attribute_text(attributes, "type", "material"),
                    color=_attribute_text(attributes, "color"),
                    remaining_percent=(
                        int(remain)
                        if isinstance(remain, (int, float))
                        and not isinstance(remain, bool)
                        and 0 <= remain <= 100
                        else None
                    ),
                    active=_attribute_bool(attributes, "active"),
                    empty=_attribute_bool(attributes, "empty"),
                )
            )
        units.append(
            AmsUnitState(
                ams_id=configured.ams_id,
                model=configured.model,
                active=_boolean_entity(mapped.get("active")),
                humidity_percent=_number_entity(mapped.get("humidity_percent"), 0, 100),
                humidity_index=_integer_entity(mapped.get("humidity_index"), 0),
                temperature=_temperature(mapped.get("temperature")),
                drying=_boolean_entity(mapped.get("drying")),
                remaining_drying_seconds=_duration_seconds(
                    mapped.get("remaining_drying_time")
                ),
                trays=tuple(trays),
            )
        )
    return tuple(units)


def _active_ams_slot(
    active_tray: Mapping[str, Any] | None,
    units: tuple[AmsUnitState, ...],
) -> tuple[str | None, ValueProvenance]:
    attributes = _attributes(active_tray)
    direct_slot = attributes.get("slot")
    if isinstance(direct_slot, (str, int)) and str(direct_slot).strip():
        return str(direct_slot).strip()[:64], ValueProvenance.OBSERVED
    for unit in units:
        for tray in unit.trays:
            if tray.active is True:
                return (
                    f"{unit.ams_id} / slot {tray.slot}",
                    ValueProvenance.INFERRED_ACTIVE_AMS_TRAY,
                )
    label = _text_entity(active_tray)
    return label, ValueProvenance.OBSERVED if label else ValueProvenance.UNKNOWN


def _offline_reason(entity: Mapping[str, Any] | None) -> str:
    if entity is None:
        return "online entity was not returned by Home Assistant"
    state = str(_state(entity) or "unknown").strip().lower()
    if state in UNAVAILABLE_STATES:
        return "Home Assistant reports printer availability as unknown"
    return "Home Assistant reports the printer offline"


def _text_entity(entity: Mapping[str, Any] | None) -> str | None:
    value = _state(entity)
    if not isinstance(value, str) or value.strip().lower() in UNAVAILABLE_STATES:
        return None
    return value.strip()[:512]


def _number_entity(
    entity: Mapping[str, Any] | None, minimum: float, maximum: float
) -> float | None:
    value = _state(entity)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def _integer_entity(entity: Mapping[str, Any] | None, minimum: int) -> int | None:
    value = _number_entity(entity, minimum, 10_000_000)
    return int(value) if value is not None and value.is_integer() else None


def _temperature(entity: Mapping[str, Any] | None) -> float | None:
    value = _number_entity(entity, -100, 1000)
    if value is None:
        return None
    unit = str(_attributes(entity).get("unit_of_measurement", "°C")).strip().lower()
    if unit in {"°f", "f", "fahrenheit"}:
        value = (value - 32) * 5 / 9
    return round(value, 3) if -100 <= value <= 500 else None


def _duration_seconds(entity: Mapping[str, Any] | None) -> int | None:
    raw = _state(entity)
    if isinstance(raw, str) and re.fullmatch(r"\d{1,3}:\d{2}(?::\d{2})?", raw.strip()):
        parts = [int(part) for part in raw.split(":")]
        if len(parts) == 2:
            return parts[0] * 3600 + parts[1] * 60
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    value = _number_entity(entity, 0, 10_000_000)
    if value is None:
        return None
    unit = str(_attributes(entity).get("unit_of_measurement", "min")).lower()
    factor = (
        1
        if unit in {"s", "sec", "second", "seconds"}
        else 3600
        if unit in {"h", "hour", "hours"}
        else 60
    )
    return round(value * factor)


def _datetime_entity(entity: Mapping[str, Any] | None) -> datetime | None:
    return _parse_datetime(_state(entity))


def _entity_timestamp(entity: Mapping[str, Any]) -> datetime | None:
    return _parse_datetime(entity.get("last_reported") or entity.get("last_updated"))


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or value.strip().lower() in UNAVAILABLE_STATES:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _attribute_text(attributes: Mapping[str, Any], *names: str) -> str | None:
    lower = {str(key).lower(): value for key, value in attributes.items()}
    for name in names:
        value = lower.get(name)
        if isinstance(value, str) and value.strip().lower() not in UNAVAILABLE_STATES:
            return value.strip()[:512]
    return None


def _attribute_bool(attributes: Mapping[str, Any], name: str) -> bool | None:
    value = {str(key).lower(): item for key, item in attributes.items()}.get(name)
    return value if isinstance(value, bool) else None


def _safe_scalar(value: object) -> str | int | float | bool | None:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
