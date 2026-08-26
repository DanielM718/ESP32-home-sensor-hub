"""Skill contracts and typed arguments/results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias

from butters.integrations.model import (
    PrintEnvironmentSnapshot,
    PrinterIntelligenceSnapshot,
    PrinterSnapshot,
    ServerHealthSnapshot,
)


class ActionClass(str, Enum):
    READ_ONLY = "read_only"
    ANALYTICAL = "analytical"
    ACTION = "action"
    # Retained for compatibility with older persisted metadata.  Neither class
    # is enabled by the v2 conversational registry.
    CONTROL = "control"
    DISRUPTIVE = "disruptive"


class AuthenticationLevel(str, Enum):
    """Authentication strength required independently of user intent."""

    NONE = "none"
    LOCAL_CONSOLE = "local_console"
    ELEVATED = "elevated"
    FRESH = "fresh"


@dataclass(frozen=True, slots=True)
class AuthenticationContext:
    level: AuthenticationLevel
    session_id: str
    identity: str
    expires_at: float
    method: str
    action_digest: str | None = None

    def valid_for(
        self,
        *,
        session_id: str,
        identity: str,
        now: float,
        action_digest: str | None = None,
    ) -> bool:
        return (
            self.session_id == session_id
            and self.identity == identity
            and now < self.expires_at
            and (
                self.level is not AuthenticationLevel.FRESH
                or (action_digest is not None and self.action_digest == action_digest)
            )
        )


class SkillAudience(str, Enum):
    """Who may invoke a skill, independent of what the skill is allowed to do.

    A skill can be strictly read-only and still expose deployment internals
    (repository state, listener inventory) that the ordinary conversation
    surface must never reveal. Audience is declared on the SkillSpec so the
    registry enforces it once, rather than each caller re-deriving it.
    """

    NORMAL = "normal"
    ADMINISTRATOR = "administrator"


class SkillError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SensorValueArgs:
    entity: str
    metric: str


@dataclass(frozen=True, slots=True)
class SensorValuesArgs:
    """One entity with an ordered set of requested measurements."""

    entity: str
    metrics: tuple[str, ...]


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


@dataclass(frozen=True, slots=True)
class HostObservationArgs:
    metric: str


@dataclass(frozen=True, slots=True)
class StackObservationArgs:
    component: str


@dataclass(frozen=True, slots=True)
class NetworkObservationArgs:
    view: str


@dataclass(frozen=True, slots=True)
class SensorHistorySummaryArgs:
    entity: str
    range_key: str


@dataclass(frozen=True, slots=True)
class ProjectStatusArgs:
    view: str


@dataclass(frozen=True, slots=True)
class DesktopArgs:
    machine: str


@dataclass(frozen=True, slots=True)
class EnvironmentActionArgs:
    state: str
    duration_minutes: int | None


@dataclass(frozen=True, slots=True)
class NoArguments:
    pass


@dataclass(frozen=True, slots=True)
class SensorHistoryArgs:
    entity: str
    metrics: tuple[str, ...]
    start: str | None
    end: str | None
    lookback: str | None
    bucket: str
    max_points: int


@dataclass(frozen=True, slots=True)
class SensorWindowArgs:
    entity: str
    metrics: tuple[str, ...]
    start: str | None
    end: str | None
    lookback: str | None


@dataclass(frozen=True, slots=True)
class CompareWindowsArgs:
    entity: str
    metrics: tuple[str, ...]
    first_start: str
    first_end: str
    second_start: str
    second_end: str


@dataclass(frozen=True, slots=True)
class SpikeArgs:
    entity: str
    metric: str
    start: str | None
    end: str | None
    lookback: str | None


@dataclass(frozen=True, slots=True)
class CorrelationArgs:
    entity: str
    metric_x: str
    metric_y: str
    start: str | None
    end: str | None
    lookback: str | None


@dataclass(frozen=True, slots=True)
class RecentPrintsArgs:
    entity: str
    limit: int


@dataclass(frozen=True, slots=True)
class PrintDetailsArgs:
    entity: str
    print_id: str


@dataclass(frozen=True, slots=True)
class PrintEnvironmentAnalysisArgs:
    printer: str
    environment: str
    metrics: tuple[str, ...]
    print_selector: str
    baseline_minutes: int | None


SkillArguments: TypeAlias = (
    SensorValueArgs
    | SensorValuesArgs
    | SensorStatusArgs
    | SensorLastSeenArgs
    | ComparisonArgs
    | AirQualityArgs
    | ServerHealthArgs
    | PrinterArgs
    | HostObservationArgs
    | StackObservationArgs
    | NetworkObservationArgs
    | SensorHistorySummaryArgs
    | ProjectStatusArgs
    | DesktopArgs
    | EnvironmentActionArgs
    | NoArguments
    | SensorHistoryArgs
    | SensorWindowArgs
    | CompareWindowsArgs
    | SpikeArgs
    | CorrelationArgs
    | RecentPrintsArgs
    | PrintDetailsArgs
    | PrintEnvironmentAnalysisArgs
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
class SensorValuesResult:
    """Several measurements read from one entity in a single snapshot.

    Each requested measurement keeps its own availability and reason, so a
    partially reporting sensor answers what it has without inventing the rest.
    """

    entity: str
    display_name: str
    status: str
    measurements: tuple[SensorValueResult, ...]


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


@dataclass(frozen=True, slots=True)
class PrinterUsageResult:
    intelligence: PrinterIntelligenceSnapshot


@dataclass(frozen=True, slots=True)
class PrinterMaintenanceResult:
    intelligence: PrinterIntelligenceSnapshot


@dataclass(frozen=True, slots=True)
class PrinterMaintenanceEventsResult:
    events: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class LastPrintResult:
    intelligence: PrinterIntelligenceSnapshot


@dataclass(frozen=True, slots=True)
class ReadOnlyObservationResult:
    name: str
    status: str
    values: dict[str, object]
    text_excerpt: str | None = None
    error_code: str | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class StructuredSkillResult:
    """Bounded semantic result used by v2 read, analytical, and action skills."""

    kind: str
    data: dict[str, object]
    evidence: dict[str, object] = field(default_factory=dict)


SkillResult: TypeAlias = (
    SensorValueResult
    | SensorValuesResult
    | SensorStatusResult
    | SensorLastSeenResult
    | ComparisonResult
    | AirQualityResult
    | ServerHealthResult
    | PrinterStatusResult
    | CurrentPrintResult
    | PrinterTemperaturesResult
    | PrintEnvironmentResult
    | PrinterUsageResult
    | PrinterMaintenanceResult
    | PrinterMaintenanceEventsResult
    | LastPrintResult
    | ReadOnlyObservationResult
    | StructuredSkillResult
)


@dataclass(frozen=True, slots=True)
class ActionAuthorization:
    """Authority derived from the current user turn, never from a model call."""

    allowed_skills: frozenset[str] = frozenset()
    source: str = "none"
    confirmed: bool = False

    def permits(self, skill_name: str) -> bool:
        return skill_name in self.allowed_skills


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
