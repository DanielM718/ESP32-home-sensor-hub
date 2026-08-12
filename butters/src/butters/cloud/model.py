"""Provider-neutral cloud reasoning contracts and typed outputs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum

from butters.diagnostics.evidence import EvidenceBundle
from butters.diagnostics.model import Confidence, DiagnosticRequest, DiagnosticStatus


class EscalationLevel(IntEnum):
    LOCAL = 0
    LIGHT = 1
    ANALYSIS = 2
    DEEP = 3
    MAXIMUM = 4


@dataclass(frozen=True, slots=True)
class ReasoningConfiguration:
    level: EscalationLevel
    model: str
    effort: str
    pro_mode: bool = False


@dataclass(frozen=True, slots=True)
class CloudBudget:
    configuration: ReasoningConfiguration
    max_output_tokens: int
    max_estimated_cost_usd: float
    max_wall_seconds: float


@dataclass(frozen=True, slots=True)
class ToolRequest:
    call_id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class CloudConclusion:
    status: DiagnosticStatus
    confidence: Confidence
    root_cause: str | None
    findings: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    hypotheses: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    recommended_next_steps: tuple[str, ...]
    concise_voice_text: str
    detailed_text: str
    escalation_needed: bool = False


@dataclass(frozen=True, slots=True)
class CloudTokenUsage:
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class CloudTurn:
    model: str
    effort: str
    elapsed_seconds: float
    response_id: str | None = None
    tool_requests: tuple[ToolRequest, ...] = ()
    conclusion: CloudConclusion | None = None
    usage: CloudTokenUsage = field(default_factory=CloudTokenUsage)


class CloudReasonerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CloudReasoner(ABC):
    @abstractmethod
    def analyze(
        self,
        request: DiagnosticRequest,
        evidence: EvidenceBundle,
        available_tools: tuple[dict[str, object], ...],
        diagnostic_context: dict[str, object],
        budget: CloudBudget,
    ) -> CloudTurn:
        """Request analysis/tool calls; never execute tools in the provider."""
