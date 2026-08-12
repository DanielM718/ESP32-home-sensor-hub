"""Protocol-independent printer state and InfluxDB point models."""

from __future__ import annotations

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
    nozzle_1_temperature: float | None = None
    nozzle_1_target: float | None = None
    nozzle_2_temperature: float | None = None
    nozzle_2_target: float | None = None
    bed_temperature: float | None = None
    bed_target: float | None = None
    chamber_temperature: float | None = None
    active_tool: str | None = None
    active_material: str | None = None
    active_filament: str | None = None
    ams_state: str | None = None
    ams_slot: str | None = None
    print_source: str | None = None
    firmware_version: str | None = None
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
        ):
            data[key] = _datetime(data.get(key))
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
        "nozzle_1_temperature": state.nozzle_1_temperature,
        "nozzle_1_target": state.nozzle_1_target,
        "nozzle_2_temperature": state.nozzle_2_temperature,
        "nozzle_2_target": state.nozzle_2_target,
        "bed_temperature": state.bed_temperature,
        "bed_target": state.bed_target,
        "chamber_temperature": state.chamber_temperature,
        "active_tool": state.active_tool,
        "active_material": state.active_material,
        "active_filament": state.active_filament,
        "ams_state": state.ams_state,
        "ams_slot": state.ams_slot,
        "print_source": state.print_source,
        "firmware_version": state.firmware_version,
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
