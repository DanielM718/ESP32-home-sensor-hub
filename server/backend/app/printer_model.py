"""Protocol-independent printer state and InfluxDB point models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class NormalizedPrinterState(str, Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    PREPARING = "preparing"
    PRINTING = "printing"
    PAUSED = "paused"
    FINISHING = "finishing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ValueProvenance(str, Enum):
    OBSERVED = "observed"
    INFERRED_ACTIVE_AMS_TRAY = "inferred_active_ams_tray"
    INFERRED_TIMESTAMP = "inferred_timestamp"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AmsTrayState:
    slot: str
    name: str | None = None
    material: str | None = None
    color: str | None = None
    remaining_percent: int | None = None
    active: bool | None = None
    empty: bool | None = None


@dataclass(frozen=True, slots=True)
class AmsUnitState:
    ams_id: str
    model: str
    active: bool | None = None
    humidity_percent: float | None = None
    humidity_index: int | None = None
    temperature: float | None = None
    drying: bool | None = None
    remaining_drying_seconds: int | None = None
    trays: tuple[AmsTrayState, ...] = ()


SESSION_ACTIVE_STATES = frozenset(
    {
        NormalizedPrinterState.PREPARING,
        NormalizedPrinterState.PRINTING,
        NormalizedPrinterState.PAUSED,
        NormalizedPrinterState.FINISHING,
    }
)

# A logical session includes preparation, pauses, and finishing. The derived
# printer_is_printing flag is deliberately narrower: only the upstream printing
# state counts, so callers never mistake heating, cooling, or a pause for active
# deposition.
MATERIAL_DEPOSITION_STATES = frozenset({NormalizedPrinterState.PRINTING})


@dataclass(frozen=True, slots=True)
class PrinterState:
    printer_id: str
    printer_model: str
    online: bool
    normalized_state: NormalizedPrinterState
    source: str
    source_timestamp: datetime | None
    observed_at: datetime
    unavailable_reason: str | None = None
    current_stage: str | None = None
    job_id: str | None = None
    job_name: str | None = None
    progress_percent: float | None = None
    remaining_seconds: int | None = None
    current_layer: int | None = None
    total_layers: int | None = None
    print_started_at: datetime | None = None
    print_finished_at: datetime | None = None
    expected_finished_at: datetime | None = None
    nozzle_1_temperature: float | None = None
    nozzle_1_target: float | None = None
    nozzle_2_temperature: float | None = None
    nozzle_2_target: float | None = None
    bed_temperature: float | None = None
    bed_target: float | None = None
    chamber_temperature: float | None = None
    chamber_target: float | None = None
    active_tool: str | None = None
    active_material: str | None = None
    active_filament: str | None = None
    ams_state: str | None = None
    ams_slot: str | None = None
    print_source: str | None = None
    firmware_version: str | None = None
    gcode_filename: str | None = None
    print_bed_type: str | None = None
    print_weight_grams: float | None = None
    print_length_meters: float | None = None
    nozzle_1_type: str | None = None
    nozzle_1_size_mm: float | None = None
    nozzle_2_type: str | None = None
    nozzle_2_size_mm: float | None = None
    cooling_fan_percent: float | None = None
    auxiliary_fan_percent: float | None = None
    chamber_fan_percent: float | None = None
    heatbreak_fan_percent: float | None = None
    wifi_signal_dbm: float | None = None
    mqtt_connection_mode: str | None = None
    mqtt_encryption: bool | None = None
    hybrid_mqtt_control_blocked: bool | None = None
    developer_lan_mode: bool | None = None
    ha_bambulab_estimated_usage_hours: float | None = None
    printer_reported_lifetime_hours: float | None = None
    cover_available: bool | None = None
    ams_units: tuple[AmsUnitState, ...] = ()
    provenance: dict[str, ValueProvenance] = field(default_factory=dict)

    @property
    def session_active(self) -> bool:
        return self.normalized_state in SESSION_ACTIVE_STATES

    @property
    def printer_is_printing(self) -> bool:
        return self.online and self.normalized_state in MATERIAL_DEPOSITION_STATES

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["normalized_state"] = self.normalized_state.value
        result["source_timestamp"] = _iso(self.source_timestamp)
        result["observed_at"] = _iso(self.observed_at)
        result["print_started_at"] = _iso(self.print_started_at)
        result["print_finished_at"] = _iso(self.print_finished_at)
        result["expected_finished_at"] = _iso(self.expected_finished_at)
        result["ams_units"] = [
            {
                **asdict(unit),
                "trays": [asdict(tray) for tray in unit.trays],
            }
            for unit in self.ams_units
        ]
        result["provenance"] = {
            key: value.value for key, value in self.provenance.items()
        }
        result["session_active"] = self.session_active
        result["printer_is_printing"] = self.printer_is_printing
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PrinterState:
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        data = {key: item for key, item in value.items() if key in allowed}
        data["normalized_state"] = NormalizedPrinterState(
            data.get("normalized_state", "unknown")
        )
        for key in (
            "source_timestamp",
            "observed_at",
            "print_started_at",
            "print_finished_at",
            "expected_finished_at",
        ):
            data[key] = _datetime(data.get(key))
        data["ams_units"] = tuple(
            AmsUnitState(
                **{
                    **{key: item for key, item in dict(unit).items() if key != "trays"},
                    "trays": tuple(
                        AmsTrayState(**dict(tray))
                        for tray in dict(unit).get("trays", ())
                    ),
                }
            )
            for unit in data.get("ams_units", ())
            if isinstance(unit, dict)
        )
        data["provenance"] = {
            key: ValueProvenance(item)
            for key, item in dict(data.get("provenance", {})).items()
        }
        return cls(**data)


@dataclass(frozen=True, slots=True)
class PrintSession:
    session_id: str
    printer_id: str
    job_id: str | None
    job_name: str | None
    started_at: datetime
    start_provenance: ValueProvenance
    ended_at: datetime | None
    end_provenance: ValueProvenance
    result: str | None
    material: str | None
    material_provenance: ValueProvenance
    active_tool: str | None
    ams_slot: str | None
    source: str
    updated_at: datetime

    @property
    def active(self) -> bool:
        return self.ended_at is None

    @property
    def duration_seconds(self) -> int | None:
        if self.ended_at is None:
            return None
        return max(0, round((self.ended_at - self.started_at).total_seconds()))

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["started_at"] = _iso(self.started_at)
        result["ended_at"] = _iso(self.ended_at)
        result["updated_at"] = _iso(self.updated_at)
        result["material_provenance"] = self.material_provenance.value
        result["start_provenance"] = self.start_provenance.value
        result["end_provenance"] = self.end_provenance.value
        result["active"] = self.active
        result["duration_seconds"] = self.duration_seconds
        return result


@dataclass(frozen=True, slots=True)
class PrinterPoint:
    measurement: str
    tags: dict[str, str]
    fields: dict[str, bool | float | int | str]
    timestamp: datetime


def printer_state_point(state: PrinterState, *, measurement: str) -> PrinterPoint:
    """Create a cardinality-safe point; job/material text is always a field."""

    fields: dict[str, bool | float | int | str] = {
        "online": state.online,
        "normalized_state": state.normalized_state.value,
        "printer_is_printing": state.printer_is_printing,
        "session_active": state.session_active,
    }
    optional: dict[str, Any] = {
        "unavailable_reason": state.unavailable_reason,
        "current_stage": state.current_stage,
        "job_id": state.job_id,
        "job_name": state.job_name,
        "progress_percent": state.progress_percent,
        "remaining_seconds": state.remaining_seconds,
        "current_layer": state.current_layer,
        "total_layers": state.total_layers,
        "print_started_at": _iso(state.print_started_at),
        "print_finished_at": _iso(state.print_finished_at),
        "expected_finished_at": _iso(state.expected_finished_at),
        "nozzle_1_temperature": state.nozzle_1_temperature,
        "nozzle_1_target": state.nozzle_1_target,
        "nozzle_2_temperature": state.nozzle_2_temperature,
        "nozzle_2_target": state.nozzle_2_target,
        "bed_temperature": state.bed_temperature,
        "bed_target": state.bed_target,
        "chamber_temperature": state.chamber_temperature,
        "chamber_target": state.chamber_target,
        "active_tool": state.active_tool,
        "active_material": state.active_material,
        "active_filament": state.active_filament,
        "ams_state": state.ams_state,
        "ams_slot": state.ams_slot,
        "print_source": state.print_source,
        "firmware_version": state.firmware_version,
        "gcode_filename": state.gcode_filename,
        "print_bed_type": state.print_bed_type,
        "print_weight_grams": state.print_weight_grams,
        "print_length_meters": state.print_length_meters,
        "nozzle_1_type": state.nozzle_1_type,
        "nozzle_1_size_mm": state.nozzle_1_size_mm,
        "nozzle_2_type": state.nozzle_2_type,
        "nozzle_2_size_mm": state.nozzle_2_size_mm,
        "cooling_fan_percent": state.cooling_fan_percent,
        "auxiliary_fan_percent": state.auxiliary_fan_percent,
        "chamber_fan_percent": state.chamber_fan_percent,
        "heatbreak_fan_percent": state.heatbreak_fan_percent,
        "wifi_signal_dbm": state.wifi_signal_dbm,
        "mqtt_connection_mode": state.mqtt_connection_mode,
        "mqtt_encryption": state.mqtt_encryption,
        "hybrid_mqtt_control_blocked": state.hybrid_mqtt_control_blocked,
        "developer_lan_mode": state.developer_lan_mode,
        "ha_bambulab_estimated_usage_hours": state.ha_bambulab_estimated_usage_hours,
        "printer_reported_lifetime_hours": state.printer_reported_lifetime_hours,
        "cover_available": state.cover_available,
        "ams_inventory_json": json.dumps(
            [
                {
                    **asdict(unit),
                    "trays": [asdict(tray) for tray in unit.trays],
                }
                for unit in state.ams_units
            ],
            sort_keys=True,
        ),
        "source_timestamp": _iso(state.source_timestamp),
        "material_provenance": state.provenance.get(
            "active_material", ValueProvenance.UNKNOWN
        ).value,
    }
    fields.update({key: value for key, value in optional.items() if value is not None})
    return PrinterPoint(
        measurement,
        {
            "printer_id": state.printer_id,
            "printer_model": state.printer_model,
            "source": state.source,
        },
        fields,
        state.observed_at,
    )


def print_session_point(session: PrintSession) -> PrinterPoint:
    fields: dict[str, bool | float | int | str] = {
        "active": session.active,
        "started_at": _iso(session.started_at) or "",
        "start_provenance": session.start_provenance.value,
        "end_provenance": session.end_provenance.value,
        "material_provenance": session.material_provenance.value,
    }
    optional: dict[str, Any] = {
        "job_id": session.job_id,
        "job_name": session.job_name,
        "ended_at": _iso(session.ended_at),
        "duration_seconds": session.duration_seconds,
        "result": session.result,
        "material": session.material,
        "active_tool": session.active_tool,
        "ams_slot": session.ams_slot,
    }
    fields.update({key: value for key, value in optional.items() if value is not None})
    return PrinterPoint(
        "print_session",
        {
            "printer_id": session.printer_id,
            "source": session.source,
        },
        {"session_id": session.session_id, **fields},
        session.started_at,
    )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
