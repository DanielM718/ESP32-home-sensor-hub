from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from butters.assistant import create_assistant
from butters.assistant_config import DesktopSettings, load_assistant_settings
from butters.integrations.desktop import DesktopAccessMode, DesktopWorkflow
from butters.skills.model import (
    ActionAuthorization,
    AuthenticationContext,
    AuthenticationLevel,
)
from butters.stt.normalization import DomainVocabulary


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


class Clock:
    def __init__(self, step: float = 1.0) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class Operations:
    def __init__(self) -> None:
        self.ssh = True
        self.icmp = False
        self.wakes = 0
        self.ensures = 0
        self.sunshine = dict(READY)
        self.monitor = {
            "accepted": True,
            "desired_state": "off",
            "already_in_state": True,
            "partial": False,
            "entities": {"switch.one": "off", "switch.two": "off"},
        }
        self.monitor_requests: list[str] = []

    def network_reachable(self, _timeout: float) -> bool:
        return self.icmp

    def ssh_ready(self, _timeout: float) -> bool:
        return self.ssh

    def send_wake(self, _timeout: float) -> bool:
        self.wakes += 1
        return True

    def sunshine_status(self, _timeout: float):
        return None if self.sunshine is None else dict(self.sunshine)

    def sunshine_service_status(self, _timeout: float):
        return {"service_ready": True}

    def ensure_sunshine_running(self, _timeout: float) -> bool:
        self.ensures += 1
        return True

    def set_monitor_power(self, desired: str, _timeout: float):
        self.monitor_requests.append(desired)
        value = dict(self.monitor) if self.monitor is not None else None
        if value is not None:
            value["desired_state"] = desired
        return value

    # Legacy protocol members are intentionally unused by explicit modes.
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


def workflow(operations, *, clock=None, agent_status=None):
    return DesktopWorkflow(
        DesktopSettings(
            enabled=True,
            host="192.0.2.1",
            poll_interval_seconds=0,
            network_timeout_seconds=45,
            wake_readiness_timeout_seconds=120,
            ssh_timeout_seconds=10,
            total_timeout_seconds=180,
            sunshine_status_enabled=True,
            sunshine_ensure_enabled=True,
        ),
        operations,
        clock=clock or Clock(0.1),
        agent_status=agent_status,
        post_headless_settle_seconds=0,
    )


def test_delayed_agent_reconnect_at_107_seconds_is_inside_wake_window() -> None:
    clock = Clock(1)
    operations = Operations()
    operations.ssh = False

    def agent():
        connected = clock.value >= 107
        return {
            "state": "connected" if connected else "disconnected",
            "agent_connected": connected,
            "last_authenticated_activity_age_seconds": 0.2 if connected else None,
        }

    result = workflow(operations, clock=clock, agent_status=agent).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["verification_complete"] is True
    assert result["wake_sent"] is True
    assert result["agent_connected"] is True
    assert "desktop_agent" in result["online_evidence"]
    assert result["elapsed_ms"] < 120_000


def test_authenticated_agent_proves_online_when_ping_fails() -> None:
    operations = Operations()
    operations.ssh = False
    result = workflow(
        operations,
        agent_status=lambda: {
            "state": "connected",
            "agent_connected": True,
            "last_authenticated_activity_age_seconds": 1.0,
        },
    ).prepare_access_mode("desktop", DesktopAccessMode.LOCAL)

    assert result["online"] is True
    assert result["icmp_reachable"] is False
    assert result["wake_sent"] is False
    assert result["management_path"] == "desktop_agent"


def test_overview_uses_cached_service_status_not_capture_probe() -> None:
    operations = Operations()
    service_calls = 0

    def service_status(_timeout: float):
        nonlocal service_calls
        service_calls += 1
        return {"service_ready": True}

    operations.sunshine_service_status = service_status

    def capture_status(_timeout: float):
        raise AssertionError("an overview must not start the interactive DXGI probe")

    operations.sunshine_status = capture_status
    desktop = workflow(operations)
    first = desktop.status("desktop")
    second = desktop.status("desktop")

    assert first.sunshine_service_ready is True
    assert first.sunshine_ready is None
    assert second.sunshine_service_ready is True
    assert service_calls == 1


@pytest.mark.parametrize(
    ("mode", "desired", "profile"),
    (
        (DesktopAccessMode.GAMING, "off", "gaming"),
        (DesktopAccessMode.ENGINEERING, "off", "engineering_low_bandwidth"),
        (DesktopAccessMode.LOCAL, "on", None),
    ),
)
def test_explicit_mode_transitions_are_idempotent(mode, desired, profile) -> None:
    operations = Operations()
    result = workflow(operations).prepare_access_mode("desktop", mode)

    assert result["verification_complete"] is True
    assert result["client_profile"] == profile
    assert operations.monitor_requests == [desired]
    assert operations.ensures == 0
    if mode is not DesktopAccessMode.LOCAL:
        assert result["sunshine_reused"] is True


def test_partial_monitor_failure_is_preserved_without_automatic_reversal() -> None:
    operations = Operations()
    operations.monitor = {
        "accepted": False,
        "partial": True,
        "already_in_state": False,
        "entities": {"switch.one": "off", "switch.two": "on"},
    }
    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["status"] == "partial"
    assert result["failure_code"] == "monitor_transition_partial"
    assert result["monitor_transition"]["entities"]["switch.two"] == "on"
    assert operations.monitor_requests == ["off"]
    assert "do not automatically reverse" in result["safe_recovery"]


def test_backend_failures_identify_capture_and_virtual_display_separately() -> None:
    operations = Operations()
    operations.sunshine["capture_ready"] = False
    operations.sunshine["plausibly_ready"] = False
    capture = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )
    assert capture["failure_code"] == "sunshine_capture_unavailable"
    assert capture["failed_dependency"] == "sunshine_capture"

    operations = Operations()
    operations.sunshine["virtual_display_ready"] = False
    operations.sunshine["plausibly_ready"] = False
    display = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.ENGINEERING
    )
    assert display["failure_code"] == "virtual_display_unavailable"
    assert display["failed_dependency"] == "virtual_display"


def test_home_assistant_unavailable_and_cancellation_are_exact() -> None:
    operations = Operations()
    operations.monitor = None
    unavailable = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )
    assert unavailable["failure_code"] == "home_assistant_unavailable"
    assert unavailable["failed_dependency"] == "home_assistant"

    cancelled = threading.Event()
    cancelled.set()
    operations = Operations()
    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING, cancel_event=cancelled
    )
    assert result["failure_code"] == "cancelled"
    assert operations.monitor_requests == []
    assert operations.wakes == operations.ensures == 0


def test_agent_online_does_not_claim_sunshine_management_readiness() -> None:
    operations = Operations()
    operations.ssh = False
    result = workflow(
        operations,
        agent_status=lambda: {
            "state": "connected",
            "agent_connected": True,
            "last_authenticated_activity_age_seconds": 0.4,
        },
    ).prepare_access_mode("desktop", DesktopAccessMode.GAMING)

    assert result["online"] is True
    assert result["failure_code"] == "sunshine_management_unavailable"
    assert result["failed_dependency"] == "ssh"
    assert result["sunshine_capture_ready"] is None


def test_future_engineering_backend_is_disabled_without_provisioning() -> None:
    operations = Operations()
    desktop = workflow(operations)
    desktop.settings = replace(
        desktop.settings, engineering_backend="rdp", rdp_backend_enabled=False
    )

    result = desktop.prepare_access_mode("desktop", DesktopAccessMode.ENGINEERING)

    assert result["failure_code"] == "access_backend_unavailable"
    assert result["failed_dependency"] == "rdp"
    assert operations.monitor_requests == []
    assert operations.ensures == 0


def test_authenticated_mode_skill_preserves_structured_partial_failure() -> None:
    operations = Operations()
    operations.monitor = {
        "accepted": False,
        "partial": True,
        "already_in_state": False,
        "entities": {"switch.one": "off", "switch.two": "on"},
    }
    settings = load_assistant_settings()
    settings = replace(settings, broker=replace(settings.broker, enabled=True))
    assistant = create_assistant(settings, DomainVocabulary((), ()))
    specification = assistant.skills.get("set_desktop_access_mode")
    assert specification is not None
    specification.implementation.__self__.desktop = workflow(operations)
    digest = "d" * 64

    execution = assistant.skills.execute(
        "set_desktop_access_mode",
        {"machine": "desktop", "mode": "gaming"},
        action_authorization=ActionAuthorization(
            frozenset({"set_desktop_access_mode"}), "direct_user_request", True
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
    )

    assert execution.failure is not None
    assert execution.failure.code == "monitor_transition_partial"
    assert execution.failure.details is not None
    assert execution.failure.details["status"] == "partial"
    assert execution.failure.details["monitor_transition"]["entities"] == {
        "switch.one": "off",
        "switch.two": "on",
    }


@pytest.mark.parametrize("mode", ("gaming", "engineering", "local"))
def test_conversational_mode_routing_is_explicit(mode: str) -> None:
    assistant = create_assistant(
        load_assistant_settings(), DomainVocabulary((), ())
    )
    route = assistant.router.route(f"switch to {mode} mode")

    assert route.skill == "set_desktop_access_mode"
    assert route.arguments == {"machine": "desktop", "mode": mode}
    negated = assistant.router.route(f"do not switch to {mode} mode")
    assert negated.skill != "set_desktop_access_mode"
