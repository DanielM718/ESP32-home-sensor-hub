"""Security and vertical-slice tests for provider-independent planning."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from butters.assistant_config import load_assistant_settings
from butters.planner.model import PlannerCatalogAction, PlannerError, PlannerRequest
from butters.planner.provider import (
    DeterministicPlannerProvider,
    DisabledPlannerProvider,
)
from butters.planner.validator import PlannerValidator
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService


class NoCloud:
    available = False


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


def _service(tmp_path, provider=None):
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        desktop=replace(
            base.desktop,
            enabled=True,
            wake_enabled=True,
            shutdown_enabled=True,
        ),
        web=replace(base.web, state_dir=tmp_path, development_mode=True).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    return BetaAssistantService(
        settings,
        DomainVocabulary((), ()),
        general_reasoner=NoCloud(),
        state_dir=tmp_path,
        planner_provider=provider,
    )


def _admin(service):
    return service.sessions.create(peer_key="identity:admin", administrator=True)


def _draft(action, parameters=None, *, confirmation=False):
    return {
        "summary": "Use a registered action.",
        "rationale": "The request matches a reviewed capability.",
        "requires_confirmation": confirmation,
        "steps": [{"action_id": action, "parameters": parameters or {}}],
    }


def _validator(service):
    validator = PlannerValidator(service.assistant.skills)
    catalog = validator.catalog(
        frozenset(
            {
                "get_desktop_status",
                "wake_desktop",
                "desktop.app.launch",
                "shutdown_desktop",
            }
        ),
        parameter_enums={"desktop.app.launch": {"app": ("git_bash", "parsec")}},
    )
    return validator, catalog


def test_unknown_action_is_rejected(tmp_path):
    service = _service(tmp_path)
    validator, catalog = _validator(service)
    with pytest.raises(PlannerError) as error:
        validator.validate(
            _draft("run_shell", {"command": "shutdown now"}),
            catalog=catalog,
            administrator=True,
        )
    assert error.value.code == "unknown_action"


@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("wake_desktop", {"machine": "desktop", "host": "10.0.0.8"}),
        ("wake_desktop", {"machine": "desktop", "mac": "aa:bb:cc:dd:ee:ff"}),
        ("shutdown_desktop", {"machine": "desktop", "command": "shutdown /s"}),
        ("desktop.app.launch", {"app": "git_bash", "path": "C:\\evil.exe"}),
    ],
)
def test_extra_or_broker_level_parameters_are_rejected(tmp_path, action, parameters):
    service = _service(tmp_path)
    validator, catalog = _validator(service)
    with pytest.raises(PlannerError) as error:
        validator.validate(
            _draft(action, parameters), catalog=catalog, administrator=True
        )
    assert error.value.code in {"invalid_arguments", "policy_denied"}


@pytest.mark.parametrize(
    "app", ["git_bash; shutdown /s", "C:\\cmd.exe", "$(id)", "calculator"]
)
def test_arbitrary_command_or_executable_injection_is_rejected(tmp_path, app):
    service = _service(tmp_path)
    validator, catalog = _validator(service)
    with pytest.raises(PlannerError):
        validator.validate(
            _draft("desktop.app.launch", {"app": app}),
            catalog=catalog,
            administrator=True,
        )


def test_provider_cannot_override_shutdown_auth_or_confirmation(tmp_path):
    service = _service(tmp_path)
    validator, catalog = _validator(service)
    plan = validator.validate(
        _draft(
            "shutdown_desktop",
            {"machine": "desktop"},
            confirmation=False,
        ),
        catalog=catalog,
        administrator=True,
    )
    assert plan.requires_confirmation is True
    assert plan.required_authentication == "fresh"


def test_shutdown_enters_existing_frozen_confirmation_flow(tmp_path):
    service = _service(tmp_path, DeterministicPlannerProvider())
    result = service.plan_conversation(_admin(service), "shut down my desktop")
    assert result["status"] == "confirmation_required"
    assert result["authentication_required"] == "fresh"
    pending = result["pending_action"]
    assert pending["state"] == "pending_confirmation"
    assert pending["steps"] == [
        {"skill": "shutdown_desktop", "arguments": {"machine": "desktop"}}
    ]


@pytest.mark.parametrize(
    ("text", "action", "parameters"),
    [
        ("check my desktop", "get_desktop_status", {"machine": "desktop"}),
        ("wake my desktop", "wake_desktop", {"machine": "desktop"}),
        ("open Git Bash", "desktop.app.launch", {"app": "git_bash"}),
        ("open Parsec", "desktop.app.launch", {"app": "parsec"}),
    ],
)
def test_fake_provider_plans_initial_desktop_intents(text, action, parameters):
    catalog = (PlannerCatalogAction(action, "test", {}, "action", "none", False),)
    raw = DeterministicPlannerProvider().plan(PlannerRequest(text, catalog, {}, ()))
    assert raw["steps"] == [{"action_id": action, "parameters": parameters}]


def test_status_executes_through_existing_registered_read_only_skill(tmp_path):
    service = _service(tmp_path, DeterministicPlannerProvider())
    implementation = service.assistant.skills.get(
        "get_desktop_status"
    ).implementation.__self__
    implementation.desktop = SimpleNamespace(
        status=lambda machine: SimpleNamespace(
            safe_dict=lambda: {
                "machine": machine,
                "network_reachable": True,
                "ssh_ready": True,
                "parsec_ready": True,
            }
        )
    )
    result = service.plan_conversation(_admin(service), "check my desktop")
    assert result["status"] == "executed"
    assert result["plan"]["steps"][0]["action_id"] == "get_desktop_status"


def test_unconfigured_provider_fails_gracefully(tmp_path):
    service = _service(tmp_path)
    result = service.plan_conversation(_admin(service), "wake my desktop")
    assert result == {
        "status": "unavailable",
        "reason_code": "planner_unavailable",
        "message": "Conversational planning is not configured.",
    }
    with pytest.raises(PlannerError) as error:
        DisabledPlannerProvider().plan(PlannerRequest("wake", (), {}, ()))
    assert error.value.code == "planner_unavailable"


def test_minimal_planner_endpoint_returns_structured_unavailable_state(tmp_path):
    service = _service(tmp_path)
    app = create_app(
        service.settings,
        DomainVocabulary((), ()),
        service,
        stt_engine_factory=Engine,
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            created = (await http.get("/api/session")).json()
            response = await http.post(
                "/api/planner",
                headers={
                    "origin": "http://testserver",
                    "x-butters-csrf": created["csrf_token"],
                },
                json={"text": "wake my desktop"},
            )
            assert response.status_code == 200
            assert response.json()["status"] == "unavailable"
            assert response.json()["reason_code"] == "planner_unavailable"

    asyncio.run(scenario())


def test_manual_admin_action_path_is_unchanged(tmp_path):
    service = _service(tmp_path, DeterministicPlannerProvider())
    result = service.execute_desktop_action(_admin(service), "wake_desktop", {})
    assert result["jobs"] == []
    assert result["pending_action"]["steps"] == [
        {"skill": "wake_desktop", "arguments": {"machine": "desktop"}}
    ]


def test_provider_failure_cannot_reach_the_action_api(tmp_path):
    class BrokenProvider:
        name = "broken"
        available = True

        def plan(self, request):
            raise RuntimeError("provider exploded")

    service = _service(tmp_path, BrokenProvider())
    performed = []
    implementation = service.assistant.skills.get(
        "wake_desktop"
    ).implementation.__self__
    implementation.actions = SimpleNamespace(
        execute=lambda *args, **kwargs: performed.append((args, kwargs))
    )
    result = service.plan_conversation(_admin(service), "wake my desktop")
    assert result["status"] == "invalid_plan"
    assert result["reason_code"] == "provider_failure"
    assert performed == []
    assert service.action_state.jobs(identity="identity:admin") == ()


def test_planner_audit_distinguishes_request_plan_and_validation(tmp_path):
    service = _service(tmp_path, DeterministicPlannerProvider())
    service.plan_conversation(_admin(service), "wake my desktop")
    entries = [
        item
        for item in service.action_state.audit_entries()
        if item["skill"] == "conversational_planner"
    ]
    assert {item["outcome"] for item in entries} >= {
        "validated",
        "confirmation_required",
    }
    validated = next(item for item in entries if item["outcome"] == "validated")
    assert validated["arguments"]["user_request"] == "wake my desktop"
    assert (
        validated["arguments"]["structured_plan"]["steps"][0]["action_id"]
        == "wake_desktop"
    )


def test_planner_audit_redacts_secrets_from_user_requests(tmp_path):
    service = _service(tmp_path)
    service.plan_conversation(
        _admin(service), "wake my desktop api_key=sk-1234567890abcdef"
    )
    entry = next(
        item
        for item in service.action_state.audit_entries()
        if item["skill"] == "conversational_planner"
    )
    assert "sk-1234567890abcdef" not in entry["arguments"]["user_request"]
    assert "[REDACTED]" in entry["arguments"]["user_request"]


def test_malformed_and_overlong_sequences_are_rejected(tmp_path):
    service = _service(tmp_path)
    validator, catalog = _validator(service)
    with pytest.raises(PlannerError, match="fields"):
        validator.validate({"steps": []}, catalog=catalog, administrator=True)
    raw = _draft("wake_desktop", {"machine": "desktop"})
    raw["steps"] = raw["steps"] * 4
    with pytest.raises(PlannerError) as error:
        validator.validate(raw, catalog=catalog, administrator=True)
    assert error.value.code == "plan_limit"
