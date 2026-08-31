from __future__ import annotations

import threading
import time
from dataclasses import replace

from butters.assistant_config import load_assistant_settings
from butters.integrations.desktop import DesktopWorkflow
from butters.integrations.model import IntegrationError


class Operations:
    def __init__(
        self,
        *,
        network: list[bool],
        ssh: list[bool],
        parsec: list[bool | None],
        wake: bool = True,
        ensure: bool = True,
        remote: bool = True,
    ) -> None:
        self.network_values = network
        self.ssh_values = ssh
        self.parsec_values = parsec
        self.wake = wake
        self.ensure = ensure
        self.remote = remote
        self.wake_calls = 0
        self.ensure_calls = 0
        self.remote_calls = 0
        self.calls: list[str] = []

    @staticmethod
    def _next(values):
        return values.pop(0) if len(values) > 1 else values[0]

    def network_reachable(self, _timeout_seconds: float) -> bool:
        self.calls.append("network")
        return self._next(self.network_values)

    def ssh_ready(self, _timeout_seconds: float) -> bool:
        self.calls.append("ssh")
        return self._next(self.ssh_values)

    def parsec_ready(self, _timeout_seconds: float) -> bool | None:
        self.calls.append("parsec_status")
        return self._next(self.parsec_values)

    def parsec_status(self, timeout_seconds: float):
        ready = self.parsec_ready(timeout_seconds)
        return None if ready is None else {"plausibly_ready": ready}

    def send_wake(self, _timeout_seconds: float) -> bool:
        self.calls.append("wake")
        self.wake_calls += 1
        return self.wake

    def ensure_parsec_running(self, _timeout_seconds: float) -> bool:
        self.calls.append("ensure_parsec")
        self.ensure_calls += 1
        return self.ensure

    def request_headless_mode(self, _timeout_seconds: float) -> bool:
        self.calls.append("headless")
        self.remote_calls += 1
        return self.remote


class Clock:
    def __init__(self, step: float = 0.01) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def _workflow(operations: Operations, *, step: float = 0.01) -> DesktopWorkflow:
    settings = replace(
        load_assistant_settings().desktop,
        poll_interval_seconds=0.0,
        network_timeout_seconds=1.0,
        ssh_timeout_seconds=1.0,
        total_timeout_seconds=3.0,
    )
    return DesktopWorkflow(settings, operations, clock=Clock(step))


def test_desktop_already_fully_ready_still_enters_headless_mode() -> None:
    operations = Operations(network=[True], ssh=[True], parsec=[True])
    result = _workflow(operations).start_remote_session("desktop")
    assert result["verification_complete"] is True
    assert result["wake_sent"] is False
    assert result["parsec_ensure_succeeded"] is True
    assert result["headless_mode_requested"] is True
    assert operations.wake_calls == 0
    assert operations.ensure_calls == 1
    assert operations.remote_calls == 1


def test_reachable_desktop_requests_headless_mode_when_parsec_not_ready() -> None:
    operations = Operations(network=[True], ssh=[True], parsec=[False, True])
    result = _workflow(operations).start_remote_session("desktop")
    assert result["headless_mode_requested"] is True
    assert result["parsec_ready"] is True
    assert result["verification_complete"] is True
    assert operations.calls.index("ensure_parsec") < operations.calls.index(
        "parsec_status"
    ) < operations.calls.index("headless")


def test_offline_desktop_wakes_then_waits_for_network_and_ssh() -> None:
    operations = Operations(
        network=[False, False, True],
        ssh=[False, True],
        parsec=[False, True],
    )
    result = _workflow(operations).start_remote_session("desktop")
    assert result["wake_sent"] is True
    assert result["network_reachable"] is True
    assert result["ssh_ready"] is True
    assert result["parsec_ensure_succeeded"] is True
    assert result["headless_mode_requested"] is True
    assert operations.calls.index("wake") < operations.calls.index("ensure_parsec")


def test_wol_failure_stops_before_remote_script() -> None:
    operations = Operations(network=[False], ssh=[False], parsec=[False], wake=False)
    result = _workflow(operations).start_remote_session("desktop")
    assert result["failed_stage"] == "wake_sent"
    assert operations.ensure_calls == 0
    assert operations.remote_calls == 0


def test_network_and_ssh_timeouts_are_distinct() -> None:
    network_timeout = _workflow(
        Operations(network=[False], ssh=[False], parsec=[False]), step=0.05
    ).start_remote_session("desktop")
    ssh_timeout = _workflow(
        Operations(network=[True], ssh=[False], parsec=[False]), step=0.05
    ).start_remote_session("desktop")
    assert network_timeout["failed_stage"] == "network_reachable"
    assert ssh_timeout["failed_stage"] == "ssh_ready"


def test_parsec_ensure_and_headless_failures_are_distinct() -> None:
    ensure = _workflow(
        Operations(network=[True], ssh=[True], parsec=[True], ensure=False)
    ).start_remote_session("desktop")
    headless = _workflow(
        Operations(network=[True], ssh=[True], parsec=[True], remote=False)
    ).start_remote_session("desktop")
    assert ensure["failed_stage"] == "parsec_ensure"
    assert headless["failed_stage"] == "headless_mode_requested"


def test_unknown_parsec_state_is_not_reported_as_ready() -> None:
    result = _workflow(
        Operations(network=[True], ssh=[True], parsec=[None, None])
    ).start_remote_session("desktop")
    assert result["parsec_ready"] is None
    assert result["verification_complete"] is False
    assert result["headless_mode_requested"] is False
    assert result["failed_stage"] == "total_timeout"


def test_workflow_cancellation_prevents_every_operation() -> None:
    event = threading.Event()
    event.set()
    operations = Operations(network=[True], ssh=[True], parsec=[True])
    result = _workflow(operations).start_remote_session("desktop", cancel_event=event)
    assert result["failed_stage"] == "cancelled"
    assert operations.wake_calls == operations.ensure_calls == operations.remote_calls == 0


def test_total_timeout_is_bounded() -> None:
    result = _workflow(
        Operations(network=[False], ssh=[False], parsec=[False]), step=2.0
    ).start_remote_session("desktop")
    assert result["failed_stage"] == "total_timeout"
    assert int(result["elapsed_ms"]) >= 0


def test_invalid_machine_is_rejected_before_any_probe() -> None:
    operations = Operations(network=[True], ssh=[True], parsec=[True])
    try:
        _workflow(operations).start_remote_session("evil.example")
    except IntegrationError as exc:
        assert exc.code == "policy_denied"
    else:
        raise AssertionError("invalid machine was accepted")


def test_reachability_wait_polls_without_wake_or_monitor_side_effects() -> None:
    operations = Operations(
        network=[False, False, True],
        ssh=[False, False, False],
        parsec=[False],
    )

    result = _workflow(operations).wait_for_reachability("desktop")

    assert result["network_reachable"] is True
    assert result["timed_out"] is False
    assert result["cancelled"] is False
    assert operations.wake_calls == 0
    assert operations.remote_calls == 0


def test_reachability_wait_reports_its_fixed_timeout_truthfully() -> None:
    operations = Operations(network=[False], ssh=[False], parsec=[False])

    result = _workflow(operations, step=0.05).wait_for_reachability("desktop")

    assert result["network_reachable"] is False
    assert result["timed_out"] is True
    assert result["cancelled"] is False
    assert operations.wake_calls == operations.remote_calls == 0


def test_reachability_wait_respects_cancellation_before_any_probe() -> None:
    event = threading.Event()
    event.set()
    operations = Operations(network=[True], ssh=[True], parsec=[True])

    result = _workflow(operations).wait_for_reachability(
        "desktop", cancel_event=event
    )

    assert result["cancelled"] is True
    assert result["timed_out"] is False
    assert operations.network_values == [True]
    assert operations.ssh_values == [True]
    assert operations.wake_calls == operations.remote_calls == 0


def test_reachability_wait_respects_cancellation_between_polls() -> None:
    settings = replace(
        load_assistant_settings().desktop,
        poll_interval_seconds=0.01,
        network_timeout_seconds=1.0,
        total_timeout_seconds=1.0,
    )
    operations = Operations(network=[False], ssh=[False], parsec=[False])
    event = threading.Event()
    timer = threading.Timer(0.03, event.set)
    timer.start()
    try:
        result = DesktopWorkflow(settings, operations, clock=time.monotonic).wait_for_reachability(
            "desktop", cancel_event=event
        )
    finally:
        timer.cancel()

    assert result["cancelled"] is True
    assert result["timed_out"] is False
    assert operations.wake_calls == operations.remote_calls == 0


def test_reachability_attempts_share_one_absolute_deadline() -> None:
    class ManualClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = ManualClock()

    class BlockingOperations(Operations):
        def __init__(self) -> None:
            super().__init__(network=[False], ssh=[False], parsec=[False])
            self.timeouts: list[tuple[str, float]] = []

        def ssh_ready(self, timeout_seconds: float) -> bool:
            self.timeouts.append(("ssh", timeout_seconds))
            clock.value += min(0.2, timeout_seconds)
            return False

        def network_reachable(self, timeout_seconds: float) -> bool:
            self.timeouts.append(("network", timeout_seconds))
            clock.value += timeout_seconds
            return False

    settings = replace(
        load_assistant_settings().desktop,
        poll_interval_seconds=0.0,
        network_timeout_seconds=0.5,
        total_timeout_seconds=0.5,
    )
    operations = BlockingOperations()

    result = DesktopWorkflow(settings, operations, clock=clock).wait_for_reachability(
        "desktop"
    )

    assert result["timed_out"] is True
    assert result["elapsed_ms"] == 500
    assert operations.timeouts == [("ssh", 0.5), ("network", 0.3)]
