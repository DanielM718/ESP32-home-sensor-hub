from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.cloud.general import GeneralCloudTurn
from butters.cloud.model import CloudTokenUsage
from butters.diagnostics.evidence import EvidenceBundle
from butters.diagnostics.model import (
    Confidence,
    DiagnosticAnswer,
    DiagnosticAssessment,
    DiagnosticDomain,
    DiagnosticRequest,
    DiagnosticStatus,
)
from butters.integrations.model import SensorRecord, SensorSnapshot, ServerHealthSnapshot
from butters.routing.entities import EntityRegistry
from butters.routing.model import RoutedIntent
from butters.stt.normalization import DomainVocabulary
from butters.web.service import BetaAssistantService, RouteOverride


class Sensors:
    def snapshot(self) -> SensorSnapshot:
        return SensorSnapshot(
            "2026-08-12T12:00:00Z",
            (
                SensorRecord("environment", "1", "2026-08-12T11:59:55Z", 5, "online", {"humidity": 21.0}),
                SensorRecord("environment", "2", "2026-08-12T11:59:55Z", 5, "online", {"humidity": 31.0}),
                SensorRecord("environment", "3", "2026-08-12T11:59:55Z", 5, "online", {"humidity": 42.0}),
            ),
        )


class Health:
    def snapshot(self) -> ServerHealthSnapshot:
        return ServerHealthSnapshot(100, 0.1, 0.1, 0.1, 1_000_000, 0, 1_000_000, 2_000_000, 45.0, "0x0", ())


class General:
    def __init__(self, available: bool = False) -> None:
        self._available = available
        self.calls = 0
        self.last_call = None

    @property
    def available(self) -> bool:
        return self._available

    def reason(self, **kwargs):
        self.calls += 1
        self.last_call = kwargs
        return GeneralCloudTurn(
            kwargs["model"],
            kwargs["effort"],
            0.01,
            response_id="response_safe_id",
            response_text="Box three is observed to be more humid; airflow and material exposure are hypotheses.",
            usage=CloudTokenUsage(input_tokens=100, output_tokens=20, reasoning_tokens=5),
        )


def _service(tmp_path: Path, *, cloud: bool = False, budget: float = 0.5):
    base = load_assistant_settings()
    settings = replace(
        base,
        cloud=replace(base.cloud, enabled=cloud, allow_paid_calls=cloud, max_estimated_cost_per_request_usd=budget),
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(base.web, state_dir=tmp_path, development_mode=True),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health())
    reasoner = General(cloud)
    return BetaAssistantService(settings, vocabulary, assistant=assistant, general_reasoner=reasoner, state_dir=tmp_path), reasoner


def test_deterministic_request_is_model_free_and_traced(tmp_path: Path) -> None:
    service, reasoner = _service(tmp_path)
    session = service.sessions.create()

    response = service.handle_text(session, "What's the humidity in filament box three?")

    assert response.route == "deterministic"
    assert response.skill == "get_sensor_value"
    assert "42" in response.response_text
    assert reasoner.calls == 0
    trace = service.traces.get(response.trace_id)
    assert trace is not None
    assert any(item.reason_code == "deterministic_skill_match" for item in trace.events)
    assert not any(item.stage == "model" and item.status == "started" for item in trace.events)


def test_cloud_disabled_override_fails_closed(tmp_path: Path) -> None:
    service, reasoner = _service(tmp_path, cloud=True)
    session = service.sessions.create()

    response = service.handle_text(
        session,
        "Explain an unrelated open ended question",
        override=RouteOverride.CLOUD_DISABLED,
        administrator=True,
    )

    assert response.stopping_reason is None or "cloud" in " ".join(response.reason_codes)
    assert reasoner.calls == 0


def test_admin_forced_cloud_reports_actual_usage_without_persisting_prompt(tmp_path: Path) -> None:
    service, reasoner = _service(tmp_path, cloud=True)
    session = service.sessions.create()

    response = service.handle_text(
        session,
        "Why might box three stay more humid than the others?",
        override=RouteOverride.FORCE_CLOUD_MODEL,
        forced_model=service.settings.cloud.terra_model,
        reasoning_effort="high",
        administrator=True,
    )

    assert response.route == "general_cloud"
    assert response.model == service.settings.cloud.terra_model
    assert response.usage and response.usage["input_tokens"] == 100
    assert reasoner.calls == 1
    assert reasoner.last_call is not None
    context = reasoner.last_call["context"]
    assert any("BOUNDED LOCAL OBSERVATIONS" in item["content"] for item in context)
    assert any(item["name"] == "compare_sensor_metric" for item in reasoner.last_call["tools"])
    assert b"Why might box three" not in (tmp_path / "usage.sqlite3").read_bytes()


def test_open_ended_causal_auto_route_collects_local_evidence_then_cloud(tmp_path: Path) -> None:
    service, reasoner = _service(tmp_path, cloud=True)
    session = service.sessions.create()

    response = service.handle_text(session, "Why might box three stay more humid than the others?")

    assert response.route == "general_cloud"
    assert response.reason_codes == ("open_ended_reasoning_required",)
    assert reasoner.calls == 1
    trace = service.traces.get(response.trace_id)
    assert trace is not None
    assert any(item.status == "prefetch_complete" for item in trace.events)


def test_budget_denial_prevents_forced_cloud_call(tmp_path: Path) -> None:
    service, reasoner = _service(tmp_path, cloud=True, budget=0.0)
    session = service.sessions.create()

    response = service.handle_text(
        session,
        "Why might box three stay more humid?",
        override=RouteOverride.FORCE_CLOUD_MODEL,
        forced_model=service.settings.cloud.terra_model,
        administrator=True,
    )

    assert response.stopping_reason == "budget_denied"
    assert reasoner.calls == 0


class Planner:
    def request_from_text(self, text, **_kwargs):
        return DiagnosticRequest(text, DiagnosticDomain.GRAFANA) if "grafana" in text.casefold() else None


class DiagnosticEngine:
    def diagnose(self, request):
        assessment = DiagnosticAssessment(request.domain, DiagnosticStatus.HEALTHY, Confidence.HIGH, (), EvidenceBundle())
        return DiagnosticAnswer(request, assessment, "local_playbook", "grafana_current_data", "Grafana and its data path are healthy.", "healthy", stopping_reason="local_complete")


def test_diagnostic_request_uses_local_playbook_path(tmp_path: Path) -> None:
    service, reasoner = _service(tmp_path)
    service.assistant.diagnostic_planner = Planner()
    service.assistant.diagnostic_engine = DiagnosticEngine()
    session = service.sessions.create()

    response = service.handle_text(session, "Why isn't Grafana showing current sensor data?")

    assert response.route == "local_playbook"
    assert response.skill == "diagnose_read_only"
    assert reasoner.calls == 0


def test_direct_component_health_question_prefers_promoted_skill_over_diagnostic(tmp_path: Path) -> None:
    service, reasoner = _service(tmp_path)
    service.assistant.diagnostic_planner = Planner()
    service.assistant.diagnostic_engine = DiagnosticEngine()
    session = service.sessions.create()

    response = service.handle_text(session, "Is Grafana healthy?")

    assert response.route == "deterministic"
    assert response.skill == "get_stack_observation"
    assert reasoner.calls == 0


def test_forced_model_diagnostic_stays_on_diagnostic_path(tmp_path: Path) -> None:
    service, reasoner = _service(tmp_path, cloud=True)
    service.assistant.diagnostic_planner = Planner()
    service.assistant.diagnostic_engine = DiagnosticEngine()
    session = service.sessions.create()
    captured = {}

    def execute(request, _trace, **kwargs):
        captured.update(kwargs)
        route = RoutedIntent("unsupported", request.text, message="bounded diagnostic fixture")
        return service._unsupported_response(request.text, request.text, route, "fixture")  # noqa: SLF001

    service._execute_diagnostic = execute  # type: ignore[method-assign]
    response = service.handle_text(
        session,
        "Why isn't Grafana showing current sensor data?",
        override=RouteOverride.FORCE_CLOUD_MODEL,
        forced_model=service.settings.cloud.sol_model,
        reasoning_effort="xhigh",
        administrator=True,
    )

    assert captured["force_cloud"] is True
    assert captured["forced_model"] == service.settings.cloud.sol_model
    assert captured["forced_effort"] == "xhigh"
    assert response.route != "general_cloud"
    assert reasoner.calls == 0


def test_request_output_limit_rejects_zero_instead_of_using_default(tmp_path: Path) -> None:
    service, _reasoner = _service(tmp_path, cloud=True)
    session = service.sessions.create()

    with pytest.raises(ValueError, match="max_output_tokens"):
        service.handle_text(
            session,
            "Explain a bounded open ended question",
            override=RouteOverride.CLOUD_AUTO,
            max_output_tokens=0,
            administrator=True,
        )
