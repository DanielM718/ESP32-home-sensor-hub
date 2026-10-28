"""Simulated session management: MTT, physical access, and Sunshine sessions.

Every Windows-side capability here is a fake implementing the broker contract
documented in docs/DESKTOP_SESSION_MANAGEMENT.md. Nothing contacts Windows,
Home Assistant, or a broker socket.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from butters.actions import broker_main
from butters.actions.broker import (
    DESKTOP_SSH_OPERATIONS,
    BrokerError,
    BrokerOperation,
    FixedBrokerConfig,
    FixedBrokerOperations,
    _desktop_control_result,
    _DesktopControlOperation,
)
from butters.assistant import create_assistant
from butters.assistant_config import (
    ConfigError,
    DesktopSettings,
    load_assistant_settings,
)
from butters.integrations.desktop import DesktopAccessMode, DesktopWorkflow
from butters.stt.normalization import DomainVocabulary

ROOT = Path(__file__).resolve().parents[1]

READY = {
    "installed": True,
    "service_present": True,
    "service_running": True,
    "service_startup": "auto",
    "host_process_present": True,
    "web_listener_present": True,
    "stream_listeners_present": True,
    "capture_ready": True,
    "virtual_display_ready": True,
    "plausibly_ready": True,
}
DISPLAYS = {
    "virtual_display_active": True,
    "virtual_mode_verified": True,
    "primary_physical_active": True,
    "secondary_physical_active": False,
    "physical_display_active": True,
}
SESSIONS = {
    "terminated": (True, False, False),
    "resumable": (True, True, False),
    "active": (True, True, True),
    "ending": (True, False, True),
}


def session(state: str) -> dict[str, object]:
    if state == "unknown":
        return {
            "api_reachable": False,
            "app_running": None,
            "stream_active": None,
            "session_state": "unknown",
        }
    reachable, app, stream = SESSIONS[state]
    return {
        "api_reachable": reachable,
        "app_running": app,
        "stream_active": stream,
        "session_state": state,
    }


class Operations:
    """Fake broker operations that log every call in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ssh = True
        self.sunshine = dict(READY)
        self.displays = dict(DISPLAYS)
        self.physical_after = 0
        self.session_states = ["terminated"]
        self.after_end = "terminated"
        self.activation: dict[str, object] | None = None
        self.deactivation: dict[str, object] | None = None
        self.monitor_ok = True

    def network_reachable(self, _timeout: float) -> bool:
        return False

    def ssh_ready(self, _timeout: float) -> bool:
        return self.ssh

    def send_wake(self, _timeout: float) -> bool:
        self.calls.append("wake")
        return True

    def sunshine_status(self, _timeout: float):
        self.calls.append("sunshine_status")
        return dict(self.sunshine)

    def sunshine_service_status(self, _timeout: float):
        return {"service_ready": True}

    def ensure_sunshine_running(self, _timeout: float) -> bool:
        self.calls.append("sunshine_ensure")
        return True

    def set_monitor_power(self, desired: str, _timeout: float):
        self.calls.append(f"monitor:{desired}")
        return {
            "accepted": self.monitor_ok,
            "desired_state": "on" if desired == "primary_on" else desired,
            "already_in_state": False,
            "partial": False,
            "entities": {},
            "preserved_entity_unchanged": True,
            "broker_ok": True,
        }

    def display_status(self, _timeout: float):
        self.calls.append("display_status")
        if self.physical_after > 0:
            self.physical_after -= 1
            return {
                **self.displays,
                "primary_physical_active": False,
                "secondary_physical_active": False,
                "physical_display_active": False,
            }
        return dict(self.displays)

    def activate_virtual_display(self, _timeout: float):
        self.calls.append("mtt_activate")
        if self.activation is not None:
            return dict(self.activation)
        self.sunshine["virtual_display_ready"] = True
        self.sunshine["plausibly_ready"] = True
        return {
            **DISPLAYS,
            "accepted": True,
            "error_code": None,
            "elapsed_ms": 1,
            "already_active": False,
            "broker_ok": True,
        }

    def deactivate_virtual_display(self, _timeout: float):
        self.calls.append("mtt_deactivate")
        if self.deactivation is not None:
            return dict(self.deactivation)
        return {
            **DISPLAYS,
            "virtual_display_active": False,
            "virtual_mode_verified": False,
            "accepted": True,
            "error_code": None,
            "elapsed_ms": 1,
            "already_inactive": False,
            "broker_ok": True,
        }

    def sunshine_session_status(self, _timeout: float):
        self.calls.append("session_status")
        state = (
            self.session_states.pop(0)
            if len(self.session_states) > 1
            else self.session_states[0]
        )
        return None if state is None else session(state)

    def end_sunshine_session(self, _timeout: float):
        self.calls.append("session_end")
        self.session_states = [self.after_end]
        return {
            **session(self.after_end),
            "accepted": self.after_end == "terminated",
            "error_code": None,
            "elapsed_ms": 1,
            "already_terminated": False,
            "broker_ok": True,
        }

    # Explicit modes never use the legacy protocol members.
    def parsec_ready(self, _timeout: float):
        return None

    def parsec_status(self, _timeout: float):
        return None

    def ensure_parsec_running(self, _timeout: float) -> bool:
        raise AssertionError("explicit modes must not start Parsec")

    def request_headless_mode(self, _timeout: float) -> bool:
        raise AssertionError("structured monitor operation expected")

    def restore_headed_mode(self, _timeout: float) -> bool:
        raise AssertionError("structured monitor operation expected")


class Clock:
    def __init__(self, step: float = 0.1) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def settings(**overrides) -> DesktopSettings:
    values: dict[str, object] = {
        "enabled": True,
        "host": "192.0.2.1",
        "poll_interval_seconds": 0,
        "wake_readiness_timeout_seconds": 120,
        "ssh_timeout_seconds": 10,
        "total_timeout_seconds": 240,
        "sunshine_status_enabled": True,
        "sunshine_ensure_enabled": True,
        "display_status_enabled": True,
        "virtual_display_activation_enabled": True,
        "virtual_display_deactivation_enabled": True,
        "sunshine_session_status_enabled": True,
        "sunshine_session_end_enabled": True,
    }
    values.update(overrides)
    return DesktopSettings(**values)


def workflow(operations, *, clock=None, agent_status=None, **overrides):
    return DesktopWorkflow(
        settings(**overrides),
        operations,
        clock=clock or Clock(),
        agent_status=agent_status,
        post_headless_settle_seconds=0,
    )


# ----- Local ---------------------------------------------------------------


def test_local_turns_on_only_the_primary_monitor_and_verifies_one_display() -> None:
    operations = Operations()
    operations.physical_after = 2
    operations.session_states = ["resumable"]

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["status"] == "ready"
    assert result["monitor_scope"] == "primary"
    assert [call for call in operations.calls if call.startswith("monitor:")] == [
        "monitor:primary_on"
    ]
    assert result["preserved_monitor_unchanged"] is True
    assert result["physical_display_verified"] is True
    assert operations.calls.count("display_status") == 3
    # Local never queries or touches Sunshine's service, and never ends a stream.
    assert "sunshine_status" not in operations.calls
    assert "sunshine_ensure" not in operations.calls
    assert "session_end" not in operations.calls


def test_local_restores_physical_access_before_considering_mtt() -> None:
    operations = Operations()

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["virtual_display_action"] == "deactivated"
    assert operations.calls == [
        "monitor:primary_on",
        "display_status",
        "session_status",
        "mtt_deactivate",
    ]
    assert result["committed_side_effects"] == [
        "monitor_outlets",
        "virtual_display_deactivate",
    ]


@pytest.mark.parametrize("state", ("active", "resumable", "ending", "unknown"))
def test_local_never_removes_mtt_while_a_session_may_need_it(state: str) -> None:
    operations = Operations()
    operations.session_states = [state]

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["status"] == "ready"
    assert result["virtual_display_action"] == "retained"
    assert result["virtual_display_retained_reason"] == (
        "sunshine_session_unknown" if state == "unknown" else "sunshine_session_present"
    )
    assert "mtt_deactivate" not in operations.calls
    assert "session_end" not in operations.calls


def test_local_with_unobservable_session_status_keeps_mtt() -> None:
    operations = Operations()
    operations.session_states = [None]

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["virtual_display_action"] == "retained"
    assert result["virtual_display_retained_reason"] == "sunshine_session_unknown"
    assert "mtt_deactivate" not in operations.calls


def test_local_without_a_verified_physical_display_keeps_everything_in_place() -> None:
    operations = Operations()
    operations.physical_after = 10_000

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["status"] == "partial"
    assert result["failure_code"] == "physical_display_unverified"
    assert result["physical_display_verified"] is False
    assert "session_status" not in operations.calls
    assert "mtt_deactivate" not in operations.calls


def test_local_without_display_observation_never_removes_mtt() -> None:
    operations = Operations()

    result = workflow(
        operations,
        display_status_enabled=False,
        virtual_display_deactivation_enabled=False,
    ).prepare_access_mode("desktop", DesktopAccessMode.LOCAL)

    assert result["status"] == "ready"
    assert result["physical_display_verified"] is None
    assert result["virtual_display_action"] == "not_managed"
    assert any("physical_display_unobserved" in item for item in result["warnings"])
    assert operations.calls == ["monitor:primary_on"]


def test_failed_mtt_removal_is_reported_without_failing_local() -> None:
    operations = Operations()
    operations.deactivation = {
        "broker_ok": False,
        "broker_error_code": "sunshine_session_present",
    }

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["status"] == "ready"
    assert result["virtual_display_action"] == "deactivation_failed"
    assert any("virtual_display_deactivation_failed" in w for w in result["warnings"])


def test_primary_monitor_failure_stops_before_any_display_or_session_step() -> None:
    operations = Operations()
    operations.monitor_ok = False

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["failure_code"] == "monitor_transition_failed"
    assert result["error"] == "the primary monitor outlet did not verify on"
    assert operations.calls == ["monitor:primary_on"]


def test_legacy_operations_never_fall_back_to_powering_both_outlets() -> None:
    class Legacy(Operations):
        set_monitor_power = None  # type: ignore[assignment]

    operations = Legacy()
    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["failure_code"] == "home_assistant_unavailable"
    assert "may already have changed" in str(result["safe_recovery"])


# ----- Gaming / Engineering ------------------------------------------------


@pytest.mark.parametrize("mode", (DesktopAccessMode.GAMING, DesktopAccessMode.ENGINEERING))
def test_remote_mode_activates_and_verifies_mtt_before_outlets_turn_off(mode) -> None:
    operations = Operations()
    operations.sunshine.update(virtual_display_ready=False, plausibly_ready=False)

    result = workflow(operations).prepare_access_mode("desktop", mode)

    assert result["status"] == "ready"
    assert result["virtual_display_action"] == "activated"
    assert operations.calls == [
        "sunshine_status",
        "mtt_activate",
        "sunshine_status",
        "monitor:off",
        "sunshine_status",
    ]


def test_ready_mtt_is_never_reactivated_so_a_stream_is_preserved() -> None:
    operations = Operations()

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["status"] == "ready"
    assert result["sunshine_reused"] is True
    assert "mtt_activate" not in operations.calls
    assert "sunshine_ensure" not in operations.calls


@pytest.mark.parametrize(
    "activation",
    (
        None,
        {"broker_ok": False, "broker_error_code": "virtual_display_identity_mismatch"},
        {**DISPLAYS, "virtual_mode_verified": False, "accepted": True, "broker_ok": True},
        {**DISPLAYS, "accepted": False, "broker_ok": True},
    ),
)
def test_unverified_mtt_activation_never_turns_outlets_off(activation) -> None:
    operations = Operations()
    operations.sunshine.update(virtual_display_ready=False, plausibly_ready=False)
    operations.activation = activation or {}
    if activation is None:
        operations.activate_virtual_display = lambda _timeout: (  # type: ignore[method-assign]
            operations.calls.append("mtt_activate") or None
        )

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["failure_code"] == "virtual_display_activation_failed"
    # The activation was sent, so its effect is reported as possibly committed.
    assert result["status"] == "partial"
    assert not any(call.startswith("monitor:") for call in operations.calls)


def test_activation_disabled_keeps_the_existing_fail_closed_gate() -> None:
    operations = Operations()
    operations.sunshine.update(virtual_display_ready=False, plausibly_ready=False)

    result = workflow(
        operations, virtual_display_activation_enabled=False
    ).prepare_access_mode("desktop", DesktopAccessMode.GAMING)

    assert result["failure_code"] == "sunshine_ensure_failed" or result[
        "failure_code"
    ] == "virtual_display_unavailable"
    assert "mtt_activate" not in operations.calls
    assert not any(call.startswith("monitor:") for call in operations.calls)


# ----- End Remote Session -> Local ------------------------------------------


def test_end_remote_session_orders_physical_access_close_verify_then_mtt() -> None:
    operations = Operations()
    operations.session_states = ["active"]

    result = workflow(operations).end_remote_session("desktop")

    assert result["status"] == "ready"
    assert result["operation"] == "end_remote_session"
    assert result["remote_session_terminated"] is True
    assert result["virtual_display_action"] == "deactivated"
    assert operations.calls == [
        "monitor:primary_on",
        "display_status",
        "session_status",
        "session_end",
        "session_status",
        "session_status",
        "mtt_deactivate",
    ]


def test_end_remote_session_disabled_fails_before_any_side_effect() -> None:
    operations = Operations()
    operations.ssh = False

    result = workflow(operations, sunshine_session_end_enabled=False).end_remote_session(
        "desktop"
    )

    assert result["failure_code"] == "remote_session_end_disabled"
    assert result["status"] == "failed"
    assert operations.calls == []


def test_end_remote_session_never_closes_without_verified_physical_access() -> None:
    operations = Operations()
    operations.physical_after = 10_000
    operations.session_states = ["active"]

    result = workflow(operations).end_remote_session("desktop")

    assert result["failure_code"] == "physical_display_unverified"
    assert "session_end" not in operations.calls


def test_end_remote_session_does_not_close_an_unknown_session() -> None:
    operations = Operations()
    operations.session_states = ["unknown"]

    result = workflow(operations).end_remote_session("desktop")

    assert result["failure_code"] == "sunshine_session_unobservable"
    assert "session_end" not in operations.calls
    assert "mtt_deactivate" not in operations.calls


def test_end_remote_session_with_nothing_running_skips_the_close() -> None:
    operations = Operations()

    result = workflow(operations).end_remote_session("desktop")

    assert result["status"] == "ready"
    assert result["remote_session_terminated"] is True
    assert result["sunshine_session_end"] is None
    assert "session_end" not in operations.calls


def test_unverified_termination_keeps_mtt_for_the_session() -> None:
    operations = Operations()
    operations.session_states = ["active"]
    operations.after_end = "resumable"

    result = workflow(operations).end_remote_session("desktop")

    assert result["failure_code"] == "remote_session_not_terminated"
    assert result["status"] == "partial"
    assert result["virtual_display_action"] == "retained"
    assert "mtt_deactivate" not in operations.calls


# ----- Serialization and budgets --------------------------------------------


def test_superseded_wake_wait_releases_the_execution_lock_promptly() -> None:
    operations = Operations()
    operations.ssh = False

    def agent():
        # Only the newer Local request ever observes the desktop as online.
        connected = threading.current_thread().name == "local"
        return {
            "state": "connected" if connected else "disconnected",
            "agent_connected": connected,
            "last_authenticated_activity_age_seconds": 0.1 if connected else None,
        }

    desktop = workflow(
        operations,
        clock=time.monotonic,
        agent_status=agent,
        display_status_enabled=False,
        virtual_display_deactivation_enabled=False,
    )
    results: dict[str, dict[str, object]] = {}
    gaming = threading.Thread(
        target=lambda: results.setdefault(
            "gaming", desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING)
        ),
        name="gaming",
    )
    gaming.start()
    deadline = time.monotonic() + 2.0
    while "wake" not in operations.calls and time.monotonic() < deadline:
        time.sleep(0.005)
    started = time.monotonic()
    local = threading.Thread(
        target=lambda: results.setdefault(
            "local", desktop.prepare_access_mode("desktop", DesktopAccessMode.LOCAL)
        ),
        name="local",
    )
    local.start()
    gaming.join(3.0)
    local.join(3.0)

    assert not gaming.is_alive() and not local.is_alive()
    assert time.monotonic() - started < 3.0
    assert results["gaming"]["failure_code"] == "mode_transition_superseded"
    assert results["local"]["status"] == "ready"
    assert operations.calls == ["wake", "monitor:primary_on"]


def test_mutation_is_not_sent_without_the_whole_broker_budget() -> None:
    operations = Operations()
    now = [0.0]

    def slow_ssh(_timeout: float) -> bool:
        # Online evidence arrives with 25s of the 240s budget left: less than
        # the reserved 30s broker request timeout.
        now[0] = 215.0
        return True

    operations.ssh_ready = slow_ssh  # type: ignore[method-assign]
    result = workflow(operations, clock=lambda: now[0]).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["failure_code"] == "desktop_mode_timeout"
    assert result["failed_dependency"] == "monitor_outlets"
    assert operations.calls == []


def test_superseded_local_request_never_removes_mtt() -> None:
    operations = Operations()
    desktop = workflow(operations)
    original = operations.sunshine_session_status

    def newer_request_arrives(timeout: float):
        with desktop._mode_state_lock:
            desktop._mode_generation += 1
        return original(timeout)

    operations.sunshine_session_status = newer_request_arrives  # type: ignore[method-assign]
    result = desktop.prepare_access_mode("desktop", DesktopAccessMode.LOCAL)

    assert result["failure_code"] == "mode_transition_superseded"
    assert "mtt_deactivate" not in operations.calls


# ----- Settings ---------------------------------------------------------------


def test_mtt_removal_cannot_be_enabled_without_both_observations() -> None:
    with pytest.raises(ConfigError):
        settings(display_status_enabled=False).validated()
    with pytest.raises(ConfigError):
        settings(sunshine_session_status_enabled=False).validated()
    with pytest.raises(ConfigError):
        settings(
            sunshine_session_status_enabled=False,
            virtual_display_deactivation_enabled=False,
        ).validated()


def test_shipped_configuration_keeps_every_new_capability_off() -> None:
    desktop = load_assistant_settings().desktop

    assert desktop.total_timeout_seconds == 240
    assert not any(
        (
            desktop.display_status_enabled,
            desktop.virtual_display_activation_enabled,
            desktop.virtual_display_deactivation_enabled,
            desktop.sunshine_session_status_enabled,
            desktop.sunshine_session_end_enabled,
        )
    )


# ----- Skill, routing, and Admin surface -----------------------------------------


def test_end_remote_session_skill_is_explicit_fresh_and_unavailable_by_default() -> None:
    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))
    spec = assistant.skills.get("end_remote_desktop_session")

    assert spec is not None
    assert spec.explicit_intent_required is True
    assert spec.authentication.value == "fresh"
    assert spec.available is False


@pytest.mark.parametrize(
    "text",
    (
        "end the remote session",
        "end remote session and return to local mode",
        "close the sunshine session",
        "end my remote desktop session",
    ),
)
def test_end_remote_session_routes_only_when_named(text: str) -> None:
    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))

    assert assistant.router.route(text).skill == "end_remote_desktop_session"


@pytest.mark.parametrize(
    "text",
    (
        "switch to local mode",
        "do not end the remote session",
        "don't close the remote session",
    ),
)
def test_local_and_negated_requests_never_end_a_session(text: str) -> None:
    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))

    assert assistant.router.route(text).skill != "end_remote_desktop_session"


def test_admin_surface_exposes_one_fixed_end_session_endpoint() -> None:
    app = (ROOT / "src/butters/web/app.py").read_text()
    script = (ROOT / "src/butters/web/static/assets/admin.js").read_text()

    assert '"/api/admin/tools/desktop/end-remote-session"' in app
    assert "runtime.start_admin_desktop_end_remote_session" in app
    assert 'api("/api/admin/tools/desktop/end-remote-session",{method:"POST",body:JSON.stringify({})})' in script


# ----- Broker -----------------------------------------------------------------


class HomeAssistant:
    def __init__(self, gigabyte: str, oled: str) -> None:
        self.states = {"switch.desktop_gigabyte": gigabyte, "switch.desktop_oled": oled}
        self.posts: list[dict[str, object]] = []

    class Response:
        status = 200

        def __init__(self, payload: object) -> None:
            self.payload = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, maximum: int) -> bytes:
            chunk, self.payload = self.payload[:maximum], self.payload[maximum:]
            return chunk

    def opener(self, request, **_kwargs):
        if request.method == "POST":
            body = json.loads(request.data)
            self.posts.append({"path": request.full_url.split("/api/", 1)[1], "body": body})
            for entity in body["entity_id"]:
                self.states[entity] = request.full_url.rsplit("turn_", 1)[1]
            return self.Response([])
        entity = request.full_url.rsplit("/", 1)[-1]
        return self.Response({"state": self.states[entity]})


def broker(tmp_path: Path, home: HomeAssistant | None = None, runner=None):
    return FixedBrokerOperations(
        FixedBrokerConfig(
            "192.168.1.209",
            "Daniel",
            "34:5A:60:D7:4C:2C",
            "192.168.1.255",
            tmp_path / "credentials" / "windows_remote_mode",
            enabled_operations=frozenset(BrokerOperation),
        ),
        runner=runner or (lambda *_a, **_k: None),
        home_assistant_token="token",
        opener=home.opener if home else (lambda *_a, **_k: None),
        sleeper=lambda _seconds: None,
    )


@pytest.mark.parametrize("oled", ("off", "on"))
def test_primary_monitor_on_names_only_the_g27qc_and_preserves_the_oled(
    tmp_path: Path, oled: str
) -> None:
    home = HomeAssistant("off", oled)

    status = broker(tmp_path, home).desktop_primary_monitor_on()

    assert home.posts == [
        {"path": "services/switch/turn_on", "body": {"entity_id": ["switch.desktop_gigabyte"]}}
    ]
    assert status["accepted"] is True
    assert status["scope"] == "primary"
    assert status["preserved_entity_unchanged"] is True
    assert home.states["switch.desktop_oled"] == oled


def test_primary_monitor_already_on_sends_no_service_call(tmp_path: Path) -> None:
    home = HomeAssistant("on", "off")

    status = broker(tmp_path, home).desktop_primary_monitor_on()

    assert home.posts == []
    assert status["already_in_state"] is True
    assert status["accepted"] is True


def test_every_desktop_control_operation_is_in_the_ssh_credential_gate(
    tmp_path: Path,
) -> None:
    invoked: list[str] = []

    class Result:
        returncode = 0
        stdout = "{}"

    def runner(argv, **_kwargs):
        invoked.append(argv[0])
        return Result()

    handlers = broker(tmp_path, runner=runner).handlers()
    ssh_operations = set()
    for operation, handler in handlers.items():
        invoked.clear()
        # Only the transport matters here; HA-backed handlers fail on the stub.
        with contextlib.suppress(Exception):
            handler()
        if invoked == ["/usr/bin/ssh"]:
            ssh_operations.add(operation)

    assert ssh_operations == set(DESKTOP_SSH_OPERATIONS)


def test_broker_main_requires_known_hosts_for_sunshine_service_status(
    tmp_path: Path, monkeypatch
) -> None:
    required: list[str] = []
    monkeypatch.setattr(
        broker_main, "_require_root_private", lambda _path, label: required.append(label)
    )
    monkeypatch.setattr(
        broker_main.pwd, "getpwnam", lambda _name: type("U", (), {"pw_uid": 1000})()
    )
    config = tmp_path / "action-broker.toml"
    config.write_text(
        """
[broker]
service_user = "butters"
[desktop]
host = "192.168.1.209"
user = "Daniel"
mac = "34:5A:60:D7:4C:2C"
broadcast = "192.168.1.255"
key = "/etc/butters/action-broker/windows_remote_mode"
[home_assistant]
url = "http://127.0.0.1:8123"
[nas]
mac = ""
broadcast = ""
[operations]
"desktop.sunshine_service_status" = true
"""
    )

    broker_main._configuration(config)

    assert "desktop known_hosts" in required


@pytest.mark.parametrize(
    "payload",
    (
        {**session("terminated"), "app_running": True},
        {**session("terminated"), "stream_active": True},
        {**session("terminated"), "api_reachable": False},
        {**session("unknown"), "app_running": False},
        {**session("active"), "session_state": "streaming"},
        {**session("resumable"), "extra": True},
    ),
)
def test_contradictory_session_reports_are_rejected(payload) -> None:
    with pytest.raises(BrokerError):
        _desktop_control_result(_DesktopControlOperation.SUNSHINE_SESSION_STATUS, payload)


@pytest.mark.parametrize("state", ("terminated", "resumable", "active", "ending", "unknown"))
def test_consistent_session_reports_cross_the_broker(state: str) -> None:
    assert _desktop_control_result(
        _DesktopControlOperation.SUNSHINE_SESSION_STATUS, session(state)
    ) == session(state)


@pytest.mark.parametrize(
    ("operation", "payload"),
    (
        (_DesktopControlOperation.DISPLAY_STATUS, {**DISPLAYS, "physical_display_active": False}),
        (_DesktopControlOperation.DISPLAY_STATUS, {**DISPLAYS, "virtual_display_active": False}),
        (_DesktopControlOperation.DISPLAY_STATUS, {**DISPLAYS, "source_name": "DISPLAY1"}),
        (
            _DesktopControlOperation.VIRTUAL_DISPLAY_ACTIVATE,
            {**DISPLAYS, "accepted": True, "error_code": None, "elapsed_ms": True, "already_active": False},
        ),
        (
            _DesktopControlOperation.VIRTUAL_DISPLAY_DEACTIVATE,
            {**DISPLAYS, "accepted": True, "error_code": None, "elapsed_ms": 1},
        ),
    ),
)
def test_display_reports_must_match_the_strict_contract(operation, payload) -> None:
    with pytest.raises(BrokerError):
        _desktop_control_result(operation, payload)


def test_valid_display_and_activation_reports_cross_the_broker() -> None:
    assert _desktop_control_result(_DesktopControlOperation.DISPLAY_STATUS, DISPLAYS) == DISPLAYS
    activation = {
        **DISPLAYS,
        "accepted": True,
        "error_code": None,
        "elapsed_ms": 4200,
        "already_active": False,
    }
    assert (
        _desktop_control_result(_DesktopControlOperation.VIRTUAL_DISPLAY_ACTIVATE, activation)
        == activation
    )


def test_broker_example_keeps_every_new_operation_disabled() -> None:
    import tomllib

    operations = tomllib.loads(
        (ROOT / "config/action-broker.example.toml").read_text()
    )["operations"]
    for name in (
        "desktop.primary_monitor_on",
        "desktop.sunshine_session_status",
        "desktop.sunshine_session_end",
        "desktop.display_status",
        "desktop.virtual_display_activate",
        "desktop.virtual_display_deactivate",
    ):
        assert operations[name] is False
    assert set(operations) == {item.value for item in BrokerOperation}


# ----- Windows helper source contracts (no PowerShell runtime here) ----------


def test_windows_session_helper_contract() -> None:
    helper = (ROOT / "windows/sunshine-session.ps1").read_text()
    control = (ROOT / "windows/desktop-control.ps1").read_text()
    installer = (ROOT / "windows/install-desktop-control.ps1").read_text()

    # Fixed loopback endpoints only; nothing caller-supplied.
    assert "'http://127.0.0.1:47989/serverinfo'" in helper
    assert "'https://127.0.0.1:47990/api/apps/close'" in helper
    assert "param(" not in helper
    # No Origin/Referer is sent, so Sunshine's CSRF check is not bypassed by a
    # forged browser header; the TLS callback pins Sunshine's own certificate.
    assert "Origin" not in helper.replace("Origin or Referer", "")
    assert "ServerCertificateValidationCallback = [Butters.SunshineTls]::Callback" in helper
    assert "ServicePointManager]::ServerCertificateValidationCallback" not in helper
    assert "DtdProcessing]::Prohibit" in helper
    # Only "terminated" can come from a complete observation of both signals.
    assert "session_state = 'unknown'" in helper
    assert "'S-1-5-18', 'S-1-5-32-544'" in helper
    # Wired as two fixed operations, and installed beside the other helpers.
    assert "'SunshineSessionStatus', 'SunshineSessionEnd'" in control
    assert "Write-JsonResult (Invoke-ButtersSunshineSessionEnd)" in control
    assert "Copy-Item $sessionSource $sessionTarget -Force" in installer
    assert "sunshine-api-credential" not in installer.split("# The Sunshine API credential")[0]


def test_desktop_control_loads_topology_only_for_the_interactive_probe() -> None:
    control = (ROOT / "windows/desktop-control.ps1").read_text()
    body = control.split("switch ($Operation) {", 1)

    assert ". $displayTopologyHelper" not in body[0]
    probe = body[1].split("'SunshineProbeInteractive' {", 1)[1].split("}", 1)[0]
    assert ". $displayTopologyHelper" in probe


def test_unpublished_mtt_operations_are_not_reimplemented_here() -> None:
    control = (ROOT / "windows/desktop-control.ps1").read_text()

    for name in ("DisplayStatus", "VirtualDisplayActivate", "VirtualDisplayDeactivate"):
        assert f"'{name}'" not in control


def test_mode_settings_replace_keeps_validation(tmp_path: Path) -> None:
    # Guard for test helpers elsewhere that build settings with replace().
    base = load_assistant_settings().desktop
    with pytest.raises(ConfigError):
        replace(base, virtual_display_deactivation_enabled=True).validated()


# ----- Independent review regressions -------------------------------------------


def test_end_remote_session_requires_display_observation() -> None:
    with pytest.raises(ConfigError):
        settings(
            display_status_enabled=False, virtual_display_deactivation_enabled=False
        ).validated()

    # Even if constructed without validation, nothing is closed unverified.
    operations = Operations()
    operations.session_states = ["active"]
    result = workflow(
        operations,
        display_status_enabled=False,
        virtual_display_deactivation_enabled=False,
    ).end_remote_session("desktop")

    assert result["failure_code"] == "physical_display_unverified"
    assert operations.calls == ["monitor:primary_on"]


def test_end_remote_session_skill_requires_display_observation() -> None:
    base = load_assistant_settings()
    configured = replace(
        base,
        broker=replace(base.broker, enabled=True),
        desktop=replace(
            base.desktop,
            sunshine_session_status_enabled=True,
            sunshine_session_end_enabled=True,
        ),
    )
    assistant = create_assistant(configured, DomainVocabulary((), ()))

    assert assistant.skills.get("end_remote_desktop_session").available is False


def test_cancellation_after_a_committed_mutation_is_partial() -> None:
    operations = Operations()
    operations.physical_after = 10_000
    cancelled = threading.Event()
    original = operations.display_status

    def cancel_during_wait(timeout: float):
        cancelled.set()
        return original(timeout)

    operations.display_status = cancel_during_wait  # type: ignore[method-assign]
    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL, cancel_event=cancelled
    )

    assert result["failure_code"] == "cancelled"
    assert result["status"] == "partial"
    assert result["committed_side_effects"] == ["monitor_outlets"]


def test_sunshine_ensure_is_a_committed_mutation_with_a_budget() -> None:
    operations = Operations()
    operations.sunshine.update(capture_ready=False, plausibly_ready=False)

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert "sunshine_ensure" in operations.calls
    assert result["committed_side_effects"][0] == "sunshine_ensure"
    assert result["status"] == "partial"
    assert not any(call.startswith("monitor:") for call in operations.calls)


def test_oled_read_failure_never_blocks_the_primary_outlet(tmp_path: Path) -> None:
    home = HomeAssistant("off", "off")
    original = home.opener

    def opener(request, **kwargs):
        if request.method == "GET" and request.full_url.endswith("switch.desktop_oled"):
            raise OSError("oled unavailable")
        return original(request, **kwargs)

    home.opener = opener  # type: ignore[method-assign]
    status = broker(tmp_path, home).desktop_primary_monitor_on()

    assert status["accepted"] is True
    assert status["entities"]["switch.desktop_oled"] == "unknown"
    assert status["preserved_entity_unchanged"] is None
    assert home.posts[0]["body"] == {"entity_id": ["switch.desktop_gigabyte"]}


@pytest.mark.parametrize(
    ("operation", "payload"),
    (
        (
            _DesktopControlOperation.SUNSHINE_SESSION_END,
            {**session("resumable"), "accepted": True, "error_code": None, "elapsed_ms": 1, "already_terminated": False},
        ),
        (
            _DesktopControlOperation.SUNSHINE_SESSION_END,
            {**session("active"), "accepted": False, "error_code": None, "elapsed_ms": 1, "already_terminated": True},
        ),
        (
            _DesktopControlOperation.SUNSHINE_SESSION_END,
            {**session("terminated"), "accepted": True, "error_code": "sunshine_api_unavailable", "elapsed_ms": 1, "already_terminated": False},
        ),
        (
            _DesktopControlOperation.VIRTUAL_DISPLAY_ACTIVATE,
            {**DISPLAYS, "virtual_mode_verified": False, "accepted": True, "error_code": None, "elapsed_ms": 1, "already_active": False},
        ),
        (
            _DesktopControlOperation.VIRTUAL_DISPLAY_DEACTIVATE,
            {**DISPLAYS, "virtual_display_active": False, "virtual_mode_verified": False, "accepted": True, "error_code": None, "elapsed_ms": 1, "already_inactive": False},
        ),
        (
            _DesktopControlOperation.VIRTUAL_DISPLAY_DEACTIVATE,
            {**DISPLAYS, "virtual_display_active": False, "virtual_mode_verified": False, "accepted": True, "error_code": None, "elapsed_ms": 1, "already_inactive": False, "session_state": "resumable"},
        ),
    ),
)
def test_mutation_results_must_agree_with_their_observation(operation, payload) -> None:
    with pytest.raises(BrokerError):
        _desktop_control_result(operation, payload)


def test_verified_mtt_removal_crosses_the_broker() -> None:
    removal = {
        **DISPLAYS,
        "virtual_display_active": False,
        "virtual_mode_verified": False,
        "accepted": True,
        "error_code": None,
        "elapsed_ms": 900,
        "already_inactive": False,
        "session_state": "terminated",
    }
    assert (
        _desktop_control_result(_DesktopControlOperation.VIRTUAL_DISPLAY_DEACTIVATE, removal)
        == removal
    )


def test_end_session_combined_with_a_remote_mode_asks_for_clarification() -> None:
    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))
    route = assistant.router.route("end the remote session and switch to gaming mode")

    assert route.skill != "end_remote_desktop_session"
    assert route.skill != "set_desktop_access_mode"


def test_windows_session_helper_review_hardening() -> None:
    helper = (ROOT / "windows/sunshine-session.ps1").read_text()

    assert "SUNSHINE_SERVER_BUSY" in helper and "SUNSHINE_SERVER_FREE" in helper
    assert "throw 'The Sunshine process could not be identified'" in helper
    assert "$allowed -notcontains $owner" in helper
    assert "AreAccessRulesProtected" in helper
    assert "$script:SunshineSessionEndBudgetSeconds = 15" in helper
    assert "$started.AddSeconds($script:SunshineSessionEndBudgetSeconds)" in helper
