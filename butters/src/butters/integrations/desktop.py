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
    def network_reachable(self) -> bool: ...

    def ssh_ready(self) -> bool: ...

    def parsec_ready(self) -> bool | None: ...

    def send_wake(self) -> bool: ...

    def request_headless_mode(self) -> bool: ...


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

    def network_reachable(self) -> bool:
        try:
            result = self.ping(
                ["/usr/bin/ping", "-c", "1", "-W", "1", self.settings.host],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return int(getattr(result, "returncode", 1)) == 0

    def ssh_ready(self) -> bool:
        try:
            connection = self.connector(
                (self.settings.host, self.settings.ssh_port), timeout=1.0
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

    def parsec_ready(self) -> bool | None:
        # The inspected scripts expose no read-only Parsec process/status probe.
        # Unknown is safer than treating ping or SSH as application readiness.
        return None

    def send_wake(self) -> bool:
        return self._broker(BrokerOperation.DESKTOP_WAKE)

    def request_headless_mode(self) -> bool:
        return self._broker(BrokerOperation.DESKTOP_MONITORS_OFF)

    def _broker(self, operation: BrokerOperation) -> bool:
        try:
            result = self.broker.request(
                operation,
                request_id=secrets.token_urlsafe(18),
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
        network = self.operations.network_reachable()
        ssh = self.operations.ssh_ready()
        network = network or ssh
        parsec = self.operations.parsec_ready() if ssh else False
        return DesktopState(machine, network, ssh, parsec)

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

        network = self.operations.network_reachable()
        ssh = self.operations.ssh_ready()
        network = network or ssh
        result["network_reachable"] = network
        if not network:
            if not self.operations.send_wake():
                return finish("wake_sent", "fixed wake operation failed")
            result["wake_sent"] = True
            network_deadline = min(
                deadline, self.clock() + self.settings.network_timeout_seconds
            )
            network = self._wait_for(
                self.operations.network_reachable, network_deadline, event
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
            ssh = self._wait_for(self.operations.ssh_ready, ssh_deadline, event)
        result["ssh_ready"] = ssh
        if not ssh:
            if event.is_set():
                return finish("cancelled", "workflow cancelled")
            if self.clock() >= deadline:
                return finish("total_timeout", "workflow deadline expired")
            return finish("ssh_ready", "desktop remote-management readiness timed out")

        ready = self.operations.parsec_ready()
        result["parsec_ready"] = ready
        if event.is_set():
            return finish("cancelled", "workflow cancelled")
        if self.clock() >= deadline:
            return finish("total_timeout", "workflow deadline expired")
        if not self.operations.request_headless_mode():
            return finish(
                "headless_mode_requested", "monitor power-off operation failed"
            )
        result["headless_mode_requested"] = True

        if ready is True:
            result["verification_complete"] = True
            return finish()

        if event.is_set():
            return finish("cancelled", "workflow cancelled")
        if self.clock() >= deadline:
            return finish("total_timeout", "workflow deadline expired")
        verified = self.operations.parsec_ready()
        result["parsec_ready"] = verified
        if verified is False:
            return finish("verification", "remote workflow verification failed")
        # None is an explicit UNKNOWN: there is no independent Parsec process
        # probe. Monitor power was accepted, but application readiness is not
        # overstated.
        result["verification_complete"] = verified is True
        if verified is None:
            result["verification_note"] = (
                "physical monitors were powered off; Parsec readiness is not independently observable"
            )
        return finish()

    def _wait_for(
        self,
        predicate: Callable[[], bool],
        deadline: float,
        event: threading.Event,
    ) -> bool:
        while not self._cancelled(event, deadline):
            if predicate():
                return True
            remaining = deadline - self.clock()
            event.wait(min(self.settings.poll_interval_seconds, max(0.0, remaining)))
        return False

    def _cancelled(self, event: threading.Event, deadline: float) -> bool:
        return event.is_set() or self.clock() >= deadline

    def _require_machine(self, machine: str) -> None:
        if not self.settings.enabled:
            raise IntegrationError("unavailable", "desktop integration is disabled")
        if machine != self.settings.machine or machine != "desktop":
            raise IntegrationError("policy_denied", "machine is not allow-listed")
