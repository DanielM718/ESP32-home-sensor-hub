"""Bounded compound planning, escalation order, and cancellation semantics."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from beta1_harness import build_app

from butters.actions.coordinator import ActionCoordinatorError
from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.integrations.model import PrinterSnapshot, SensorRecord, SensorSnapshot
from butters.routing.compound import (
    MAX_COMPOUND_OPERATIONS,
    plan_compound_request,
    split_clauses,
)
from butters.routing.normalization import normalize_request
from butters.skills.model import (
    ActionClass,
    AuthenticationContext,
    AuthenticationLevel,
    SkillError,
    StructuredSkillResult,
)
from butters.stt.normalization import DomainVocabulary
from butters.web.service import BetaAssistantService

PRINTER_AND_AIR = "Is the printer running and is the office air quality okay?"
SAME_ENTITY = "What are the temperature and humidity in the printer room?"
WAKE_AND_REACH = "Wake my desktop and tell me when it is reachable."


@pytest.fixture
def service(tmp_path: Path):
    _app, runtime, _settings = build_app(tmp_path)
    implementation = runtime.assistant.skills.get(
        "get_sensor_value"
    ).implementation.__self__
    implementation.sensor_provider = _CompoundSensors()
    implementation.printer_provider = _CompoundPrinter()
    return runtime


class _CompoundSensors:
    def snapshot(self) -> SensorSnapshot:
        return SensorSnapshot(
            "2026-08-27T12:00:00Z",
            (
                SensorRecord(
                    "environment",
                    "3",
                    "2026-08-27T12:00:00Z",
                    1,
                    "online",
                    {"humidity": 42.0},
                ),
                SensorRecord(
                    "air_quality",
                    "office",
                    "2026-08-27T12:00:00Z",
                    1,
                    "online",
                    {
                        "temperature_c": 23.0,
                        "humidity": 45.0,
                        "co2": 600,
                        "pm25": 4.0,
                        "voc_index": 80,
                    },
                ),
            ),
        )


class _CompoundPrinter:
    def current(self) -> PrinterSnapshot:
        return PrinterSnapshot(
            "x2d",
            "X2D",
            True,
            "idle",
            "2026-08-27T12:00:00Z",
            {},
        )


def _session(runtime, name: str):
    return runtime.sessions.create(peer_key=f"identity:{name}@example.com")


def _answer(runtime, text: str, name: str = "compound") -> dict[str, object]:
    return runtime.handle_text(_session(runtime, name), text).as_dict()


class _CountingRegistry:
    """Wrap the registry to count how many skill calls one turn performs."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, skill_name, arguments, **kwargs):
        self.calls.append(skill_name)
        return self._inner.execute(skill_name, arguments, **kwargs)


# --- A: independent read-only clauses ------------------------------------


def test_two_independent_reads_are_both_planned_and_answered(service) -> None:
    """The reported failure: this collapsed into one irrelevant clarification."""

    result = _answer(service, PRINTER_AND_AIR)

    assert result["route"] == "compound"
    assert result["reason_codes"] == ("bounded_compound_plan",)
    text = str(result["response_text"]).casefold()
    assert "x2d" in text
    assert "printer room" in text
    assert "co2" in text


def test_the_compound_answer_is_not_a_clarification(service) -> None:
    result = _answer(service, PRINTER_AND_AIR)

    assert "which sensor did you mean" not in str(result["response_text"]).casefold()


# --- B: same-entity multi-metric must stay one call ----------------------


def test_a_same_entity_multi_metric_read_stays_a_single_call(service) -> None:
    """This must not be split into one request per metric."""

    counting = _CountingRegistry(service.assistant.skills)
    service.assistant.skills = counting
    result = _answer(service, SAME_ENTITY)

    assert result["route"] == "deterministic"
    assert counting.calls == ["get_sensor_values"]


def test_the_planner_never_sees_a_request_the_router_already_answers(
    service,
) -> None:
    normalized = normalize_request(SAME_ENTITY)
    route = service.assistant.router.route(normalized)

    # The planner runs only after a whole-text route failure, and this matches.
    assert route.matched
    assert route.skill == "get_sensor_values"
    assert sorted(route.arguments["metrics"]) == ["humidity", "temperature"]


# --- C: wake + reachability ----------------------------------------------


def test_wake_with_a_readiness_question_composes_the_existing_observation(
    service,
) -> None:
    """Previously this matched wake alone and never reported reachability."""

    route = service.assistant.router.route(normalize_request(WAKE_AND_REACH))

    assert route.skill == "wake_desktop"
    assert route.action_plan == (
        ("wake_desktop", {"machine": "desktop"}),
        ("wait_for_desktop_reachability", {"machine": "desktop"}),
    )


def test_a_plain_wake_request_is_left_exactly_as_it_was(service) -> None:
    route = service.assistant.router.route(normalize_request("wake my desktop"))

    assert route.skill == "wake_desktop"
    assert route.action_plan == ()


def test_the_composed_step_is_a_non_mutating_observation(service) -> None:
    spec = service.assistant.skills.get("wait_for_desktop_reachability")

    assert spec is not None
    assert spec.action_class is ActionClass.READ_ONLY
    assert spec.side_effects == "none"


def test_the_wake_step_still_carries_its_own_authentication(tmp_path: Path) -> None:
    """Composing an observation must not soften the action's requirement."""

    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        web=replace(base.web, state_dir=tmp_path, development_mode=True).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    runtime = BetaAssistantService(
        settings, DomainVocabulary((), ()), state_dir=tmp_path
    )
    plan = runtime.actions.freeze_plan(
        steps=(
            ("wake_desktop", {"machine": "desktop"}),
            ("wait_for_desktop_reachability", {"machine": "desktop"}),
        ),
        summary="wake and report readiness",
        session_id="session",
        identity="identity:admin@example.com",
        request_id="request",
        source="text",
    )

    assert [step.skill for step in plan.steps] == [
        "wake_desktop",
        "wait_for_desktop_reachability",
    ]
    # The plan still demands the same elevated ceremony the wake alone did.
    assert plan.authentication.value == "elevated"


def test_a_plan_of_observations_alone_is_refused(tmp_path: Path) -> None:
    """An observation must never acquire an action's authorization."""

    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        web=replace(base.web, state_dir=tmp_path, development_mode=True).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    runtime = BetaAssistantService(
        settings, DomainVocabulary((), ()), state_dir=tmp_path
    )
    with pytest.raises(ActionCoordinatorError) as failure:
        runtime.actions.freeze_plan(
            steps=(("wait_for_desktop_reachability", {"machine": "desktop"}),),
            summary="observation only",
            session_id="session",
            identity="identity:admin@example.com",
            request_id="request",
            source="text",
        )

    assert "at least one action" in str(failure.value)


def test_an_observation_may_not_be_smuggled_ahead_of_an_action(
    tmp_path: Path,
) -> None:
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        web=replace(base.web, state_dir=tmp_path, development_mode=True).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    runtime = BetaAssistantService(
        settings, DomainVocabulary((), ()), state_dir=tmp_path
    )
    with pytest.raises(ActionCoordinatorError):
        runtime.actions.freeze_plan(
            steps=(
                ("get_desktop_status", {"machine": "desktop"}),
                ("wake_desktop", {"machine": "desktop"}),
            ),
            summary="observation first",
            session_id="session",
            identity="identity:admin@example.com",
            request_id="request",
            source="text",
        )


# --- the planner never composes privilege --------------------------------


def test_the_planner_refuses_to_compose_a_mutating_clause(service) -> None:
    plan = plan_compound_request(
        service.assistant.router,
        normalize_request(WAKE_AND_REACH),
        service.assistant.skills,
    )

    assert plan.status == "action_not_composable"
    assert plan.operations == ()


def test_the_planner_only_emits_registered_non_mutating_skills(service) -> None:
    plan = plan_compound_request(
        service.assistant.router,
        normalize_request(PRINTER_AND_AIR),
        service.assistant.skills,
    )

    assert plan.planned
    for operation in plan.operations:
        spec = service.assistant.skills.get(operation.skill)
        assert spec is not None
        assert spec.action_class is not ActionClass.ACTION
        assert spec.side_effects == "none"


# --- D: bounded ----------------------------------------------------------


def test_an_excessive_compound_request_is_refused_not_expanded(service) -> None:
    text = (
        "is the printer running and is the office air quality okay and what is "
        "the humidity in box three and what is the humidity in box one and what "
        "is the humidity in box two"
    )
    result = _answer(service, text, "broad")

    assert result["reason_codes"] == ("compound_request_too_broad",)
    assert "too many separate things" in str(result["response_text"])


def test_the_plan_never_exceeds_its_operation_bound(service) -> None:
    text = (
        "is the printer running and what is the humidity in box three and what "
        "is the humidity in box one"
    )
    plan = plan_compound_request(
        service.assistant.router, normalize_request(text), service.assistant.skills
    )

    assert plan.planned
    assert len(plan.operations) <= MAX_COMPOUND_OPERATIONS


def test_clause_splitting_ignores_fragments(service) -> None:
    assert split_clauses("a and b") == ()
    assert len(split_clauses("is the printer running and is the air okay")) == 2


def test_a_partial_failure_is_stated_rather_than_hidden(service) -> None:
    text = "is the printer running and tell me a joke about printers"
    plan = plan_compound_request(
        service.assistant.router, normalize_request(text), service.assistant.skills
    )

    # Only one clause resolves, so this is not presented as a compound answer.
    assert plan.planned is False


# --- reasoning / escalation order ----------------------------------------


def test_all_conversational_providers_are_disabled_by_default() -> None:
    settings = load_assistant_settings()

    assert settings.llm.enabled is False
    assert settings.cloud.enabled is False
    assert settings.cloud.allow_paid_calls is False


def test_with_no_reasoner_an_open_request_reaches_a_truthful_fallback(
    service,
) -> None:
    assert service.general_reasoner.available is False
    result = _answer(service, "what is the capital of france", "fallback")

    assert result["route"] == "unsupported"
    assert result["reason_codes"] == (
        "open_ended_reasoning_required",
        "cloud_disabled",
    )
    text = str(result["response_text"])
    # Truthful about why, and never implies a local conversational model exists.
    assert "currently enabled" in text
    assert "thinking" not in text.casefold()


def test_a_configured_provider_would_receive_only_the_safe_catalog() -> None:
    """A fake enabled model sees the derived catalog and nothing else."""

    seen: dict[str, object] = {}

    class FakeModel:
        def propose_tools(self, _text, tools, _context):
            seen["tools"] = tools
            raise RuntimeError("provider stops here")

    assistant = create_assistant(
        load_assistant_settings(),
        DomainVocabulary((), ()),
        language_model=FakeModel(),
    )
    assistant.handle_text("what is the capital of france")

    names = {tool.name for tool in seen["tools"]}
    actions = {
        spec.name
        for spec in assistant.skills.skills
        if spec.action_class is ActionClass.ACTION
    }
    assert names
    assert names.isdisjoint(actions)
    assert "get_sensor_values" in names


def test_a_model_proposing_an_action_cannot_execute_it() -> None:
    """Registry policy, not the catalog, is the last word."""

    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))
    execution = assistant.skills.execute(
        "start_remote_desktop_session", {"machine": "desktop"}
    )

    assert execution.ok is False
    assert execution.failure is not None


# --- cancellation ---------------------------------------------------------


def test_a_turn_cancelled_before_any_tool_runs_is_genuinely_stopped(
    service,
) -> None:
    cancelled = threading.Event()
    cancelled.set()
    session = _session(service, "cancel")
    result = service.handle_text(
        session, "what is the humidity in box three", cancel_event=cancelled
    ).as_dict()

    assert result["reason_codes"] == ("client_cancelled",)
    assert "cancelled before it ran" in str(result["response_text"])


def test_a_compound_turn_stops_between_reads_when_cancelled(service) -> None:
    counting = _CountingRegistry(service.assistant.skills)
    service.assistant.skills = counting
    cancelled = threading.Event()
    session = _session(service, "compoundcancel")

    original = counting.execute

    def stop_after_first(skill_name, arguments, **kwargs):
        cancelled.set()
        return original(skill_name, arguments, **kwargs)

    counting.execute = stop_after_first
    service.handle_text(session, PRINTER_AND_AIR, cancel_event=cancelled)

    # The first read ran; the second was not started.
    assert len(counting.calls) == 1


def test_an_uncooperative_read_finishes_after_the_client_stops_waiting(service) -> None:
    """A set token is not proof that an already-running adapter was stopped."""

    skill = service.assistant.skills.get("get_sensor_value")
    assert skill is not None
    original = skill.implementation
    started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    responses = []

    def uncooperative(arguments):
        started.set()
        assert release.wait(2)
        return original(arguments)

    service.assistant.skills._skills[skill.name] = replace(
        skill, implementation=uncooperative
    )

    worker = threading.Thread(
        target=lambda: responses.append(
            service.handle_text(
                _session(service, "uncooperative"),
                "what is the humidity in box three",
                cancel_event=cancelled,
            )
        )
    )
    worker.start()
    assert started.wait(2)
    cancelled.set()

    # The adapter ignores cancellation and is still running. The browser's
    # interaction-generation guard must suppress this response if it is stale.
    assert worker.is_alive()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(responses) == 1
    assert responses[0].reason_codes != ("client_cancelled",)


def test_a_sensitive_read_cannot_replace_the_reviewed_plan_observation(
    tmp_path: Path,
) -> None:
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        web=replace(base.web, state_dir=tmp_path, development_mode=True).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    runtime = BetaAssistantService(
        settings, DomainVocabulary((), ()), state_dir=tmp_path
    )

    with pytest.raises(ActionCoordinatorError):
        runtime.actions.freeze_plan(
            steps=(
                ("wake_desktop", {"machine": "desktop"}),
                ("get_server_health", {}),
            ),
            summary="wake and inspect host",
            session_id="session",
            identity="identity:admin@example.com",
            request_id="request",
            source="text",
        )


def test_a_negated_wake_never_becomes_an_action(service) -> None:
    route = service.assistant.router.route(
        normalize_request("do not wake my desktop and tell me printer status")
    )

    assert route.skill != "wake_desktop"
    assert not route.action_plan


def _action_runtime(tmp_path: Path) -> BetaAssistantService:
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        web=replace(base.web, state_dir=tmp_path, development_mode=True).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    return BetaAssistantService(settings, DomainVocabulary((), ()), state_dir=tmp_path)


def _completed_job(runtime, job_id: str, *, identity: str = "identity:admin"):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = runtime.action_state.job(
            job_id, session_id="session", identity=identity
        )
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.005)
    raise AssertionError("action job did not settle")


def _run_fake_wake_plan(tmp_path: Path, *, wake_fails=False, reachable=True):
    runtime = _action_runtime(tmp_path)
    calls: list[str] = []
    wake = runtime.assistant.skills.get("wake_desktop")
    observe = runtime.assistant.skills.get("wait_for_desktop_reachability")
    assert wake is not None and observe is not None

    def fake_wake(_arguments):
        calls.append("wake")
        if wake_fails:
            raise SkillError("operation_failed", "fixed wake failed")
        return StructuredSkillResult("action_result", {"accepted": True})

    def fake_observe(_arguments):
        calls.append("wait")
        return StructuredSkillResult(
            "desktop_reachability_wait",
            {
                "network_reachable": reachable,
                "ssh_ready": reachable,
                "parsec_ready": None,
                "timed_out": not reachable,
                "cancelled": False,
                "elapsed_ms": 1000,
            },
        )

    runtime.assistant.skills._skills["wake_desktop"] = replace(
        wake, implementation=fake_wake
    )
    runtime.assistant.skills._skills["wait_for_desktop_reachability"] = replace(
        observe, implementation=fake_observe
    )
    plan = runtime.actions.freeze_plan(
        steps=(
            ("wake_desktop", {"machine": "desktop"}),
            ("wait_for_desktop_reachability", {"machine": "desktop"}),
        ),
        summary="wake and wait",
        session_id="session",
        identity="identity:admin",
        request_id="request",
        source="text",
    )
    authentication = AuthenticationContext(
        AuthenticationLevel.ELEVATED,
        "session",
        "identity:admin",
        time.time() + 60,
        "webauthn",
    )
    job = runtime.actions.execute(
        plan.plan_id,
        session_id="session",
        identity="identity:admin",
        authentication=authentication,
    )[0]
    return _completed_job(runtime, str(job["job_id"])), calls


def test_authorized_wake_waits_only_after_wake_succeeds(tmp_path: Path) -> None:
    job, calls = _run_fake_wake_plan(tmp_path, reachable=True)

    assert calls == ["wake", "wait"]
    assert job["state"] == "completed"
    data = job["result"]["steps"][1]["result"]["data"]
    assert data["network_reachable"] is True
    assert all(step["skill"] != "monitors_off" for step in job["result"]["steps"])


def test_wake_success_with_reachability_timeout_is_truthful(tmp_path: Path) -> None:
    job, calls = _run_fake_wake_plan(tmp_path, reachable=False)

    assert calls == ["wake", "wait"]
    assert job["state"] == "completed"
    data = job["result"]["steps"][1]["result"]["data"]
    assert data["network_reachable"] is False
    assert data["timed_out"] is True


def test_wake_failure_stops_before_reachability_polling(tmp_path: Path) -> None:
    job, calls = _run_fake_wake_plan(tmp_path, wake_fails=True)

    assert calls == ["wake"]
    assert job["state"] == "failed"
    assert job["failure_code"] == "operation_failed"


def test_unauthorized_wake_never_starts_a_job(tmp_path: Path) -> None:
    runtime = _action_runtime(tmp_path)
    session = runtime.sessions.create(
        peer_key="identity:not-admin@example.com", administrator=False
    )

    result = runtime.handle_text(
        session, "wake my desktop and tell me when it is reachable"
    )

    assert result.route == "action_denied"
    assert result.reason_codes == ("administrator_required",)
    assert runtime.action_state.jobs(identity=session.peer_key) == ()


def test_an_uncancelled_turn_is_unaffected(service) -> None:
    result = service.handle_text(
        _session(service, "plain"),
        "what is the humidity in box three",
        cancel_event=threading.Event(),
    ).as_dict()

    assert result["reason_codes"] != ("client_cancelled",)


def test_asyncio_is_not_required_for_cancellation(service) -> None:
    """Cancellation is a plain threading.Event, usable from any caller."""

    assert asyncio.iscoroutinefunction(service.handle_text) is False
