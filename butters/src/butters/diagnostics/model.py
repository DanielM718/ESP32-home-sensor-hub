"""Machine-readable diagnostic request, finding, and assessment types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from butters.diagnostics.evidence import EvidenceBundle


class DiagnosticDomain(str, Enum):
    SENSOR = "sensor"
    SENSOR_PIPELINE = "sensor_pipeline"
    GRAFANA = "grafana"
    HOME_ASSISTANT = "home_assistant"
    MQTT = "mqtt"
    INFLUXDB = "influxdb"
    SERVER = "server"
    NETWORK = "network"
    KR260 = "kr260"
    MONITORING_STACK = "monitoring_stack"
    UNKNOWN = "unknown"


class DiagnosticStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class FindingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RequestDepth(str, Enum):
    NORMAL = "normal"
    DETAILED = "detailed"
    EXHAUSTIVE = "exhaustive"


@dataclass(frozen=True, slots=True)
class RequestComplexity:
    diagnostic_language: bool = False
    historical_comparison: bool = False
    systems_referenced: tuple[str, ...] = ()
    root_cause_requested: bool = False
    explicit_deep_analysis: bool = False


@dataclass(frozen=True, slots=True)
class ObservationComplexity:
    known_playbook_matched: bool
    contradictory_evidence: bool = False
    unresolved_causes: int = 0
    unfamiliar_errors: bool = False
    missing_or_stale_evidence: bool = False
    systems_implicated: tuple[str, ...] = ()
    logs_need_interpretation: bool = False


@dataclass(frozen=True, slots=True)
class DiagnosticRequest:
    text: str
    domain: DiagnosticDomain = DiagnosticDomain.UNKNOWN
    target: str | None = None
    depth: RequestDepth = RequestDepth.NORMAL
    local_only: bool = False
    allow_cloud: bool = False
    max_escalation: int = 3
    complexity: RequestComplexity = field(default_factory=RequestComplexity)


@dataclass(frozen=True, slots=True)
class DiagnosticFinding:
    code: str
    severity: FindingSeverity
    summary: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticAssessment:
    domain: DiagnosticDomain
    status: DiagnosticStatus
    confidence: Confidence
    findings: tuple[DiagnosticFinding, ...]
    evidence: EvidenceBundle
    root_cause: str | None = None
    hypotheses: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    recommended_next_steps: tuple[str, ...] = ()
    escalation_required: bool = False
    escalation_reason: str | None = None
    observation_complexity: ObservationComplexity | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticAnswer:
    request: DiagnosticRequest
    assessment: DiagnosticAssessment
    route: str
    playbook: str
    concise_voice_text: str
    detailed_text: str
    cloud_used: bool = False
    cloud_model: str | None = None
    cloud_reasoning: str | None = None
    tool_calls: int = 0
    estimated_cost_usd: float = 0.0
    stopping_reason: str = "local_complete"

