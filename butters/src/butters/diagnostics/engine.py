"""Local-first diagnostic orchestration and explainable answer formatting."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Protocol

from butters.diagnostics.evidence import EvidenceBundle
from butters.diagnostics.model import (
    Confidence,
    DiagnosticAnswer,
    DiagnosticAssessment,
    DiagnosticRequest,
    DiagnosticStatus,
    RequestDepth,
)
from butters.diagnostics.planner import DiagnosticPlanner
from butters.diagnostics.playbooks import LocalDiagnosticRules
from butters.diagnostics.session import DiagnosticSession
from butters.diagnostics.tools import DiagnosticToolRegistry


class CloudEscalator(Protocol):
    def escalate(
        self,
        request: DiagnosticRequest,
        local_assessment: DiagnosticAssessment,
        session: DiagnosticSession,
    ) -> DiagnosticAnswer: ...


class DiagnosticEngine:
    def __init__(
        self,
        planner: DiagnosticPlanner,
        tools: DiagnosticToolRegistry,
        *,
        rules: LocalDiagnosticRules | None = None,
        cloud: CloudEscalator | None = None,
        session_ttl_seconds: float = 900.0,
        max_evidence_bytes: int = 64 * 1024,
    ) -> None:
        self.planner = planner
        self.tools = tools
        self.rules = rules or LocalDiagnosticRules()
        self.cloud = cloud
        self.session_ttl_seconds = session_ttl_seconds
        self.max_evidence_bytes = max_evidence_bytes

    def diagnose(self, request: DiagnosticRequest) -> DiagnosticAnswer:
        plan = self.planner.plan(request)
        session = DiagnosticSession(
            request.text,
            ttl_seconds=self.session_ttl_seconds,
            evidence=EvidenceBundle(max_bytes=self.max_evidence_bytes),
        )
        for invocation in plan.tools:
            canonical = json.dumps(invocation.arguments, sort_keys=True, separators=(",", ":"))
            if not session.remember_tool(invocation.name, canonical):
                session.stopping_reason = "repeated_tool_call"
                break
            execution = self.tools.execute(invocation.name, invocation.arguments)
            try:
                session.evidence = session.evidence.add(execution.evidence)
            except ValueError:
                session.stopping_reason = "evidence_budget_exhausted"
                break
        assessment = self.rules.assess(plan, session.evidence)
        if (
            request.depth in {RequestDepth.DETAILED, RequestDepth.EXHAUSTIVE}
            and request.allow_cloud
            and request.max_escalation > 0
            and not request.local_only
            and not assessment.escalation_required
        ):
            assessment = replace(
                assessment,
                escalation_required=True,
                escalation_reason="the user explicitly requested detailed cloud analysis",
            )
        if (
            assessment.escalation_required
            and request.allow_cloud
            and not request.local_only
            and request.max_escalation > 0
            and self.cloud is not None
        ):
            return self.cloud.escalate(request, assessment, session)
        if assessment.escalation_required and (
            request.local_only or not request.allow_cloud or request.max_escalation <= 0
        ):
            route = "local_insufficient"
            default_stopping = "local_only_best_local_result"
        elif assessment.escalation_required:
            route = "cloud_escalation_pending"
            default_stopping = "cloud_disabled_best_local_result"
        else:
            route = "local_playbook"
            default_stopping = "local_complete"
        stopping = session.stopping_reason or default_stopping
        return DiagnosticAnswer(
            request=request,
            assessment=assessment,
            route=route,
            playbook=plan.playbook,
            concise_voice_text=_voice_text(assessment),
            detailed_text=_detailed_text(assessment),
            cloud_used=False,
            tool_calls=len(session.completed_tools),
            stopping_reason=stopping,
        )


def _voice_text(assessment: DiagnosticAssessment) -> str:
    if assessment.root_cause:
        if assessment.confidence in {Confidence.CONFIRMED, Confidence.HIGH}:
            return assessment.root_cause.rstrip(".") + ". No changes were made."
        return "The most likely issue is " + assessment.root_cause.rstrip(".").lower() + ". No changes were made."
    if assessment.status is DiagnosticStatus.HEALTHY and assessment.findings:
        return assessment.findings[0].summary.rstrip(".") + "."
    if assessment.confidence is Confidence.INSUFFICIENT:
        return "I collected the available read-only evidence, but it is not enough for a supported diagnosis."
    return "The diagnostic completed without a confirmed root cause."


def _detailed_text(assessment: DiagnosticAssessment) -> str:
    observed = []
    for item in assessment.evidence.items:
        summary = _evidence_summary(item.values)
        observed.append(f"[{item.evidence_id}] {item.status.value}: {summary}")
    concluded = assessment.root_cause or (
        assessment.findings[0].summary if assessment.findings else "No supported conclusion."
    )
    possible = "; ".join(assessment.hypotheses) or "No additional unsupported cause is asserted."
    recommended = "; ".join(assessment.recommended_next_steps) or "No next step was identified."
    return (
        "OBSERVED\n- " + ("\n- ".join(observed) if observed else "No evidence was collected.")
        + f"\n\nCONCLUDED\n{concluded} (confidence: {assessment.confidence.value})"
        + f"\n\nPOSSIBLE\n{possible}"
        + f"\n\nRECOMMENDED NEXT STEP\n{recommended}"
    )


def _evidence_summary(values: dict[str, object]) -> str:
    if not values:
        return "no structured values"
    text = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text if len(text) <= 360 else text[:357] + "..."
