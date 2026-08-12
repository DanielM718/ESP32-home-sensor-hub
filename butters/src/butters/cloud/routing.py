"""Two-phase evidence-aware escalation selection."""

from __future__ import annotations

from butters.assistant_config import CloudSettings
from butters.cloud.model import EscalationLevel, ReasoningConfiguration
from butters.diagnostics.model import (
    Confidence,
    DiagnosticAssessment,
    DiagnosticRequest,
    RequestDepth,
)


class EscalationPolicy:
    def __init__(self, settings: CloudSettings) -> None:
        self.settings = settings

    def initial(
        self, request: DiagnosticRequest, local: DiagnosticAssessment
    ) -> ReasoningConfiguration:
        complexity = local.observation_complexity
        if (
            request.depth is RequestDepth.EXHAUSTIVE
            and request.max_escalation >= EscalationLevel.MAXIMUM
        ):
            return ReasoningConfiguration(
                EscalationLevel.MAXIMUM, self.settings.sol_model, "max", True
            )
        if request.max_escalation == EscalationLevel.LIGHT:
            return ReasoningConfiguration(
                EscalationLevel.LIGHT, self.settings.luna_model, "medium"
            )
        if request.max_escalation >= EscalationLevel.DEEP and complexity and (
            complexity.contradictory_evidence
            or complexity.unfamiliar_errors
            or complexity.logs_need_interpretation
            or complexity.unresolved_causes >= 3
        ):
            return ReasoningConfiguration(
                EscalationLevel.DEEP, self.settings.sol_model, "xhigh"
            )
        # Luna is used only when the caller explicitly caps the investigation
        # at level 1; it is not an extra hop on the default diagnostic route.
        if request.max_escalation >= EscalationLevel.ANALYSIS:
            return ReasoningConfiguration(
                EscalationLevel.ANALYSIS, self.settings.terra_model, "high"
            )
        return ReasoningConfiguration(
            EscalationLevel.LIGHT, self.settings.luna_model, "medium"
        )

    def next(
        self,
        current: ReasoningConfiguration,
        request: DiagnosticRequest,
        conclusion: Confidence,
    ) -> ReasoningConfiguration | None:
        if conclusion not in {Confidence.LOW, Confidence.INSUFFICIENT}:
            return None
        if current.level <= EscalationLevel.ANALYSIS and request.max_escalation >= 3:
            return ReasoningConfiguration(
                EscalationLevel.DEEP, self.settings.sol_model, "xhigh"
            )
        maximum_allowed = (
            request.depth is RequestDepth.EXHAUSTIVE
            or self.settings.allow_automatic_maximum
        )
        if (
            current.level == EscalationLevel.DEEP
            and request.max_escalation >= 4
            and maximum_allowed
        ):
            return ReasoningConfiguration(
                EscalationLevel.MAXIMUM, self.settings.sol_model, "max", True
            )
        return None
