"""Bounded cloud diagnostic tool loop with local policy enforcement."""

from __future__ import annotations

import json
import time

from butters.assistant_config import CloudSettings
from butters.cloud.model import (
    CloudBudget,
    CloudConclusion,
    CloudReasoner,
    CloudReasonerError,
    CloudTokenUsage,
    ReasoningConfiguration,
)
from butters.cloud.routing import EscalationPolicy
from butters.cloud.usage import UsageLedger
from butters.diagnostics.engine import _detailed_text, _voice_text
from butters.diagnostics.model import (
    DiagnosticAnswer,
    DiagnosticAssessment,
    DiagnosticFinding,
    DiagnosticRequest,
    FindingSeverity,
)
from butters.diagnostics.session import DiagnosticSession
from butters.diagnostics.tools import DiagnosticToolRegistry


class CloudDiagnosticEscalator:
    def __init__(
        self,
        reasoner: CloudReasoner,
        tools: DiagnosticToolRegistry,
        settings: CloudSettings,
        *,
        policy: EscalationPolicy | None = None,
        ledger: UsageLedger | None = None,
        clock: callable = time.perf_counter,
    ) -> None:
        self.reasoner = reasoner
        self.tools = tools
        self.settings = settings
        self.policy = policy or EscalationPolicy(settings)
        self.ledger = ledger or UsageLedger(settings)
        self.clock = clock

    def escalate(
        self,
        request: DiagnosticRequest,
        local_assessment: DiagnosticAssessment,
        session: DiagnosticSession,
    ) -> DiagnosticAnswer:
        if request.max_escalation <= 0:
            return _local_fallback(request, local_assessment, session, "maximum_escalation_local_only")
        if not self.settings.enabled or not self.settings.allow_paid_calls:
            return _local_fallback(request, local_assessment, session, "cloud_disabled")
        configuration = self.policy.initial(request, local_assessment)
        started = self.clock()
        escalation_steps = 0
        total_calls = 0
        total_rounds = 0
        previous_response_id: str | None = None
        function_outputs: list[dict[str, object]] = []
        while configuration is not None:
            escalation_steps += 1
            if escalation_steps > self.settings.max_escalation_steps:
                return _local_fallback(request, local_assessment, session, "escalation_step_limit")
            session.escalation_history.append(
                f"level={int(configuration.level)} model={configuration.model} effort={configuration.effort}"
            )
            estimate = self.ledger.conservative_request_estimate(
                configuration.model,
                session.evidence.encoded_bytes,
                self.settings.max_output_tokens,
            )
            if not self.ledger.permits(estimate):
                return _local_fallback(request, local_assessment, session, "cloud_budget_exceeded")
            conclusion: CloudConclusion | None = None
            for round_index in range(self.settings.max_tool_rounds + 1):
                if total_rounds >= self.settings.max_cloud_requests_per_diagnostic:
                    return _local_fallback(request, local_assessment, session, "cloud_request_limit")
                if self.clock() - started > self.settings.max_wall_seconds:
                    return _local_fallback(request, local_assessment, session, "cloud_wall_time_limit")
                total_rounds += 1
                budget = CloudBudget(
                    configuration,
                    self.settings.max_output_tokens,
                    self.settings.max_estimated_cost_per_request_usd,
                    max(0.0, self.settings.max_wall_seconds - (self.clock() - started)),
                )
                context: dict[str, object] = {
                    "local_status": local_assessment.status.value,
                    "local_confidence": local_assessment.confidence.value,
                    "local_escalation_reason": local_assessment.escalation_reason,
                    "completed_tools": [name for name, _args in session.completed_tools],
                }
                if previous_response_id:
                    context["previous_response_id"] = previous_response_id
                    context["function_call_outputs"] = function_outputs
                try:
                    turn = self.reasoner.analyze(
                        request,
                        session.evidence,
                        _relevant_tools(self.tools, request),
                        context,
                        budget,
                    )
                except CloudReasonerError as exc:
                    self.ledger.record(
                        request.domain.value,
                        configuration,
                        CloudTokenUsage(),
                        tool_rounds=total_rounds,
                        wall_seconds=self.clock() - started,
                        success=False,
                        escalation_occurred=escalation_steps > 1,
                        error_code=exc.code,
                        estimated_cost_override=estimate,
                    )
                    return _local_fallback(request, local_assessment, session, exc.code)
                session.input_tokens += turn.usage.input_tokens
                session.cached_tokens += turn.usage.cached_tokens
                session.output_tokens += turn.usage.output_tokens
                session.reasoning_tokens += turn.usage.reasoning_tokens
                usage_record = self.ledger.record(
                    request.domain.value,
                    configuration,
                    turn.usage,
                    tool_rounds=round_index,
                    wall_seconds=turn.elapsed_seconds,
                    success=True,
                    escalation_occurred=escalation_steps > 1,
                )
                session.estimated_cost_usd += usage_record.estimated_cost_usd
                previous_response_id = turn.response_id
                if turn.conclusion is not None:
                    conclusion = turn.conclusion
                    break
                if round_index >= self.settings.max_tool_rounds:
                    return _local_fallback(request, local_assessment, session, "tool_round_limit")
                tool_request = turn.tool_requests[0]
                total_calls += 1
                if total_calls > self.settings.max_total_tool_calls:
                    return _local_fallback(request, local_assessment, session, "tool_call_limit")
                canonical = json.dumps(tool_request.arguments, sort_keys=True, separators=(",", ":"))
                if not session.remember_tool(tool_request.name, canonical):
                    return _local_fallback(request, local_assessment, session, "repeated_tool_call")
                failure = self.tools.validate(tool_request.name, tool_request.arguments)
                if failure is not None:
                    return _local_fallback(request, local_assessment, session, f"tool_policy_{failure}")
                execution = self.tools.execute(tool_request.name, tool_request.arguments)
                try:
                    session.evidence = session.evidence.add(execution.evidence)
                except ValueError:
                    return _local_fallback(request, local_assessment, session, "evidence_budget_exhausted")
                function_outputs = [
                    {
                        "type": "function_call_output",
                        "call_id": tool_request.call_id,
                        "output": json.dumps(execution.evidence.as_dict(), separators=(",", ":")),
                    }
                ]
            if conclusion is None:
                return _local_fallback(request, local_assessment, session, "cloud_no_conclusion")
            invalid_ids = set(conclusion.evidence_ids) - {item.evidence_id for item in session.evidence.items}
            if invalid_ids:
                return _local_fallback(request, local_assessment, session, "unsupported_evidence_reference")
            next_configuration = self.policy.next(configuration, request, conclusion.confidence)
            if conclusion.escalation_needed and next_configuration is not None:
                configuration = next_configuration
                previous_response_id = None
                function_outputs = []
                continue
            assessment = _assessment_from_conclusion(local_assessment, session, conclusion)
            return DiagnosticAnswer(
                request,
                assessment,
                "cloud_escalation",
                "cloud_tool_loop",
                conclusion.concise_voice_text,
                conclusion.detailed_text,
                cloud_used=True,
                cloud_model=configuration.model,
                cloud_reasoning=configuration.effort + ("+pro" if configuration.pro_mode else ""),
                tool_calls=total_calls,
                estimated_cost_usd=session.estimated_cost_usd,
                stopping_reason="cloud_complete",
            )
        return _local_fallback(request, local_assessment, session, "cloud_exhausted")


def _relevant_tools(
    registry: DiagnosticToolRegistry, request: DiagnosticRequest
) -> tuple[dict[str, object], ...]:
    by_domain: dict[str, set[str]] = {
        "sensor": {"get_sensor_status", "get_sensor_last_seen", "get_sensor_history_summary", "get_mqtt_health", "inspect_allowlisted_mqtt_topic", "get_bridge_health"},
        "sensor_pipeline": {"get_sensor_status", "get_sensor_history_summary", "get_mqtt_health", "inspect_allowlisted_mqtt_topic", "get_bridge_health", "get_influx_health", "get_dashboard_health", "read_service_logs"},
        "grafana": {"get_sensor_status", "get_sensor_history_summary", "get_influx_health", "get_grafana_health", "get_dashboard_health", "read_service_logs"},
        "home_assistant": {"get_sensor_status", "get_home_assistant_health", "get_container_status", "get_container_health", "read_container_logs"},
        "mqtt": {"get_mqtt_health", "inspect_allowlisted_mqtt_topic", "get_bridge_health", "read_service_logs"},
        "influxdb": {"get_influx_health", "get_bridge_health", "get_dashboard_health", "read_service_logs"},
        "server": {"get_server_health", "get_load", "get_memory_status", "get_swap_status", "get_disk_status", "get_temperature", "get_throttle_status", "get_service_status", "get_service_summary", "get_failed_units", "read_service_logs"},
        "network": {"get_network_interfaces", "get_route_summary", "resolve_host", "ping_allowlisted_host", "check_tcp_port", "get_tailscale_status", "get_local_listeners"},
        "kr260": {"run_kr260_diagnostic"},
        "monitoring_stack": {"get_sensor_status", "get_mqtt_health", "get_bridge_health", "get_influx_health", "get_dashboard_health", "get_grafana_health", "get_service_status", "read_service_logs"},
    }
    relevant = by_domain.get(request.domain.value, set())
    # Keep the provider prompt small and never expose unrelated tools.
    return tuple(spec.as_model_tool() for spec in registry.tools if spec.name in relevant)


def _assessment_from_conclusion(
    local: DiagnosticAssessment,
    session: DiagnosticSession,
    conclusion: CloudConclusion,
) -> DiagnosticAssessment:
    findings = tuple(
        DiagnosticFinding(
            f"cloud_finding_{index + 1}",
            FindingSeverity.ERROR if conclusion.status.value == "failed" else FindingSeverity.WARNING,
            finding,
            conclusion.evidence_ids,
        )
        for index, finding in enumerate(conclusion.findings)
    )
    return DiagnosticAssessment(
        local.domain,
        conclusion.status,
        conclusion.confidence,
        findings,
        session.evidence,
        root_cause=conclusion.root_cause,
        hypotheses=conclusion.hypotheses,
        unresolved_questions=conclusion.unresolved_questions,
        recommended_next_steps=conclusion.recommended_next_steps,
        escalation_required=conclusion.escalation_needed,
        escalation_reason="cloud reasoner requested a higher tier" if conclusion.escalation_needed else None,
        observation_complexity=local.observation_complexity,
    )


def _local_fallback(
    request: DiagnosticRequest,
    assessment: DiagnosticAssessment,
    session: DiagnosticSession,
    reason: str,
) -> DiagnosticAnswer:
    return DiagnosticAnswer(
        request,
        assessment,
        "local_fallback",
        "best_local_evidence",
        _voice_text(assessment),
        _detailed_text(assessment),
        cloud_used=False,
        tool_calls=len(session.completed_tools),
        estimated_cost_usd=session.estimated_cost_usd,
        stopping_reason=reason,
    )
