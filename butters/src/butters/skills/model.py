"""Skill contracts and typed arguments/results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias

from butters.integrations.model import (
    PrintEnvironmentSnapshot,
    PrinterSnapshot,
    ServerHealthSnapshot,
)


class ActionClass(str, Enum):
    READ_ONLY = "read_only"
    CONTROL = "control"
    DISRUPTIVE = "disruptive"


class SkillError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SensorValueArgs:
    entity: str
    metric: str


@dataclass(frozen=True, slots=True)
class SensorStatusArgs:
    entity: str | None


@dataclass(frozen=True, slots=True)
class SensorLastSeenArgs:
    entity: str


@dataclass(frozen=True, slots=True)
class ComparisonArgs:
    group: str
    metric: str
    operation: str


@dataclass(frozen=True, slots=True)
class AirQualityArgs:
    entity: str


@dataclass(frozen=True, slots=True)
class ServerHealthArgs:
    pass


@dataclass(frozen=True, slots=True)
class PrinterArgs:
    entity: str


SkillArguments: TypeAlias = (
    SensorValueArgs
    | SensorStatusArgs
    | SensorLastSeenArgs
    | ComparisonArgs
    | AirQualityArgs
    | ServerHealthArgs
    | PrinterArgs
)


@dataclass(frozen=True, slots=True)
class SensorValueResult:
    entity: str
    display_name: str
    metric: str
    metric_name: str
    value: float | int | None
    unit: str
    timestamp: str | None
    age_seconds: int | None
    status: str
    available: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EntityStatusResult:
    entity: str
    display_name: str
    status: str
    timestamp: str | None
    age_seconds: int | None


@dataclass(frozen=True, slots=True)
class SensorStatusResult:
    entities: tuple[EntityStatusResult, ...]
    all_reporting: bool
    reporting_count: int
    configured_count: int


@dataclass(frozen=True, slots=True)
class SensorLastSeenResult:
    entity: str
    display_name: str
    status: str
    timestamp: str | None
    age_seconds: int | None


@dataclass(frozen=True, slots=True)
class ComparisonMissing:
    entity: str
    display_name: str
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    group: str
    metric: str
    operation: str
    entity: str | None
    display_name: str | None
    value: float | int | None
    unit: str
    timestamp: str | None
    age_seconds: int | None
    considered_count: int
    missing: tuple[ComparisonMissing, ...] = ()


@dataclass(frozen=True, slots=True)
class AirQualityResult:
    entity: str
    display_name: str
    status: str
    timestamp: str | None
    age_seconds: int | None
    measurements: dict[str, float | int | None]
    summary_category: str | None
    summary_severity: str | None
    driving_metric: str | None


@dataclass(frozen=True, slots=True)
class ServerHealthResult:
    health: ServerHealthSnapshot


@dataclass(frozen=True, slots=True)
class PrinterStatusResult:
    printer: PrinterSnapshot


@dataclass(frozen=True, slots=True)
class CurrentPrintResult:
    printer: PrinterSnapshot


@dataclass(frozen=True, slots=True)
class PrinterTemperaturesResult:
    printer: PrinterSnapshot


@dataclass(frozen=True, slots=True)
class PrintEnvironmentResult:
    summary: PrintEnvironmentSnapshot


SkillResult: TypeAlias = (
    SensorValueResult
    | SensorStatusResult
    | SensorLastSeenResult
    | ComparisonResult
    | AirQualityResult
    | ServerHealthResult
    | PrinterStatusResult
    | CurrentPrintResult
    | PrinterTemperaturesResult
    | PrintEnvironmentResult
)


@dataclass(frozen=True, slots=True)
class SkillFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SkillExecution:
    skill: str
    action_class: ActionClass | None
    elapsed_seconds: float
    result: SkillResult | None = None
    failure: SkillFailure | None = None
    arguments: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.result is not None and self.failure is None
