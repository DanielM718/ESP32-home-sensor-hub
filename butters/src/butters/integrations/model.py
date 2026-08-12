"""Typed integration boundary models; no credentials or client APIs escape here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class IntegrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SensorRecord:
    sensor_type: str
    source_id: str
    last_seen: str | None
    age_seconds: int | None
    status: str
    values: dict[str, object]
    available_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SensorSnapshot:
    generated_at: str
    records: tuple[SensorRecord, ...]

    def find(self, sensor_type: str, source_id: str) -> SensorRecord | None:
        return next(
            (
                record
                for record in self.records
                if record.sensor_type == sensor_type and record.source_id == source_id
            ),
            None,
        )


class SensorSnapshotProvider(Protocol):
    def snapshot(self) -> SensorSnapshot: ...


@dataclass(frozen=True, slots=True)
class PrinterSnapshot:
    printer_id: str
    printer_model: str
    online: bool
    normalized_state: str
    observed_at: str | None
    values: dict[str, object]
    provenance: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PrintEnvironmentSnapshot:
    available: bool
    reason: str | None
    observational: bool
    session: dict[str, object]
    metrics: dict[str, dict[str, float | int | None]]
    voc_recovery_seconds: int | None


@dataclass(frozen=True, slots=True)
class PrinterIntelligenceSnapshot:
    usage: dict[str, object]
    maintenance_tasks: tuple[dict[str, object], ...]
    completion_history: tuple[dict[str, object], ...]
    print_history: tuple[dict[str, object], ...]


class PrinterSnapshotProvider(Protocol):
    def current(self) -> PrinterSnapshot: ...

    def environment_summary(self) -> PrintEnvironmentSnapshot: ...

    def intelligence(self) -> PrinterIntelligenceSnapshot: ...


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    name: str
    unit: str
    active: bool
    state: str


@dataclass(frozen=True, slots=True)
class ServerHealthSnapshot:
    uptime_seconds: float | None
    load_1m: float
    load_5m: float
    load_15m: float
    available_memory_bytes: int | None
    swap_used_bytes: int | None
    disk_free_bytes: int | None
    disk_total_bytes: int | None
    temperature_c: float | None
    throttled: str | None
    services: tuple[ServiceHealth, ...] = field(default_factory=tuple)


class ServerHealthProvider(Protocol):
    def snapshot(self) -> ServerHealthSnapshot: ...
