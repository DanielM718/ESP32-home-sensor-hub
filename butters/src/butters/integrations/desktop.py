"""Fixed, bounded Windows desktop observation and headless-session workflow."""

from __future__ import annotations

import secrets
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from butters.actions.broker import BrokerClient, BrokerError, BrokerOperation
from butters.assistant_config import BrokerSettings, DesktopSettings
from butters.integrations.model import IntegrationError

NETWORK_ATTEMPT_TIMEOUT_SECONDS = 2.0
SSH_ATTEMPT_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class DesktopState:
    machine: str
    network_reachable: bool
    ssh_ready: bool
    parsec_ready: bool | None
    observed: bool = True

    def safe_dict(self) -> dict[str, object]:
        return {
            "machine": self.machine,
            "network_reachable": self.network_reachable,
            "ssh_ready": self.ssh_ready,
            "parsec_ready": self.parsec_ready,
            "observed": self.observed,
        }


class DesktopOperations(Protocol):
    def network_reachable(self, timeout_seconds: float) -> bool: ...

    def ssh_ready(self, timeout_seconds: float) -> bool: ...

    def parsec_ready(self, timeout_seconds: float) -> bool | None: ...

    def parsec_status(self, timeout_seconds: float) -> dict[str, object] | None: ...

    def send_wake(self, timeout_seconds: float) -> bool: ...

    def ensure_parsec_running(self, timeout_seconds: float) -> bool: ...

    def request_headless_mode(self, timeout_seconds: float) -> bool: ...


class BrokerDesktopOperations:
    """Read locally and cross the enumerated broker boundary for mutations."""

    def __init__(
        self,
        settings: DesktopSettings,
        *,
        broker: BrokerClient,
        ping: Callable[..., Any] = subprocess.run,
        connector: Callable[..., Any] = socket.create_connection,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.ping = ping
        self.connector = connector

    def network_reachable(self, timeout_seconds: float) -> bool:
        attempt_timeout = min(NETWORK_ATTEMPT_TIMEOUT_SECONDS, timeout_seconds)
        if attempt_timeout <= 0:
            return False
        try:
            result = self.ping(
                ["/usr/bin/ping", "-c", "1", "-W", "1", self.settings.host],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=attempt_timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return int(getattr(result, "returncode", 1)) == 0

    def ssh_ready(self, timeout_seconds: float) -> bool:
        attempt_timeout = min(SSH_ATTEMPT_TIMEOUT_SECONDS, timeout_seconds)
        if attempt_timeout <= 0:
            return False
        try:
            connection = self.connector(
                (self.settings.host, self.settings.ssh_port), timeout=attempt_timeout
            )
        except (OSError, TimeoutError):
            return False
        try:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        except OSError:
            pass
        return True

    def parsec_ready(self, timeout_seconds: float) -> bool | None:
        status = self.parsec_status(timeout_seconds)
        ready = None if status is None else status.get("plausibly_ready")
        return ready if isinstance(ready, bool) else None

    def parsec_status(self, timeout_seconds: float) -> dict[str, object] | None:
        try:
            result = self.broker.request(
                BrokerOperation.DESKTOP_PARSEC_STATUS,
                request_id=secrets.token_urlsafe(18),
                timeout_seconds=timeout_seconds,
            )
        except BrokerError:
            return None
        return dict(result.status) if result.ok else None

    def send_wake(self, timeout_seconds: float) -> bool:
        return self._broker(BrokerOperation.DESKTOP_WAKE, timeout_seconds)

    def ensure_parsec_running(self, timeout_seconds: float) -> bool:
        return self._broker(BrokerOperation.DESKTOP_PARSEC_ENSURE, timeout_seconds)

    def request_headless_mode(self, timeout_seconds: float) -> bool:
        return self._broker(BrokerOperation.DESKTOP_MONITORS_OFF, timeout_seconds)

    def _broker(self, operation: BrokerOperation, timeout_seconds: float) -> bool:
        try:
            result = self.broker.request(
                operation,
                request_id=secrets.token_urlsafe(18),
                timeout_seconds=timeout_seconds,
            )
        except BrokerError:
            return False
        return result.ok


class DesktopWorkflow:
    def __init__(
        self,
        settings: DesktopSettings,
        operations: DesktopOperations | None = None,
        *,
        broker_settings: BrokerSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.broker_settings = broker_settings or BrokerSettings()
        self.operations = operations or BrokerDesktopOperations(
            settings,
            broker=BrokerClient(self.broker_settings),
        )
        self.clock = clock

    def status(self, machine: str) -> DesktopState:
        self._require_machine(machine)
        deadline = self.clock() + self.settings.total_timeout_seconds
        ssh = self._ssh_ready(deadline)
        network = ssh or self._network_reachable(deadline)
        network = network or ssh
        parsec = self._parsec_ready(deadline) if ssh else False
        return DesktopState(machine, network, ssh, parsec)

    def parsec_status(self, machine: str) -> dict[str, object]:
        """Return only bounded user-relevant state for the fixed Parsec host."""

        self._require_machine(machine)
        deadline = self.clock() + self.settings.total_timeout_seconds
        ssh = self._ssh_ready(deadline)
        network = ssh or self._network_reachable(deadline)
        network = network or ssh
        status = (
            self.operations.parsec_status(self._remaining(deadline))
            if ssh and self._remaining(deadline) > 0
            else None
        )
        result: dict[str, object] = {
            "machine": machine,
            "desktop_reachable": network,
            "ssh_reachable": ssh,
            "installed": None,
            "installation_type": "unknown",
            "service_present": None,
            "service_running": None,
            "service_startup": "unknown",
            "service_process_present": None,
            "host_process_present": None,
            "system_host_process_present": None,
            "user_host_process_present": None,
            "plausibly_ready": False,
            "observed": status is not None,
        }
        if status is not None:
            result.update(status)
        return result

    def wait_for_reachability(
        self, machine: str, *, cancel_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Poll the fixed desktop until network/SSH reachability or timeout.

        This is the observation half of an authenticated wake plan. It never
        sends WOL, invokes SSH commands, or changes monitor state; sequencing in
        ActionCoordinator starts it only after the frozen wake action succeeds.
        """

        self._require_machine(machine)
        event = cancel_event or threading.Event()
        started = self.clock()
        deadline = min(
            started + self.settings.network_timeout_seconds,
            started + self.settings.total_timeout_seconds,
        )
        observed = {"network": False, "ssh": False}

        def reachable(_remaining: float) -> bool:
            ssh = self._ssh_ready(deadline)
            network = ssh or self._network_reachable(deadline)
            observed["network"] = network
            observed["ssh"] = ssh
            return network

        ready = self._wait_for(reachable, deadline, event)
        cancelled = event.is_set()
        parsec = self._parsec_ready(deadline) if ready and observed["ssh"] else False
        return {
            "machine": machine,
            "network_reachable": bool(ready and observed["network"]),
            "ssh_ready": bool(ready and observed["ssh"]),
            "parsec_ready": parsec,
            "timed_out": not ready and not cancelled,
            "cancelled": cancelled,
            "elapsed_ms": round((self.clock() - started) * 1000),
        }

    def start_remote_session(
        self, machine: str, *, cancel_event: threading.Event | None = None
    ) -> dict[str, object]:
        self._require_machine(machine)
        event = cancel_event or threading.Event()
        started = self.clock()
        deadline = started + self.settings.total_timeout_seconds
        result: dict[str, object] = {
            "machine": machine,
            "wake_sent": False,
            "network_reachable": False,
            "ssh_ready": False,
            "parsec_ensure_succeeded": False,
            "headless_mode_requested": False,
            "parsec_ready": None,
            "verification_complete": False,
            "elapsed_ms": 0,
            "failed_stage": None,
            "error": None,
        }

        def finish(
            stage: str | None = None, error: str | None = None
        ) -> dict[str, object]:
            result["failed_stage"] = stage
            result["error"] = error
            result["elapsed_ms"] = round((self.clock() - started) * 1000)
            return result

        if event.is_set():
            return finish("cancelled", "workflow cancelled")
        if self.clock() >= deadline:
            return finish("total_timeout", "workflow deadline expired")

        ssh = self._ssh_ready(deadline)
        network = ssh or self._network_reachable(deadline)
        result["network_reachable"] = network
        if not network:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return finish("total_timeout", "workflow deadline expired")
            if not self.operations.send_wake(remaining):
                return finish("wake_sent", "fixed wake operation failed")
            result["wake_sent"] = True
            network_deadline = min(
                deadline, self.clock() + self.settings.network_timeout_seconds
            )
            network = self._wait_for(
                lambda _remaining: self._network_reachable(network_deadline),
                network_deadline,
                event,
            )
            result["network_reachable"] = network
            if not network:
                if event.is_set():
                    return finish("cancelled", "workflow cancelled")
                if self.clock() >= deadline:
                    return finish("total_timeout", "workflow deadline expired")
                return finish(
                    "network_reachable", "desktop network readiness timed out"
                )

        ssh_deadline = min(deadline, self.clock() + self.settings.ssh_timeout_seconds)
        if not ssh:
            ssh = self._wait_for(
                lambda _remaining: self._ssh_ready(ssh_deadline), ssh_deadline, event
            )
        result["ssh_ready"] = ssh
        if not ssh:
            if event.is_set():
                return finish("cancelled", "workflow cancelled")
            if self.clock() >= deadline:
                return finish("total_timeout", "workflow deadline expired")
            return finish("ssh_ready", "desktop remote-management readiness timed out")

        if event.is_set():
            return finish("cancelled", "workflow cancelled")
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return finish("total_timeout", "workflow deadline expired")
        if not self.operations.ensure_parsec_running(remaining):
            return finish("parsec_ensure", "fixed on-demand Parsec operation failed")
        result["parsec_ensure_succeeded"] = True

        last_parsec_state: bool | None = None

        def parsec_ready(_remaining: float) -> bool:
            nonlocal last_parsec_state
            last_parsec_state = self._parsec_ready(deadline)
            return last_parsec_state is True

        ready = self._wait_for(parsec_ready, deadline, event)
        result["parsec_ready"] = True if ready else last_parsec_state
        if not ready:
            if event.is_set():
                return finish("cancelled", "workflow cancelled")
            if self.clock() >= deadline:
                return finish("total_timeout", "workflow deadline expired")
            return finish("parsec_ready", "Parsec readiness verification failed")

        remaining = self._remaining(deadline)
        if remaining <= 0:
            return finish("total_timeout", "workflow deadline expired")
        if not self.operations.request_headless_mode(remaining):
            return finish(
                "headless_mode_requested", "monitor power-off operation failed"
            )
        result["headless_mode_requested"] = True
        result["verification_complete"] = True
        return finish()

    def _wait_for(
        self,
        predicate: Callable[[float], bool],
        deadline: float,
        event: threading.Event,
    ) -> bool:
        while not self._cancelled(event, deadline):
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return False
            if predicate(remaining):
                return True
            remaining = self._remaining(deadline)
            event.wait(min(self.settings.poll_interval_seconds, max(0.0, remaining)))
        return False

    def _network_reachable(self, deadline: float) -> bool:
        remaining = self._remaining(deadline)
        return remaining > 0 and self.operations.network_reachable(
            min(NETWORK_ATTEMPT_TIMEOUT_SECONDS, remaining)
        )

    def _ssh_ready(self, deadline: float) -> bool:
        remaining = self._remaining(deadline)
        return remaining > 0 and self.operations.ssh_ready(
            min(SSH_ATTEMPT_TIMEOUT_SECONDS, remaining)
        )

    def _parsec_ready(self, deadline: float) -> bool | None:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return None
        return self.operations.parsec_ready(remaining)

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self.clock())

    def _cancelled(self, event: threading.Event, deadline: float) -> bool:
        return event.is_set() or self.clock() >= deadline

    def _require_machine(self, machine: str) -> None:
        if not self.settings.enabled:
            raise IntegrationError("unavailable", "desktop integration is disabled")
        if machine != self.settings.machine or machine != "desktop":
            raise IntegrationError("policy_denied", "machine is not allow-listed")
