from __future__ import annotations

import threading
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
        remote: bool = True,
    ) -> None:
        self.network_values = network
        self.ssh_values = ssh
        self.parsec_values = parsec
        self.wake = wake
        self.remote = remote
        self.wake_calls = 0
        self.remote_calls = 0

    @staticmethod
    def _next(values):
        return values.pop(0) if len(values) > 1 else values[0]

    def network_reachable(self) -> bool:
        return self._next(self.network_values)

    def ssh_ready(self) -> bool:
        return self._next(self.ssh_values)

    def parsec_ready(self) -> bool | None:
        return self._next(self.parsec_values)

    def send_wake(self) -> bool:
        self.wake_calls += 1
        return self.wake

    def request_remote_mode(self) -> bool:
        self.remote_calls += 1
        return self.remote


class Clock:
    def __init__(self, step: float = 0.1) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def _workflow(operations: Operations, *, step: float = 0.1) -> DesktopWorkflow:
    settings = replace(
        load_assistant_settings().desktop,
        poll_interval_seconds=0.0,
        network_timeout_seconds=1.0,
        ssh_timeout_seconds=1.0,
        total_timeout_seconds=3.0,
    )
    return DesktopWorkflow(settings, operations, clock=Clock(step))


def test_desktop_already_fully_ready_is_observation_only() -> None:
    operations = Operations(network=[True], ssh=[True], parsec=[True])
    result = _workflow(operations).start_remote_session("desktop")
    assert result["verification_complete"] is True
    assert result["wake_sent"] is False
    assert result["remote_mode_requested"] is False
    assert operations.wake_calls == operations.remote_calls == 0


def test_reachable_desktop_requests_fixed_remote_mode_when_parsec_not_ready() -> None:
    operations = Operations(network=[True], ssh=[True], parsec=[False, True])
    result = _workflow(operations).start_remote_session("desktop")
    assert result["remote_mode_requested"] is True
    assert result["parsec_ready"] is True
    assert result["verification_complete"] is True


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
    assert result["remote_mode_requested"] is True


def test_wol_failure_stops_before_remote_script() -> None:
    operations = Operations(network=[False], ssh=[False], parsec=[False], wake=False)
    result = _workflow(operations).start_remote_session("desktop")
    assert result["failed_stage"] == "wake_sent"
    assert operations.remote_calls == 0


def test_network_and_ssh_timeouts_are_distinct() -> None:
    network_timeout = _workflow(
        Operations(network=[False], ssh=[False], parsec=[False]), step=0.4
    ).start_remote_session("desktop")
    ssh_timeout = _workflow(
        Operations(network=[True], ssh=[False], parsec=[False]), step=0.4
    ).start_remote_session("desktop")
    assert network_timeout["failed_stage"] == "network_reachable"
    assert ssh_timeout["failed_stage"] == "ssh_ready"


def test_script_failure_and_verification_failure_are_distinct() -> None:
    script = _workflow(
        Operations(network=[True], ssh=[True], parsec=[False], remote=False)
    ).start_remote_session("desktop")
    verify = _workflow(
        Operations(network=[True], ssh=[True], parsec=[False, False])
    ).start_remote_session("desktop")
    assert script["failed_stage"] == "remote_mode_requested"
    assert verify["failed_stage"] == "verification"


def test_unknown_parsec_state_is_not_reported_as_ready() -> None:
    result = _workflow(
        Operations(network=[True], ssh=[True], parsec=[None, None])
    ).start_remote_session("desktop")
    assert result["parsec_ready"] is None
    assert result["verification_complete"] is False
    assert "not independently observable" in str(result["verification_note"])


def test_workflow_cancellation_prevents_every_operation() -> None:
    event = threading.Event()
    event.set()
    operations = Operations(network=[True], ssh=[True], parsec=[True])
    result = _workflow(operations).start_remote_session("desktop", cancel_event=event)
    assert result["failed_stage"] == "cancelled"
    assert operations.wake_calls == operations.remote_calls == 0


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
