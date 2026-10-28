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
from butters.web.service import BetaAssistantService

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


def test_prepare_for_streaming_is_the_strict_gaming_mode_not_legacy_parsec() -> None:
    service = object.__new__(BetaAssistantService)
    called = {}

    def start_mode(session, mode):
        called.update(session=session, mode=mode)
        return {"status": "queued"}

    service.start_admin_desktop_mode = start_mode
    session = object()
    result = BetaAssistantService.start_admin_desktop_streaming(service, session)

    assert result == {"status": "queued"}
    assert called == {"session": session, "mode": "gaming"}


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
        # The structured broker result BrokerDesktopOperations returns.
        self.monitor = {
            "broker_ok": True,
            "accepted": True,
            "desired_state": "off",
            "already_in_state": True,
            "partial": False,
            "entities": {"switch.desktop_gigabyte": "off", "switch.desktop_oled": "off"},
            "power_w": {"switch.desktop_gigabyte": 0.0, "switch.desktop_oled": 0.0},
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
        # Local turns on only the primary G27QC outlet; the OLED is preserved.
        (DesktopAccessMode.LOCAL, "primary_on", None),
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
        "entities": {"switch.desktop_gigabyte": "off", "switch.desktop_oled": "on"},
    }
    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["status"] == "partial"
    assert result["failure_code"] == "monitor_transition_partial"
    assert result["monitor_transition"]["entities"]["switch.desktop_oled"] == "on"
    assert operations.monitor_requests == ["off"]
    assert "do not automatically reverse" in result["safe_recovery"]


def test_broker_envelope_failure_cannot_be_reported_as_monitor_success() -> None:
    operations = Operations()
    operations.monitor["broker_ok"] = False

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["verification_complete"] is False
    assert result["failure_code"] == "monitor_transition_failed"
    assert result["monitor_transition"]["accepted"] is True
    assert result["monitor_transition"]["broker_ok"] is False


def test_newer_local_transition_supersedes_remote_before_monitor_power_off() -> None:
    operations = Operations()
    capture_started = threading.Event()
    release_capture = threading.Event()
    original_status = operations.sunshine_status

    def blocked_status(timeout: float):
        capture_started.set()
        assert release_capture.wait(min(timeout, 5.0))
        return original_status(timeout)

    operations.sunshine_status = blocked_status
    desktop = workflow(operations, clock=time.monotonic)
    results: dict[str, dict[str, object]] = {}

    remote = threading.Thread(
        target=lambda: results.setdefault(
            "remote",
            desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING),
        )
    )
    remote.start()
    assert capture_started.wait(2.0)
    local = threading.Thread(
        target=lambda: results.setdefault(
            "local",
            desktop.prepare_access_mode("desktop", DesktopAccessMode.LOCAL),
        )
    )
    local.start()
    deadline = time.monotonic() + 2.0
    while desktop._mode_generation < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    release_capture.set()
    remote.join(2.0)
    local.join(2.0)

    assert not remote.is_alive() and not local.is_alive()
    assert results["remote"]["failure_code"] == "mode_transition_superseded"
    assert results["local"]["verification_complete"] is True
    assert operations.monitor_requests == ["primary_on"]


def test_local_generation_cannot_register_inside_inflight_monitor_commit() -> None:
    operations = Operations()
    monitor_started = threading.Event()
    release_monitor = threading.Event()
    original_monitor = operations.set_monitor_power

    def blocked_monitor(desired: str, timeout: float):
        monitor_started.set()
        assert release_monitor.wait(min(timeout, 5.0))
        return original_monitor(desired, timeout)

    operations.set_monitor_power = blocked_monitor
    desktop = workflow(operations, clock=time.monotonic)
    results: dict[str, dict[str, object]] = {}
    remote = threading.Thread(
        target=lambda: results.setdefault(
            "remote",
            desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING),
        )
    )
    remote.start()
    assert monitor_started.wait(2.0)
    local = threading.Thread(
        target=lambda: results.setdefault(
            "local",
            desktop.prepare_access_mode("desktop", DesktopAccessMode.LOCAL),
        )
    )
    local.start()
    time.sleep(0.05)

    # The older call already crossed the commit boundary. Local is not exposed
    # as the current generation until that bounded broker operation returns.
    assert desktop._mode_generation == 1
    release_monitor.set()
    remote.join(2.0)
    local.join(2.0)

    assert not remote.is_alive() and not local.is_alive()
    assert results["remote"]["failure_code"] in {
        None,
        "mode_transition_superseded",
    }
    assert results["local"]["verification_complete"] is True
    assert operations.monitor_requests == ["off", "primary_on"]


def test_cancellation_before_monitor_commit_has_no_monitor_side_effect() -> None:
    operations = Operations()
    capture_started = threading.Event()
    release_capture = threading.Event()
    cancelled = threading.Event()
    original_status = operations.sunshine_status

    def blocked_status(timeout: float):
        capture_started.set()
        assert release_capture.wait(min(timeout, 5.0))
        return original_status(timeout)

    operations.sunshine_status = blocked_status
    desktop = workflow(operations, clock=time.monotonic)
    results: dict[str, dict[str, object]] = {}
    worker = threading.Thread(
        target=lambda: results.setdefault(
            "value",
            desktop.prepare_access_mode(
                "desktop", DesktopAccessMode.GAMING, cancel_event=cancelled
            ),
        )
    )
    worker.start()
    assert capture_started.wait(2.0)
    cancelled.set()
    release_capture.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert results["value"]["failure_code"] == "cancelled"
    assert operations.monitor_requests == []


def test_disabled_operations_fail_before_side_effects() -> None:
    operations = Operations()
    desktop = workflow(operations)
    desktop.settings = replace(desktop.settings, headless_enabled=False)
    remote = desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING)
    assert remote["failure_code"] == "headless_management_disabled"
    assert operations.monitor_requests == []

    operations = Operations()
    operations.ssh = operations.icmp = False
    desktop = workflow(operations)
    desktop.settings = replace(desktop.settings, wake_enabled=False)
    local = desktop.prepare_access_mode("desktop", DesktopAccessMode.LOCAL)
    assert local["failure_code"] == "desktop_wake_disabled"
    assert operations.wakes == 0

    operations = Operations()
    desktop = workflow(operations)
    desktop.settings = replace(
        desktop.settings,
        sunshine_status_enabled=False,
        sunshine_ensure_enabled=False,
    )
    remote = desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING)
    assert remote["failure_code"] == "sunshine_status_disabled"
    assert operations.monitor_requests == []


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
        "entities": {"switch.desktop_gigabyte": "off", "switch.desktop_oled": "on"},
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
        "switch.desktop_gigabyte": "off",
        "switch.desktop_oled": "on",
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


# --- Wake ordering: both outlets verified off before any Wake-on-LAN --------

GIGABYTE = "switch.desktop_gigabyte"
OLED = "switch.desktop_oled"


def _monitor_result(
    states: dict[str, str] | None = None,
    *,
    already: bool = True,
    power: dict[str, float | None] | None = None,
    **overrides,
):
    value = {
        "broker_ok": True,
        "accepted": True,
        "desired_state": "off",
        "already_in_state": already,
        "partial": False,
        "entities": dict({GIGABYTE: "off", OLED: "off"} if states is None else states),
        "power_w": dict(power or {GIGABYTE: 0.0, OLED: 0.0}),
    }
    value.update(overrides)
    return value


class OfflineDesktop(Operations):
    """A powered-off desktop that answers SSH only after a wake was sent.

    Every monitor call and wake lands in one ordered event log, and each monitor
    call consumes the next scripted broker result (the last one repeats).
    """

    def __init__(self, script, *, wake_ok: bool = True) -> None:
        super().__init__()
        self.ssh = False
        self.script = list(script)
        self.wake_ok = wake_ok
        self.events: list[str] = []

    def ssh_ready(self, _timeout: float) -> bool:
        return self.wakes > 0 and self.wake_ok

    def send_wake(self, timeout: float) -> bool:
        self.events.append("wake")
        super().send_wake(timeout)
        return self.wake_ok

    def set_monitor_power(self, desired: str, _timeout: float):
        self.monitor_requests.append(desired)
        self.events.append(f"monitor_{desired}")
        if desired != "off":
            return {
                "broker_ok": True,
                "accepted": True,
                "desired_state": desired,
                "already_in_state": False,
                "partial": False,
                "entities": {GIGABYTE: "on", OLED: "off"},
                "preserved_entity_unchanged": True,
            }
        value = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return None if value is None else dict(value)

    def sunshine_status(self, timeout: float):
        self.events.append("sunshine_status")
        return super().sunshine_status(timeout)


def test_offline_gaming_verifies_both_outlets_off_before_the_only_wake() -> None:
    operations = OfflineDesktop(
        [
            # The command turns both outlets off ...
            _monitor_result(already=False),
            # ... and a separate read confirms them, changing nothing.
            _monitor_result(),
        ]
    )

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["status"] == "ready"
    assert result["verification_complete"] is True
    assert operations.events[:3] == ["monitor_off", "monitor_off", "wake"]
    assert operations.wakes == 1
    # Virtual display and capture are verified after the wake, then the final
    # call confirms the outlets stayed off, then capture is re-verified.
    assert operations.events[3:] == [
        "sunshine_status",
        "monitor_off",
        "sunshine_status",
    ]
    assert result["virtual_display_ready"] is True
    assert result["sunshine_capture_ready"] is True
    assert result["monitors_remained_off"] is True
    verification = result["pre_wake_monitor_verification"]
    assert verification["reads"] == 2
    assert verification["entities"] == {GIGABYTE: "off", OLED: "off"}
    assert verification["power_evidence"] == "metered"
    assert "on" not in operations.monitor_requests


def test_outlets_already_off_need_one_unchanged_read_before_the_wake() -> None:
    operations = OfflineDesktop([_monitor_result()])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["status"] == "ready"
    assert operations.events[:2] == ["monitor_off", "wake"]
    assert result["pre_wake_monitor_verification"]["reads"] == 1


@pytest.mark.parametrize(
    ("monitor", "code"),
    (
        # Home Assistant or the broker did not answer at all.
        (None, "home_assistant_unavailable"),
        # The broker envelope failed.
        (_monitor_result(broker_ok=False), "monitor_transition_failed"),
        # No decided acknowledgement.
        (_monitor_result(accepted=None), "monitor_transition_failed"),
        (_monitor_result(accepted=False, partial=True), "monitor_transition_partial"),
        # Acknowledged, but the evidence does not prove both outlets off.
        (_monitor_result({GIGABYTE: "off", OLED: "unavailable"}), "monitor_off_unverified"),
        (_monitor_result({GIGABYTE: "off", OLED: "unknown"}), "monitor_off_unverified"),
        (_monitor_result({GIGABYTE: "off"}), "monitor_off_unverified"),
        (_monitor_result({}), "monitor_off_unverified"),
        (
            _monitor_result({GIGABYTE: "off", OLED: "off", "switch.other": "off"}),
            "monitor_off_unverified",
        ),
        (_monitor_result(partial=None), "monitor_off_unverified"),
        (
            {k: v for k, v in _monitor_result().items() if k != "broker_ok"},
            "monitor_off_unverified",
        ),
    ),
)
def test_unverified_monitor_state_blocks_the_wake(monitor, code) -> None:
    operations = OfflineDesktop([monitor])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["failure_code"] == code
    assert result["wake_sent"] is False
    assert operations.wakes == 0
    assert "wake" not in operations.events
    # Nothing tries to reverse or re-drive the outlets after a failure.
    assert operations.monitor_requests == ["off"]


def test_home_assistant_failure_during_confirmation_blocks_the_wake() -> None:
    operations = OfflineDesktop([_monitor_result(already=False), None])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["failure_code"] == "home_assistant_unavailable"
    assert result["status"] == "partial"
    assert operations.wakes == 0
    assert operations.events == ["monitor_off", "monitor_off"]


def test_command_acknowledgement_alone_never_satisfies_the_wake() -> None:
    # Every read reports that it had to change an outlet: something keeps
    # turning one back on, so no read is ever an unchanged confirmation.
    operations = OfflineDesktop([_monitor_result(already=False)])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["failure_code"] == "monitor_off_unstable"
    assert operations.wakes == 0
    assert operations.events == ["monitor_off", "monitor_off"]


def test_lagging_meter_delays_the_wake_until_it_agrees() -> None:
    operations = OfflineDesktop(
        [
            _monitor_result(already=False, power={GIGABYTE: 31.5, OLED: 0.0}),
            _monitor_result(power={GIGABYTE: 31.5, OLED: 0.0}),
            _monitor_result(),
        ]
    )

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["status"] == "ready"
    assert operations.events[:4] == [
        "monitor_off",
        "monitor_off",
        "monitor_off",
        "wake",
    ]
    assert result["pre_wake_monitor_verification"]["power_w"] == {
        GIGABYTE: 0.0,
        OLED: 0.0,
    }


def test_meter_that_never_agrees_blocks_the_wake_within_its_bound() -> None:
    operations = OfflineDesktop([_monitor_result(power={GIGABYTE: 0.0, OLED: 42.0})])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["failure_code"] == "monitor_power_unsettled"
    assert operations.wakes == 0
    # Bounded by the confirmation window, not the 180s workflow budget.
    assert result["elapsed_ms"] <= 25_000


def test_unreadable_meter_falls_back_to_verified_outlet_state() -> None:
    operations = OfflineDesktop([_monitor_result(power={GIGABYTE: None, OLED: None})])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["status"] == "ready"
    assert result["pre_wake_monitor_verification"]["power_evidence"] == "unavailable"
    assert any("monitor_power_unmetered" in w for w in result["warnings"])


def test_wake_failure_after_verified_off_leaves_outlets_off() -> None:
    operations = OfflineDesktop([_monitor_result()], wake_ok=False)

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["failure_code"] == "desktop_wake_failed"
    assert result["status"] == "partial"
    assert result["wake_sent"] is False
    assert operations.events == ["monitor_off", "wake"]
    assert operations.wakes == 1
    assert "wake_on_lan" in result["committed_side_effects"]
    assert "left off" in result["safe_recovery"]


def test_stale_verification_cannot_satisfy_the_wake(monkeypatch) -> None:
    from butters.integrations import desktop as desktop_module

    # Any verification older than the bound is discarded, even a good one.
    monkeypatch.setattr(desktop_module, "PRE_WAKE_VERIFICATION_MAX_AGE_SECONDS", 0.0)
    operations = OfflineDesktop([_monitor_result()])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["failure_code"] == "monitor_off_verification_stale"
    assert operations.wakes == 0


def test_an_earlier_request_verification_is_never_reused() -> None:
    operations = OfflineDesktop([_monitor_result()], wake_ok=False)
    desktop = workflow(operations)
    first = desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING)
    assert first["pre_wake_monitor_verification"] is not None

    # Home Assistant is gone now: the next request must not wake on the
    # strength of the earlier request's reads.
    operations.script = [None]
    second = desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING)

    assert second["failure_code"] == "home_assistant_unavailable"
    assert second["pre_wake_monitor_verification"] is None
    assert operations.wakes == 1


def test_cancellation_between_verification_and_wake_sends_nothing() -> None:
    cancel = threading.Event()

    class CancelAfterVerify(OfflineDesktop):
        def set_monitor_power(self, desired: str, timeout: float):
            value = super().set_monitor_power(desired, timeout)
            cancel.set()
            return value

    operations = CancelAfterVerify([_monitor_result()])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING, cancel_event=cancel
    )

    assert result["failure_code"] == "cancelled"
    assert operations.wakes == 0


def test_repeated_requests_wake_once_then_reuse_the_running_desktop() -> None:
    operations = OfflineDesktop([_monitor_result()])
    desktop = workflow(operations)

    first = desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING)
    second = desktop.prepare_access_mode("desktop", DesktopAccessMode.GAMING)

    assert first["status"] == second["status"] == "ready"
    assert first["wake_sent"] is True
    assert second["wake_sent"] is False
    assert second["pre_wake_monitor_verification"] is None
    assert operations.wakes == 1
    assert operations.monitor_requests.count("on") == 0


def test_online_desktop_verifies_capture_before_its_single_monitor_off() -> None:
    operations = OfflineDesktop([_monitor_result(already=False)])
    operations.wakes = 1  # already running: SSH answers, nothing is woken
    operations.events.clear()

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["status"] == "ready"
    assert result["wake_sent"] is False
    assert result["pre_wake_monitor_verification"] is None
    assert result["monitors_remained_off"] is None
    assert operations.events == ["sunshine_status", "monitor_off", "sunshine_status"]
    assert operations.wakes == 1


def test_offline_local_verifies_off_before_wake_then_restores_the_primary() -> None:
    operations = OfflineDesktop([_monitor_result(already=False), _monitor_result()])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["status"] == "ready"
    assert operations.events == [
        "monitor_off",
        "monitor_off",
        "wake",
        "monitor_primary_on",
    ]


def test_online_local_is_unchanged_by_the_wake_gate() -> None:
    operations = OfflineDesktop([_monitor_result()])
    operations.wakes = 1
    operations.events.clear()

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.LOCAL
    )

    assert result["status"] == "ready"
    assert operations.events == ["monitor_primary_on"]
    assert result["pre_wake_monitor_verification"] is None


def test_legacy_boolean_monitor_protocol_cannot_satisfy_the_wake() -> None:
    class Legacy(OfflineDesktop):
        set_monitor_power = None  # type: ignore[assignment]

        def request_headless_mode(self, _timeout: float) -> bool:
            self.events.append("monitor_off")
            return True

    operations = Legacy([_monitor_result()])

    result = workflow(operations).prepare_access_mode(
        "desktop", DesktopAccessMode.GAMING
    )

    assert result["failure_code"] == "monitor_off_unverified"
    assert operations.wakes == 0
