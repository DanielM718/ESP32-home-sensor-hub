from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from butters.actions.broker import (
    BrokerError,
    BrokerOperation,
    FixedBrokerConfig,
    FixedBrokerOperations,
)
from butters.actions.coordinator import ActionCoordinator
from butters.actions.store import ActionStateError, ActionStateStore
from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.integrations.desktop import DesktopWorkflow
from butters.integrations.model import IntegrationError
from butters.llm.catalog import derive_safe_tool_catalog
from butters.skills.actions_v2 import ActionSkillImplementations
from butters.skills.model import (
    ActionAuthorization,
    ActionClass,
    AuthenticationContext,
    AuthenticationLevel,
    DesktopArgs,
)
from butters.stt.normalization import DomainVocabulary

PARSEC_HEALTHY = {
    "installed": True,
    "installation_type": "machine",
    "service_present": True,
    "service_running": True,
    "service_startup": "manual",
    "service_process_present": True,
    "host_process_present": True,
    "system_host_process_present": True,
    "user_host_process_present": True,
    "plausibly_ready": True,
}


class RunnerResult:
    def __init__(self, payload: dict[str, object], returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = json.dumps(payload)


def _fixed_operations(tmp_path: Path, runner, operations) -> FixedBrokerOperations:
    return FixedBrokerOperations(
        FixedBrokerConfig(
            "192.168.1.209",
            "Daniel",
            "34:5A:60:D7:4C:2C",
            "192.168.1.255",
            tmp_path / "credentials" / "windows_remote_mode",
            enabled_operations=frozenset(operations),
        ),
        runner=runner,
    )


@pytest.mark.parametrize(
    "state",
    (
        {
            **PARSEC_HEALTHY,
            "installed": False,
            "installation_type": "absent",
            "service_present": False,
            "service_running": False,
            "service_startup": "absent",
            "service_process_present": False,
            "host_process_present": False,
            "system_host_process_present": False,
            "user_host_process_present": False,
            "plausibly_ready": False,
        },
        PARSEC_HEALTHY,
        {
            **PARSEC_HEALTHY,
            "service_running": False,
            "service_process_present": False,
            "plausibly_ready": False,
        },
    ),
)
def test_parsec_status_accepts_only_the_bounded_public_schema(
    tmp_path: Path, state: dict[str, object]
) -> None:
    operations = _fixed_operations(
        tmp_path,
        lambda _argv, **_kwargs: RunnerResult(state),
        {BrokerOperation.DESKTOP_PARSEC_STATUS},
    )

    assert operations.handlers()[BrokerOperation.DESKTOP_PARSEC_STATUS]() == state


def test_parsec_status_rejects_internal_paths_services_and_extra_fields(
    tmp_path: Path,
) -> None:
    unsafe = {
        **PARSEC_HEALTHY,
        "executable_path": "C:\\arbitrary.exe",
        "service_name": "caller-selected",
    }
    operations = _fixed_operations(
        tmp_path,
        lambda _argv, **_kwargs: RunnerResult(unsafe),
        {BrokerOperation.DESKTOP_PARSEC_STATUS},
    )

    with pytest.raises(BrokerError) as malformed:
        operations.desktop_parsec_status()
    assert malformed.value.code == "malformed_result"


def test_parsec_ensure_already_healthy_and_recovery_results_are_structured(
    tmp_path: Path,
) -> None:
    outputs = [
        {**PARSEC_HEALTHY, "accepted": True, "error_code": None, "elapsed_ms": 4, "already_running": True},
        {**PARSEC_HEALTHY, "accepted": True, "error_code": None, "elapsed_ms": 540, "already_running": False},
    ]
    operations = _fixed_operations(
        tmp_path,
        lambda _argv, **_kwargs: RunnerResult(outputs.pop(0)),
        {BrokerOperation.DESKTOP_PARSEC_ENSURE},
    )

    first = operations.desktop_parsec_ensure()
    second = operations.desktop_parsec_ensure()
    assert first["already_running"] is True and first["plausibly_ready"] is True
    assert second["already_running"] is False and second["elapsed_ms"] == 540


@pytest.mark.parametrize(
    ("operation", "error_code"),
    (
        (BrokerOperation.DESKTOP_PARSEC_ENSURE, "parsec_start_timeout"),
        (BrokerOperation.DESKTOP_PARSEC_RESTART, "parsec_restart_timeout"),
    ),
)
def test_parsec_action_timeout_or_failure_is_not_accepted(
    tmp_path: Path, operation: BrokerOperation, error_code: str
) -> None:
    payload = {
        **PARSEC_HEALTHY,
        "service_running": False,
        "service_process_present": False,
        "host_process_present": False,
        "plausibly_ready": False,
        "accepted": False,
        "error_code": error_code,
        "elapsed_ms": 20000,
    }
    if operation is BrokerOperation.DESKTOP_PARSEC_ENSURE:
        payload["already_running"] = False
    operations = _fixed_operations(
        tmp_path,
        lambda _argv, **_kwargs: RunnerResult(payload),
        {operation},
    )

    with pytest.raises(BrokerError) as failed:
        operations.handlers()[operation]()
    assert failed.value.code == error_code


def test_parsec_restart_success_and_subprocess_timeout_are_bounded(
    tmp_path: Path,
) -> None:
    success = {
        **PARSEC_HEALTHY,
        "accepted": True,
        "error_code": None,
        "elapsed_ms": 591,
    }
    assert _fixed_operations(
        tmp_path,
        lambda _argv, **_kwargs: RunnerResult(success),
        {BrokerOperation.DESKTOP_PARSEC_RESTART},
    ).desktop_parsec_restart()["plausibly_ready"] is True

    def timed_out(_argv, **_kwargs):
        raise subprocess.TimeoutExpired("ssh", 25)

    with pytest.raises(BrokerError) as timeout:
        _fixed_operations(
            tmp_path,
            timed_out,
            {BrokerOperation.DESKTOP_PARSEC_RESTART},
        ).desktop_parsec_restart()
    assert timeout.value.code == "operation_failed"


def test_wake_and_simple_parsec_start_are_separate_fixed_operations() -> None:
    calls: list[BrokerOperation] = []

    class Actions:
        def execute(self, operation, *, cancel_event=None):
            calls.append(operation)
            return {"accepted": True}

    implementation = ActionSkillImplementations(None, Actions(), None, None)
    arguments = DesktopArgs("desktop")

    implementation.wake_desktop(arguments)
    assert calls == [BrokerOperation.DESKTOP_WAKE]

    calls.clear()
    implementation.ensure_parsec_running(arguments)
    assert calls == [BrokerOperation.DESKTOP_PARSEC_ENSURE]
    assert BrokerOperation.DESKTOP_MONITORS_OFF not in calls


def test_every_desktop_control_operation_uses_one_fixed_script_and_no_dynamic_argv(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        name = argv[-1].rsplit(" ", 1)[-1]
        if name.startswith("Parsec"):
            payload = dict(PARSEC_HEALTHY)
            if name != "ParsecStatus":
                payload.update(accepted=True, error_code=None, elapsed_ms=1)
                if name == "ParsecEnsure":
                    payload["already_running"] = True
        else:
            payload = {
                "accepted": True,
                "transition": name.casefold(),
                "scheduled": True,
            }
        return RunnerResult(payload)

    enabled = {
        BrokerOperation.DESKTOP_PARSEC_STATUS,
        BrokerOperation.DESKTOP_PARSEC_ENSURE,
        BrokerOperation.DESKTOP_PARSEC_RESTART,
        BrokerOperation.DESKTOP_LOCK,
        BrokerOperation.DESKTOP_SLEEP,
        BrokerOperation.DESKTOP_RESTART,
        BrokerOperation.DESKTOP_SHUTDOWN,
    }
    handlers = _fixed_operations(tmp_path, runner, enabled).handlers()
    for operation in enabled:
        handlers[operation]()

    assert len(calls) == len(enabled)
    for argv in calls:
        assert argv[-2] == "Daniel@192.168.1.209"
        assert argv[-1].startswith(
            "powershell.exe -NoLogo -NoProfile -NonInteractive "
            "-ExecutionPolicy Bypass -File C:\\ProgramData\\Butters\\desktop-control.ps1 -Operation "
        )
        assert not any(token in argv for token in ("shell", "service", "path", "host"))
        assert argv[-1].rsplit(" ", 1)[-1] in {
            "ParsecStatus",
            "ParsecEnsure",
            "ParsecRestart",
            "Lock",
            "Sleep",
            "Restart",
            "Shutdown",
        }


class StatusOperations:
    def __init__(self, status: dict[str, object] | None, *, network=True, ssh=True):
        self.value = status
        self.network = network
        self.ssh = ssh

    def network_reachable(self, _timeout_seconds: float) -> bool:
        return self.network

    def ssh_ready(self, _timeout_seconds: float) -> bool:
        return self.ssh

    def parsec_status(self, _timeout_seconds: float) -> dict[str, object] | None:
        return self.value

    def parsec_ready(self, _timeout_seconds: float) -> bool | None:
        return None if self.value is None else bool(self.value["plausibly_ready"])

    def send_wake(self, _timeout_seconds: float) -> bool:
        raise AssertionError

    def ensure_parsec_running(self, _timeout_seconds: float) -> bool:
        raise AssertionError

    def request_headless_mode(self, _timeout_seconds: float) -> bool:
        raise AssertionError


def test_user_parsec_status_is_truthful_when_offline_healthy_or_unobservable() -> None:
    settings = load_assistant_settings().desktop
    offline = DesktopWorkflow(
        settings, StatusOperations(None, network=False, ssh=False)
    ).parsec_status("desktop")
    healthy = DesktopWorkflow(settings, StatusOperations(PARSEC_HEALTHY)).parsec_status(
        "desktop"
    )
    unknown = DesktopWorkflow(settings, StatusOperations(None)).parsec_status("desktop")

    assert offline["desktop_reachable"] is False
    assert offline["installed"] is None and offline["plausibly_ready"] is False
    assert healthy["observed"] is True and healthy["plausibly_ready"] is True
    assert unknown["ssh_reachable"] is True and unknown["observed"] is False
    assert not any("path" in key or "name" in key for key in healthy)


@pytest.mark.parametrize(
    ("phrase", "skill"),
    (
        ("is Parsec running?", "get_parsec_status"),
        ("is Parsec ready?", "get_parsec_status"),
        ("can I connect to Parsec?", "get_parsec_status"),
        ("start Parsec", "ensure_parsec_running"),
        ("make sure Parsec is running", "ensure_parsec_running"),
        ("restart Parsec", "restart_parsec"),
        ("lock my computer", "lock_desktop"),
        ("put my desktop to sleep", "sleep_desktop"),
        ("restart my computer", "restart_desktop"),
        ("turn off my desktop", "shutdown_desktop"),
    ),
)
def test_desktop_v2_routing_is_explicit(phrase: str, skill: str) -> None:
    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))
    route = assistant.router.route(phrase)
    assert route.matched and route.skill == skill
    assert route.arguments == {"machine": "desktop"}


@pytest.mark.parametrize(
    "phrase",
    (
        "don't shut down my desktop",
        "do not restart Parsec",
        "never put the desktop to sleep",
        "don't start Parsec",
    ),
)
def test_negated_desktop_v2_requests_never_route_to_an_action(phrase: str) -> None:
    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))
    route = assistant.router.route(phrase)
    assert route.skill not in {
        "ensure_parsec_running",
        "restart_parsec",
        "lock_desktop",
        "sleep_desktop",
        "restart_desktop",
        "shutdown_desktop",
    }


@pytest.mark.parametrize(
    "phrase",
    (
        "restart my desktop or restart Parsec",
        "restart or shut down my desktop",
    ),
)
def test_ambiguous_destructive_desktop_requests_require_clarification(
    phrase: str,
) -> None:
    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))
    route = assistant.router.route(phrase)
    assert route.status == "clarification"
    assert route.skill is None


def _enabled_assistant(tmp_path: Path):
    settings = load_assistant_settings()
    settings = replace(
        settings,
        broker=replace(settings.broker, enabled=True),
        desktop=replace(
            settings.desktop,
            parsec_status_enabled=True,
            parsec_ensure_enabled=True,
            parsec_restart_enabled=True,
            lock_enabled=True,
            sleep_enabled=True,
            restart_enabled=True,
            shutdown_enabled=True,
        ),
    )
    state = ActionStateStore(tmp_path / "actions.sqlite3", settings.actions)
    assistant = create_assistant(
        settings,
        DomainVocabulary((), ()),
        action_state=state,
    )
    return assistant, state


def test_every_new_action_requires_fresh_auth_and_models_see_zero_actions(
    tmp_path: Path,
) -> None:
    assistant, _state = _enabled_assistant(tmp_path)
    action_names = {
        "start_remote_desktop_session",
        "ensure_parsec_running",
        "restart_parsec",
        "lock_desktop",
        "sleep_desktop",
        "restart_desktop",
        "shutdown_desktop",
    }
    for name in action_names:
        spec = assistant.skills.get(name)
        assert spec is not None
        assert spec.action_class is ActionClass.ACTION
        assert spec.authentication is AuthenticationLevel.FRESH
        denied = assistant.skills.execute(name, {"machine": "desktop"})
        assert denied.failure and denied.failure.code == "action_confirmation_required"

    catalog = derive_safe_tool_catalog(
        assistant.skills, assistant.router.entities, assistant.router.metrics
    )
    visible = {item.name for item in catalog}
    assert action_names.isdisjoint(visible)
    assert "get_parsec_status" in visible


def test_frozen_parsec_plan_binds_exact_skill_machine_digest_and_is_one_use(
    tmp_path: Path,
) -> None:
    assistant, state = _enabled_assistant(tmp_path)
    arguments = {"machine": "desktop"}
    plan = ActionCoordinator(assistant.skills, state).freeze(
        skill="restart_parsec",
        arguments=arguments,
        summary="Restart Parsec",
        session_id="session",
        identity="person",
        request_id="request",
        source="browser",
    )
    arguments["machine"] = "evil.example"

    assert plan.steps[0].skill == "restart_parsec"
    assert plan.steps[0].arguments == {"machine": "desktop"}
    assert len(plan.digest) == 64
    state.claim(plan)
    with pytest.raises(ActionStateError) as replay:
        state.claim(plan)
    assert replay.value.code == "pending_action_replayed"


def test_extra_action_arguments_are_denied_before_the_broker_boundary(
    tmp_path: Path,
) -> None:
    assistant, _state = _enabled_assistant(tmp_path)
    for extra in (
        {"service": "Spooler"},
        {"path": "C:\\evil.exe"},
        {"command": "whoami"},
        {"host": "evil.example"},
    ):
        canonical, failure = assistant.skills.validate_action_intent(
            "ensure_parsec_running", {"machine": "desktop", **extra}
        )
        assert canonical is None
        assert failure and failure.code == "invalid_arguments"


def test_parsec_action_cancellation_stops_before_the_fixed_adapter(
    tmp_path: Path,
) -> None:
    settings = load_assistant_settings()
    settings = replace(
        settings,
        broker=replace(settings.broker, enabled=True),
        desktop=replace(settings.desktop, parsec_ensure_enabled=True),
    )
    state = ActionStateStore(tmp_path / "actions.sqlite3", settings.actions)
    assistant = create_assistant(
        settings, DomainVocabulary((), ()), action_state=state
    )
    implementation = assistant.skills.get("ensure_parsec_running").implementation.__self__
    calls: list[BrokerOperation] = []

    class CancelledActions:
        def execute(self, operation, *, cancel_event=None):
            calls.append(operation)
            assert cancel_event is not None and cancel_event.is_set()
            raise IntegrationError("cancelled", "action was cancelled")

    implementation.actions = CancelledActions()
    event = threading.Event()
    event.set()
    digest = "a" * 64
    execution = assistant.skills.execute(
        "ensure_parsec_running",
        {"machine": "desktop"},
        action_authorization=ActionAuthorization(
            frozenset({"ensure_parsec_running"}), "direct_user_request", True
        ),
        authentication_context=AuthenticationContext(
            AuthenticationLevel.FRESH,
            "session",
            "person",
            time.time() + 60,
            "webauthn",
            digest,
        ),
        session_id="session",
        identity="person",
        action_digest=digest,
        cancel_event=event,
    )

    assert execution.failure and execution.failure.code == "cancelled"
    assert calls == []


def test_windows_helper_uses_s3_not_hibernate_and_fixed_tasks_only() -> None:
    root = Path(__file__).resolve().parents[1]
    helper = (root / "windows" / "desktop-control.ps1").read_text(encoding="utf-8")
    installer = (root / "windows" / "install-desktop-control.ps1").read_text(
        encoding="utf-8"
    )

    assert "SetSuspendState($false, $false, $false)" in helper
    assert "shutdown.exe /h" not in helper
    assert "\\Butters\\LockDesktop" in helper
    assert "\\Butters\\SleepDesktop" in helper
    assert "ValidateSet('ParsecStatus'" in helper
    assert "Set-Service -Name 'Parsec' -StartupType Manual" in installer
    assert "StartupType Automatic" not in installer
    assert "Start-Service -Name 'Parsec'" not in installer
    assert "Set-Service" not in helper


def test_human_git_bash_launcher_cannot_change_automation_shell_semantics() -> None:
    root = Path(__file__).resolve().parents[1]
    launcher = (root / "windows" / "human-ssh-launcher.ps1").read_text(
        encoding="utf-8"
    )
    powershell_files = "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "windows").glob("*.ps1")
    )

    assert "C:\\Program Files\\Git\\bin\\bash.exe" in launcher
    assert "$env:SSH_ORIGINAL_COMMAND" in launcher
    assert "& $bash '--login' '-i'" in launcher
    assert "& $bash '--login' '-c' $original" in launcher
    assert "DefaultShell" not in powershell_files
