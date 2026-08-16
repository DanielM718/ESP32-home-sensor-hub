from __future__ import annotations

import threading
import time
from dataclasses import replace

from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.integrations.model import SensorSnapshot, ServerHealthSnapshot
from butters.skills.model import (
    ActionAuthorization,
    ActionClass,
    AuthenticationContext,
    AuthenticationLevel,
    DesktopArgs,
    StructuredSkillResult,
)
from butters.skills.policy import PolicyValidator, allow_arguments
from butters.skills.registry import (
    SkillRegistry,
    SkillSpec,
    current_cancel_event,
    required_string,
    strict_arguments,
)
from butters.stt.normalization import DomainVocabulary


class Sensors:
    def snapshot(self):
        return SensorSnapshot("2026-08-15T12:00:00Z", ())


class Health:
    def snapshot(self):
        return ServerHealthSnapshot(1, 0, 0, 0, 1, 0, 1, 1, 40, "0x0", ())


class Printer:
    def current(self):
        raise RuntimeError("not used")

    def environment_summary(self):
        raise RuntimeError("not used")

    def intelligence(self):
        raise RuntimeError("not used")

    def current_session(self):
        return None

    def recent_sessions(self, _limit):
        return ()

    def session(self, _print_id):
        return None


class Desktop:
    def __init__(self) -> None:
        self.calls = 0

    def status(self, machine):
        from butters.integrations.desktop import DesktopState

        return DesktopState(machine, True, True, True)

    def start_remote_session(self, machine, *, cancel_event=None):
        self.calls += 1
        return {
            "machine": machine,
            "wake_sent": True,
            "network_reachable": True,
            "ssh_ready": True,
            "remote_mode_requested": True,
            "parsec_ready": True,
            "verification_complete": True,
            "elapsed_ms": 10,
            "failed_stage": None,
            "error": None,
        }


def _assistant():
    settings = load_assistant_settings()
    settings = replace(
        settings,
        broker=replace(settings.broker, enabled=True),
    )
    assistant = create_assistant(
        settings,
        DomainVocabulary((), ()),
        sensor_adapter=Sensors(),
        server_adapter=Health(),
        printer_adapter=Printer(),
    )
    implementation = assistant.skills.get(
        "start_remote_desktop_session"
    ).implementation.__self__
    desktop = Desktop()
    implementation.desktop = desktop
    return assistant, desktop


def test_v2_metadata_has_typed_policy_and_result_contracts() -> None:
    assistant, _desktop = _assistant()
    v2 = [item for item in assistant.skills.skills if item.version == "2.0.0"]
    assert {item.action_class for item in v2} == {
        ActionClass.READ_ONLY,
        ActionClass.ANALYTICAL,
        ActionClass.ACTION,
    }
    assert all(item.input_schema.get("additionalProperties") is False for item in v2)
    assert all(item.output_schema and item.max_result_bytes > 0 for item in v2)
    action = assistant.skills.get("start_remote_desktop_session")
    assert action is not None
    assert action.explicit_intent_required and action.confirmation_required
    assert action.side_effects.startswith("wake configured desktop")


def test_action_requires_exact_direct_user_authorization() -> None:
    assistant, desktop = _assistant()
    registry = assistant.skills
    args = {"machine": "desktop"}
    denied = registry.execute("start_remote_desktop_session", args)
    wrong = registry.execute(
        "start_remote_desktop_session",
        args,
        action_authorization=ActionAuthorization(
            frozenset({"some_other_action"}), "direct_user_request", True
        ),
    )
    model_only = registry.execute(
        "start_remote_desktop_session",
        args,
        action_authorization=ActionAuthorization(
            frozenset({"start_remote_desktop_session"}), "cloud_proposal", True
        ),
    )
    allowed = registry.execute(
        "start_remote_desktop_session",
        args,
        action_authorization=ActionAuthorization(
            frozenset({"start_remote_desktop_session"}),
            "direct_user_request",
            True,
        ),
        authentication_context=AuthenticationContext(
            AuthenticationLevel.ELEVATED,
            "session",
            "identity",
            time.time() + 60,
            "webauthn",
        ),
        session_id="session",
        identity="identity",
    )
    assert denied.failure and denied.failure.code == "action_confirmation_required"
    assert wrong.failure and wrong.failure.code == "action_confirmation_required"
    assert (
        model_only.failure and model_only.failure.code == "action_confirmation_required"
    )
    assert allowed.ok
    assert desktop.calls == 1


def test_cloud_text_cannot_inject_host_script_command_or_extra_fields() -> None:
    assistant, desktop = _assistant()
    registry = assistant.skills
    attempts = (
        {"machine": "evil.example"},
        {"machine": "desktop", "hostname": "evil.example"},
        {"machine": "desktop", "script": "/tmp/test.sh"},
        {"machine": "desktop", "command": "rm -rf /"},
    )
    for arguments in attempts:
        _canonical, failure = registry.validate_action_intent(
            "start_remote_desktop_session", arguments
        )
        assert failure
        assert failure.code in {"invalid_arguments", "policy_denied"}
    for name, arguments in (
        ("run_shell", {"command": "rm -rf /"}),
        ("ssh", {"host": "10.0.0.5", "command": "id"}),
        ("execute_script", {"path": "/tmp/test.sh"}),
        ("query_database", {"query": "from(bucket: secrets)"}),
    ):
        result = registry.execute(name, arguments)
        assert result.failure and result.failure.code == "unknown_skill"
    assert desktop.calls == 0


def test_natural_language_direct_action_and_status_stay_deterministic() -> None:
    assistant, desktop = _assistant()
    action = assistant.handle_text("Turn on my computer and get Parsec ready.")
    status = assistant.handle_text("Is my computer ready for Parsec?")
    suggestion = assistant.handle_text("Why can't I connect to my desktop?")
    assert action.route.skill == "start_remote_desktop_session"
    assert action.execution and action.execution.failure
    assert action.execution.failure.code == "authentication_required"
    assert status.route.skill == "get_desktop_status"
    assert status.execution and status.execution.ok
    assert suggestion.route.skill != "start_remote_desktop_session"
    assert desktop.calls == 0


def test_explicit_multi_action_request_freezes_as_exact_deterministic_plan() -> None:
    assistant, _desktop = _assistant()
    route = assistant.router.route(
        "Prepare my computer and turn on the dehumidifier for 30 minutes"
    )
    assert route.matched
    assert route.action_plan == (
        ("start_remote_desktop_session", {"machine": "desktop"}),
        (
            "set_dehumidifier",
            {"state": "on", "duration_minutes": 30},
        ),
    )


def _desktop_parser(values):
    strict_arguments(values, required=frozenset({"machine"}))
    return DesktopArgs(required_string(values, "machine"))


def test_v2_skill_timeout_sets_cancellation_and_returns_without_hanging() -> None:
    policy = PolicyValidator(
        allowed_actions=frozenset({ActionClass.READ_ONLY, ActionClass.ANALYTICAL})
    )
    registry = SkillRegistry(policy)
    cancelled = threading.Event()

    def slow(_args):
        event = current_cancel_event()
        assert event is not None
        event.wait(1)
        cancelled.set()
        return StructuredSkillResult("slow", {})

    registry.register(
        SkillSpec(
            "slow_analysis",
            "fixture",
            ActionClass.ANALYTICAL,
            _desktop_parser,
            allow_arguments,
            slow,
            0.02,
            version="2.0.0",
        )
    )
    started = time.perf_counter()
    result = registry.execute("slow_analysis", {"machine": "desktop"})
    assert result.failure and result.failure.code == "timeout"
    assert time.perf_counter() - started < 0.2
    assert cancelled.wait(0.2)


def test_v2_skill_rejects_excessive_structured_result() -> None:
    registry = SkillRegistry(
        PolicyValidator(allowed_actions=frozenset({ActionClass.ANALYTICAL}))
    )
    registry.register(
        SkillSpec(
            "large_analysis",
            "fixture",
            ActionClass.ANALYTICAL,
            _desktop_parser,
            allow_arguments,
            lambda _args: StructuredSkillResult("large", {"value": "x" * 1000}),
            1,
            version="2.0.0",
            max_result_bytes=100,
        )
    )
    result = registry.execute("large_analysis", {"machine": "desktop"})
    assert result.failure and result.failure.code == "result_too_large"
