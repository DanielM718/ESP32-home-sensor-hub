"""Fixed, bounded Windows desktop observation and headless-session workflow."""

from __future__ import annotations

import ipaddress
import secrets
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, cast

from butters.actions.broker import (
    MONITOR_OFF_POWER_CEILING_W,
    BrokerClient,
    BrokerError,
    BrokerOperation,
    FixedBrokerOperations,
)
from butters.assistant_config import BrokerSettings, DesktopSettings
from butters.integrations.model import IntegrationError

NETWORK_ATTEMPT_TIMEOUT_SECONDS = 2.0
SSH_ATTEMPT_TIMEOUT_SECONDS = 1.0
RESOLUTION_ATTEMPT_TIMEOUT_SECONDS = 1.0
# Resolve the fixed mDNS name in a killable helper process. socket's connect
# timeout begins only after getaddrinfo returns, so passing a timeout to
# create_connection does not otherwise enforce the workflow's wall-clock
# deadline. The ping subprocess is independently capped.
SSH_OBSERVATION_WORST_CASE_SECONDS = (
    RESOLUTION_ATTEMPT_TIMEOUT_SECONDS + SSH_ATTEMPT_TIMEOUT_SECONDS
)
STATUS_WORST_CASE_SECONDS = (
    SSH_OBSERVATION_WORST_CASE_SECONDS + NETWORK_ATTEMPT_TIMEOUT_SECONDS
)
# Registered ceiling for the read skill, with headroom over that worst case. It
# has to exceed it: a skill deadline shorter than one observation makes every
# field `unknown` and hides the powered-off state the probes actually measured.
STATUS_TIMEOUT_SECONDS = 10.0
POST_HEADLESS_SETTLE_SECONDS = 5.0
# A monitor needs ~15s to power on, and Windows then enumerates it. Bounded by
# the workflow deadline as well.
PHYSICAL_DISPLAY_SETTLE_SECONDS = 30.0
# Sunshine stops a session within ~150ms of its application ending, then
# closes the stream sockets when the last session reference drops.
SESSION_END_VERIFY_SECONDS = 15.0
SESSION_OBSERVATION_SECONDS = 30.0
# Before any Wake-on-LAN, both outlets must be proven off by a read in the same
# request. A P115 meter reports on its own poll, so a draw measured just before
# the relay opened can linger for a few seconds; this bounds the wait for a
# reading that agrees with "off". The broker re-checks immediately before the
# packet, and a verification older than the age below is never relied on.
PRE_WAKE_MONITOR_CONFIRM_SECONDS = 20.0
PRE_WAKE_VERIFICATION_MAX_AGE_SECONDS = 10.0
SUNSHINE_SESSION_STATES = frozenset(
    {"terminated", "resumable", "active", "ending", "unknown"}
)


@dataclass(frozen=True, slots=True)
class DesktopState:
    machine: str
    network_reachable: bool
    ssh_ready: bool
    parsec_ready: bool | None
    sunshine_ready: bool | None = None
    observed: bool = True
    online: bool | None = None
    machine_state: str = "unknown"
    online_evidence: tuple[str, ...] = ()
    agent_connected: bool = False
    agent_heartbeat_age_seconds: float | None = None
    sunshine_service_ready: bool | None = None
    sunshine_observed_at: float | None = None

    def safe_dict(self) -> dict[str, object]:
        return {
            "machine": self.machine,
            "network_reachable": self.network_reachable,
            "ssh_ready": self.ssh_ready,
            "parsec_ready": self.parsec_ready,
            "sunshine_ready": self.sunshine_ready,
            "observed": self.observed,
            "online": self.online,
            "machine_state": self.machine_state,
            "online_evidence": list(self.online_evidence),
            "agent_connected": self.agent_connected,
            "agent_heartbeat_age_seconds": self.agent_heartbeat_age_seconds,
            "sunshine_service_ready": self.sunshine_service_ready,
            "sunshine_observed_at": self.sunshine_observed_at,
        }


class DesktopAccessMode(str, Enum):
    GAMING = "gaming"
    ENGINEERING = "engineering"
    LOCAL = "local"


class DesktopOperations(Protocol):
    def network_reachable(self, timeout_seconds: float) -> bool: ...

    def ssh_ready(self, timeout_seconds: float) -> bool: ...

    def parsec_ready(self, timeout_seconds: float) -> bool | None: ...

    def parsec_status(self, timeout_seconds: float) -> dict[str, object] | None: ...

    def sunshine_ready(self, timeout_seconds: float) -> bool | None: ...

    def sunshine_status(self, timeout_seconds: float) -> dict[str, object] | None: ...

    def sunshine_service_status(
        self, timeout_seconds: float
    ) -> dict[str, object] | None: ...

    def send_wake(self, timeout_seconds: float) -> bool: ...

    def ensure_parsec_running(self, timeout_seconds: float) -> bool: ...

    def ensure_sunshine_running(self, timeout_seconds: float) -> bool: ...

    def request_headless_mode(self, timeout_seconds: float) -> bool: ...

    def restore_headed_mode(self, timeout_seconds: float) -> bool: ...

    def set_monitor_power(
        self, desired_state: str, timeout_seconds: float
    ) -> dict[str, object] | None: ...

    def display_status(self, timeout_seconds: float) -> dict[str, object] | None: ...

    def activate_virtual_display(
        self, timeout_seconds: float
    ) -> dict[str, object] | None: ...

    def deactivate_virtual_display(
        self, timeout_seconds: float
    ) -> dict[str, object] | None: ...

    def sunshine_session_status(
        self, timeout_seconds: float
    ) -> dict[str, object] | None: ...

    def end_sunshine_session(
        self, timeout_seconds: float
    ) -> dict[str, object] | None: ...


class BrokerDesktopOperations:
    """Read locally and cross the enumerated broker boundary for mutations."""

    def __init__(
        self,
        settings: DesktopSettings,
        *,
        broker: BrokerClient,
        ping: Callable[..., Any] = subprocess.run,
        resolver: Callable[..., Any] = subprocess.run,
        connector: Callable[..., Any] = socket.create_connection,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.ping = ping
        self.resolver = resolver
        self.connector = connector
        self.clock = clock

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
        started = self.clock()
        host = self._resolved_host(timeout_seconds)
        remaining = timeout_seconds - (self.clock() - started)
        attempt_timeout = min(SSH_ATTEMPT_TIMEOUT_SECONDS, remaining)
        if attempt_timeout <= 0:
            return False
        if host is None:
            return False
        try:
            connection = self.connector(
                (host, self.settings.ssh_port), timeout=attempt_timeout
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

    def _resolved_host(self, timeout_seconds: float) -> str | None:
        try:
            return str(ipaddress.ip_address(self.settings.host))
        except ValueError:
            pass
        attempt_timeout = min(RESOLUTION_ATTEMPT_TIMEOUT_SECONDS, timeout_seconds)
        if attempt_timeout <= 0:
            return None
        try:
            result = self.resolver(
                ["/usr/bin/getent", "ahostsv4", self.settings.host],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=attempt_timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if int(getattr(result, "returncode", 1)) != 0:
            return None
        for line in str(getattr(result, "stdout", "")).splitlines():
            fields = line.split(maxsplit=1)
            candidate = fields[0] if fields else ""
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.version == 4:
                return str(address)
        return None

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

    def sunshine_ready(self, timeout_seconds: float) -> bool | None:
        status = self.sunshine_status(timeout_seconds)
        ready = None if status is None else status.get("plausibly_ready")
        return ready if isinstance(ready, bool) else None

    def sunshine_status(self, timeout_seconds: float) -> dict[str, object] | None:
        try:
            result = self.broker.request(
                BrokerOperation.DESKTOP_SUNSHINE_STATUS,
                request_id=secrets.token_urlsafe(18),
                timeout_seconds=timeout_seconds,
            )
        except BrokerError:
            return None
        return dict(result.status) if result.ok else None

    def sunshine_service_status(
        self, timeout_seconds: float
    ) -> dict[str, object] | None:
        try:
            result = self.broker.request(
                BrokerOperation.DESKTOP_SUNSHINE_SERVICE_STATUS,
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

    def ensure_sunshine_running(self, timeout_seconds: float) -> bool:
        return self._broker(BrokerOperation.DESKTOP_SUNSHINE_ENSURE, timeout_seconds)

    def request_headless_mode(self, timeout_seconds: float) -> bool:
        return self._broker(BrokerOperation.DESKTOP_MONITORS_OFF, timeout_seconds)

    def restore_headed_mode(self, timeout_seconds: float) -> bool:
        return self._broker(BrokerOperation.DESKTOP_MONITORS_ON, timeout_seconds)

    def set_monitor_power(
        self, desired_state: str, timeout_seconds: float
    ) -> dict[str, object] | None:
        # An explicit table: an unknown value must never fall through to the
        # operation that powers both outlets, including the intentionally-off
        # OLED.
        operation = {
            "off": BrokerOperation.DESKTOP_MONITORS_OFF,
            "on": BrokerOperation.DESKTOP_MONITORS_ON,
            "primary_on": BrokerOperation.DESKTOP_PRIMARY_MONITOR_ON,
        }.get(desired_state)
        if operation is None:
            raise ValueError("monitor state is not allow-listed")
        return self._mutation(operation, timeout_seconds)

    def display_status(self, timeout_seconds: float) -> dict[str, object] | None:
        return self._observation(BrokerOperation.DESKTOP_DISPLAY_STATUS, timeout_seconds)

    def activate_virtual_display(
        self, timeout_seconds: float
    ) -> dict[str, object] | None:
        return self._mutation(
            BrokerOperation.DESKTOP_VIRTUAL_DISPLAY_ACTIVATE, timeout_seconds
        )

    def deactivate_virtual_display(
        self, timeout_seconds: float
    ) -> dict[str, object] | None:
        return self._mutation(
            BrokerOperation.DESKTOP_VIRTUAL_DISPLAY_DEACTIVATE, timeout_seconds
        )

    def sunshine_session_status(
        self, timeout_seconds: float
    ) -> dict[str, object] | None:
        return self._observation(
            BrokerOperation.DESKTOP_SUNSHINE_SESSION_STATUS, timeout_seconds
        )

    def end_sunshine_session(self, timeout_seconds: float) -> dict[str, object] | None:
        return self._mutation(
            BrokerOperation.DESKTOP_SUNSHINE_SESSION_END, timeout_seconds
        )

    def _observation(
        self, operation: BrokerOperation, timeout_seconds: float
    ) -> dict[str, object] | None:
        try:
            result = self.broker.request(
                operation,
                request_id=secrets.token_urlsafe(18),
                timeout_seconds=timeout_seconds,
            )
        except BrokerError:
            return None
        return dict(result.status) if result.ok else None

    def _mutation(
        self, operation: BrokerOperation, timeout_seconds: float
    ) -> dict[str, object] | None:
        """Return the broker's decided outcome, or None if none was received.

        None means the outcome is unknown, not that nothing changed: callers
        record the side effect as possibly committed.
        """

        try:
            result = self.broker.request(
                operation,
                request_id=secrets.token_urlsafe(18),
                timeout_seconds=timeout_seconds,
            )
        except BrokerError:
            return None
        status = dict(result.status)
        status["broker_ok"] = result.ok
        if not result.ok:
            status["broker_error_code"] = result.error_code
        return status

    def _broker(self, operation: BrokerOperation, timeout_seconds: float) -> bool:
        try:
            result = self.broker.request(
                operation,
                request_id=secrets.token_urlsafe(18),
                timeout_seconds=timeout_seconds,
            )
        except BrokerError:
            return False
        if operation in {
            BrokerOperation.DESKTOP_MONITORS_OFF,
            BrokerOperation.DESKTOP_MONITORS_ON,
        }:
            # The broker envelope can succeed even when one or both outlets
            # fail to converge. Only confirmed HA state is monitor success.
            return result.ok and result.status.get("accepted") is True
        return result.ok


class DesktopWorkflow:
    def __init__(
        self,
        settings: DesktopSettings,
        operations: DesktopOperations | None = None,
        *,
        broker_settings: BrokerSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        agent_status: Callable[[], Mapping[str, object]] | None = None,
        post_headless_settle_seconds: float = POST_HEADLESS_SETTLE_SECONDS,
        mutation_budget_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.broker_settings = broker_settings or BrokerSettings()
        self.operations = operations or BrokerDesktopOperations(
            settings,
            broker=BrokerClient(self.broker_settings),
        )
        self.clock = clock
        self.wall_clock = wall_clock
        self.agent_status = agent_status
        self.post_headless_settle_seconds = post_headless_settle_seconds
        # Time that must remain before a broker mutation is sent: the client's
        # whole request timeout by default. A test seam, like the settle time.
        self.mutation_budget_seconds = (
            self.broker_settings.request_timeout_seconds
            if mutation_budget_seconds is None
            else float(mutation_budget_seconds)
        )
        self._sunshine_service_cache: (
            tuple[float, float, dict[str, object] | None] | None
        ) = None
        self._mode_state_lock = threading.Lock()
        self._mode_execution_lock = threading.Lock()
        # Linearization boundary for registering a new mode generation versus
        # committing the monitor broker call. A Local generation can either win
        # before monitor-off is sent, or be registered after that bounded call;
        # it can never become current while an older request commits the call.
        self._mode_commit_lock = threading.Lock()
        self._mode_generation = 0

    def status(self, machine: str) -> DesktopState:
        """Cheap online, management, and service observations for an overview.

        parsec_status_enabled gates get_parsec_status, so a deployment that
        leaves it false has asked not to observe Parsec. This overview reached
        the same desktop.parsec_status broker operation regardless, which made
        the flag mean less than it says. Report parsec_ready as None -- not
        observed -- instead. The remote-session workflow does not come through
        here, so its readiness verification is untouched.
        """

        self._require_machine(machine)
        deadline = self.clock() + self.settings.total_timeout_seconds
        agent_connected, heartbeat_age = self._agent_observation()
        ssh = self._ssh_ready(deadline)
        icmp = False if ssh or agent_connected else self._network_reachable(deadline)
        network = ssh or icmp or agent_connected
        evidence = tuple(
            name
            for name, present in (
                ("desktop_agent", agent_connected),
                ("ssh", ssh),
                ("icmp", icmp),
            )
            if present
        )
        parsec = (
            self._parsec_ready(deadline)
            if ssh and self.settings.parsec_status_enabled
            else (False if not ssh and self.settings.parsec_status_enabled else None)
        )
        sunshine_service = (
            self._sunshine_service_ready(deadline)
            if ssh and self.settings.sunshine_status_enabled
            else None
        )
        observed_at = (
            self._sunshine_service_cache[1]
            if sunshine_service is not None and self._sunshine_service_cache is not None
            else None
        )
        return DesktopState(
            machine=machine,
            network_reachable=network,
            ssh_ready=ssh,
            parsec_ready=parsec,
            sunshine_ready=None,
            observed=True,
            online=network,
            machine_state="online" if network else "unreachable",
            online_evidence=evidence,
            agent_connected=agent_connected,
            agent_heartbeat_age_seconds=heartbeat_age,
            sunshine_service_ready=sunshine_service,
            sunshine_observed_at=observed_at,
        )

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
            "authentication_error": None,
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
        observed = {"network": False, "ssh": False, "agent": False}

        def reachable(_remaining: float) -> bool:
            agent, _age = self._agent_observation()
            if agent:
                observed["network"] = True
                observed["agent"] = True
                return True
            ssh = self._ssh_ready(deadline)
            network = ssh or self._network_reachable(deadline)
            observed["network"] = network or agent
            observed["ssh"] = ssh
            observed["agent"] = agent
            return network or agent

        ready = self._wait_for(reachable, deadline, event)
        cancelled = event.is_set()
        parsec = self._parsec_ready(deadline) if ready and observed["ssh"] else False
        return {
            "machine": machine,
            "network_reachable": bool(ready and observed["network"]),
            "ssh_ready": bool(ready and observed["ssh"]),
            "agent_connected": bool(ready and observed["agent"]),
            "parsec_ready": parsec,
            "timed_out": not ready and not cancelled,
            "cancelled": cancelled,
            "elapsed_ms": round((self.clock() - started) * 1000),
        }

    def start_remote_session(
        self, machine: str, *, cancel_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Compatibility alias for the explicit Gaming mode."""

        return self.prepare_access_mode(
            machine, DesktopAccessMode.GAMING, cancel_event=cancel_event
        )

    def prepare_access_mode(
        self,
        machine: str,
        mode: DesktopAccessMode | str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Serialize mode transitions and make newer explicit intent win."""

        self._require_machine(machine)
        selected_mode = (
            mode if isinstance(mode, DesktopAccessMode) else DesktopAccessMode(mode)
        )
        return self._serialized(
            machine, selected_mode, cancel_event, end_remote_session=False
        )

    def end_remote_session(
        self, machine: str, *, cancel_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Explicitly close the Sunshine application session, then converge Local.

        Ordinary Local mode never ends a stream. This separately requested
        operation restores and verifies physical access first, closes the
        Sunshine application through the fixed authenticated helper, verifies
        termination, and only then considers removing the virtual display. It
        shares the mode generation, so a newer explicit request supersedes it.
        """

        self._require_machine(machine)
        return self._serialized(
            machine, DesktopAccessMode.LOCAL, cancel_event, end_remote_session=True
        )

    def _serialized(
        self,
        machine: str,
        mode: DesktopAccessMode,
        cancel_event: threading.Event | None,
        *,
        end_remote_session: bool,
    ) -> dict[str, object]:
        event = cancel_event or threading.Event()
        started = self.clock()
        deadline = started + self.settings.total_timeout_seconds
        with self._mode_commit_lock:
            with self._mode_state_lock:
                self._mode_generation += 1
                generation = self._mode_generation
        while True:
            with self._mode_state_lock:
                current = generation == self._mode_generation
            remaining = deadline - self.clock()
            if not current or event.is_set() or remaining <= 0:
                # The internal implementation returns the same structured
                # cancelled/superseded/timeout result without taking a side
                # effect when any of these conditions is already true.
                return self._prepare_access_mode(
                    machine,
                    mode,
                    cancel_event=event,
                    transition_generation=generation,
                    request_started=started,
                    end_remote_session=end_remote_session,
                )
            if self._mode_execution_lock.acquire(timeout=min(0.1, remaining)):
                try:
                    return self._prepare_access_mode(
                        machine,
                        mode,
                        cancel_event=event,
                        transition_generation=generation,
                        request_started=started,
                        end_remote_session=end_remote_session,
                    )
                finally:
                    self._mode_execution_lock.release()

    def _prepare_access_mode(
        self,
        machine: str,
        mode: DesktopAccessMode,
        *,
        cancel_event: threading.Event | None,
        transition_generation: int,
        request_started: float,
        end_remote_session: bool = False,
    ) -> dict[str, object]:
        """Converge one explicit mode without changing Windows topology itself.

        Agent, SSH and ICMP are independent evidence.  Only Sunshine readiness
        is allowed to claim capture readiness, and monitor success requires the
        broker's verified per-outlet acknowledgement.  A failure after a
        successful monitor transition is never automatically reversed because
        doing so could disrupt an active user session.  The virtual display is
        changed only by the fixed Windows helper: activated for a remote mode
        before any outlet turns off, and removed in Local only after a physical
        display and a terminated Sunshine application are both verified.
        """

        selected_mode = mode
        event = cancel_event or threading.Event()
        started = request_started
        deadline = started + self.settings.total_timeout_seconds
        selected_backend = (
            None
            if selected_mode is DesktopAccessMode.LOCAL
            else self.settings.engineering_backend
            if selected_mode is DesktopAccessMode.ENGINEERING
            else "sunshine"
        )
        result: dict[str, object] = {
            "machine": machine,
            "mode": selected_mode.value,
            "operation": (
                "end_remote_session" if end_remote_session else "set_access_mode"
            ),
            "status": "pending",
            "machine_state": "unknown",
            "online": False,
            "online_evidence": [],
            "agent_connected": False,
            "agent_heartbeat_age_seconds": None,
            "wake_sent": False,
            "network_reachable": False,
            "icmp_reachable": False,
            "ssh_ready": False,
            "management_path": None,
            "sunshine_ensure_succeeded": False,
            "sunshine_reused": False,
            "sunshine_service_ready": None,
            "sunshine_capture_ready": None,
            "virtual_display_ready": None,
            "sunshine_ready": None,
            "stream_backend": selected_backend,
            "client_profile": (
                "engineering_low_bandwidth"
                if selected_mode is DesktopAccessMode.ENGINEERING
                else "gaming"
                if selected_mode is DesktopAccessMode.GAMING
                else None
            ),
            "monitor_desired_state": (
                "on" if selected_mode is DesktopAccessMode.LOCAL else "off"
            ),
            # Local needs one usable physical display: only the primary G27QC
            # outlet is turned on and the OLED outlet keeps its current state.
            "monitor_scope": (
                "primary" if selected_mode is DesktopAccessMode.LOCAL else "all"
            ),
            "monitor_transition": None,
            # Set only when this request had to wake Windows.
            "pre_wake_monitor_verification": None,
            "monitors_remained_off": None,
            "preserved_monitor_unchanged": None,
            "physical_display_verified": None,
            "display_status": None,
            "virtual_display_activation": None,
            "virtual_display_deactivation": None,
            "virtual_display_action": None,
            "virtual_display_retained_reason": None,
            "sunshine_session": None,
            "sunshine_session_end": None,
            "remote_session_terminated": None,
            "committed_side_effects": [],
            "warnings": [],
            "verification_complete": False,
            "elapsed_ms": 0,
            "failed_stage": None,
            "failure_code": None,
            "failed_dependency": None,
            "error": None,
            "safe_recovery": None,
        }
        transition = _Transition(
            self, result, event, deadline, transition_generation, started
        )
        finish = transition.finish
        superseded = transition.superseded

        stale = superseded("workflow")
        if stale is not None:
            return stale
        if event.is_set():
            return finish("cancelled", "workflow", "workflow cancelled")
        if self.clock() >= deadline:
            return finish("desktop_mode_timeout", "workflow", "workflow deadline expired")
        if not self.settings.monitors_enabled:
            return finish(
                "monitor_management_disabled",
                "monitor_outlets",
                "monitor management is not enabled",
            )
        if (
            selected_mode is not DesktopAccessMode.LOCAL
            and not self.settings.headless_enabled
        ):
            return finish(
                "headless_management_disabled",
                "monitor_outlets",
                "headless monitor management is not enabled",
            )
        if end_remote_session and not (
            self.settings.sunshine_session_status_enabled
            and self.settings.sunshine_session_end_enabled
        ):
            return finish(
                "remote_session_end_disabled",
                "sunshine_session",
                "ending a Sunshine session is not enabled",
            )

        def observe_online(observation_deadline: float) -> bool:
            agent, age = self._agent_observation()
            # A fresh authenticated heartbeat already proves Windows is online.
            # Do not pay for (or depend on) mDNS, SSH, or ICMP after stronger
            # evidence. Remote modes separately obtain their management path.
            ssh_now = False if agent else self._ssh_ready(observation_deadline)
            icmp_now = (
                False
                if ssh_now or agent
                else self._network_reachable(observation_deadline)
            )
            evidence = [
                name
                for name, present in (
                    ("desktop_agent", agent),
                    ("ssh", ssh_now),
                    ("icmp", icmp_now),
                )
                if present
            ]
            result.update(
                agent_connected=agent,
                agent_heartbeat_age_seconds=age,
                ssh_ready=ssh_now,
                icmp_reachable=icmp_now,
                network_reachable=bool(evidence),
                online=bool(evidence),
                online_evidence=evidence,
                management_path="ssh" if ssh_now else "desktop_agent" if agent else None,
            )
            return bool(evidence)

        online = observe_online(deadline)
        stale = superseded("desktop_online_evidence")
        if stale is not None:
            return stale
        if not online:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return finish("desktop_mode_timeout", "workflow", "workflow deadline expired")
            if not self.settings.wake_enabled:
                return finish(
                    "desktop_wake_disabled",
                    "wake_on_lan",
                    "the fixed wake operation is not enabled",
                )
            # Invariant: no wake packet before both outlets are verified off.
            failure = self._verify_monitors_off_before_wake(transition)
            if failure is not None:
                return failure
            pre_wake = cast(dict[str, object], result["pre_wake_monitor_verification"])
            with self._mode_commit_lock:
                stale = superseded("wake_on_lan")
                if stale is not None:
                    return stale
                if event.is_set():
                    return finish("cancelled", "wake_on_lan", "workflow cancelled")
                if (
                    self.clock() - cast(float, pre_wake["verified_at"])
                    > PRE_WAKE_VERIFICATION_MAX_AGE_SECONDS
                ):
                    return finish(
                        "monitor_off_verification_stale",
                        "wake_on_lan",
                        "the monitor-off verification is too old to rely on",
                    )
                timeout = transition.mutation_timeout()
                if timeout is None:
                    return finish(
                        "desktop_mode_timeout",
                        "wake_on_lan",
                        "not enough time remains to send the wake safely",
                    )
                woke = self.operations.send_wake(timeout)
                transition.committed("wake_on_lan")
            if not woke:
                result["safe_recovery"] = (
                    "Both monitor outlets were verified off and were left off. The "
                    "wake was refused or not confirmed; check the desktop.wake gate "
                    "and Home Assistant before one explicit retry."
                )
                return finish("desktop_wake_failed", "wake_on_lan", "fixed wake operation failed")
            result["wake_sent"] = True
            result["machine_state"] = "starting"
            wake_deadline = min(
                deadline,
                self.clock() + self.settings.wake_readiness_timeout_seconds,
            )
            online = self._wait_for(
                lambda _remaining: observe_online(wake_deadline),
                wake_deadline,
                event,
                stop=transition.is_superseded,
            )
            stale = superseded("desktop_online_evidence")
            if stale is not None:
                return stale
            if not online:
                if event.is_set():
                    return finish("cancelled", "startup", "workflow cancelled")
                if self.clock() >= deadline:
                    return finish("desktop_mode_timeout", "workflow", "workflow deadline expired")
                result["machine_state"] = "startup_timed_out"
                return finish(
                    "desktop_startup_timed_out",
                    "desktop_online_evidence",
                    "no authenticated or network evidence arrived within the wake window",
                )
        result["machine_state"] = "online"

        if selected_mode is DesktopAccessMode.LOCAL:
            return self._converge_local(transition, end_remote_session)
        return self._converge_remote(transition, selected_backend)

    def _converge_remote(
        self, transition: _Transition, selected_backend: str | None
    ) -> dict[str, object]:
        result = transition.result
        event = transition.event
        deadline = transition.deadline
        finish = transition.finish
        superseded = transition.superseded

        backend_enabled = {
            "sunshine": True,
            "rdp": self.settings.rdp_backend_enabled,
            "remote_app": self.settings.remote_app_backend_enabled,
        }.get(str(selected_backend), False)
        if selected_backend != "sunshine" or not backend_enabled:
            return finish(
                "access_backend_unavailable",
                str(selected_backend or "stream_backend"),
                "the selected Engineering backend is not explicitly provisioned",
            )
        # The currently deployed Sunshine control path is SSH.  A fresh
        # agent heartbeat proves Windows is online, but does not claim this
        # management capability or Sunshine capture readiness.
        if not result["ssh_ready"]:
            ssh_deadline = min(
                deadline, self.clock() + self.settings.ssh_timeout_seconds
            )
            result["ssh_ready"] = self._wait_for(
                lambda _remaining: self._ssh_ready(ssh_deadline),
                ssh_deadline,
                event,
                stop=transition.is_superseded,
            )
        stale = superseded("sunshine_management")
        if stale is not None:
            return stale
        if not result["ssh_ready"]:
            if event.is_set():
                return finish("cancelled", "sunshine_management", "workflow cancelled")
            return finish(
                "sunshine_management_unavailable",
                "ssh",
                "Windows is online, but no provisioned Sunshine management path is available",
            )
        result["management_path"] = "ssh"

        if not (
            self.settings.sunshine_status_enabled
            or self.settings.sunshine_ensure_enabled
        ):
            return finish(
                "sunshine_status_disabled",
                "sunshine_capture_probe",
                "Sunshine readiness observation is not enabled",
            )
        stale = superseded("sunshine_capture_probe")
        if stale is not None:
            return stale
        status = self.operations.sunshine_status(self._remaining(deadline))
        self._record_sunshine_readiness(result, status)
        ready = self._sunshine_mode_ready(status)
        if ready:
            result["sunshine_reused"] = True
            result["virtual_display_action"] = "already_active"
        elif (
            status is not None
            and status.get("virtual_display_ready") is not True
            and self.settings.virtual_display_activation_enabled
        ):
            # Activate and verify MTT before anything can claim readiness or
            # turn an outlet off. A ready MTT is never re-activated, so an
            # existing stream's capture target is left alone.
            failure = self._activate_virtual_display(transition)
            if failure is not None:
                return failure
            status = self.operations.sunshine_status(self._remaining(deadline))
            self._record_sunshine_readiness(result, status)
            ready = self._sunshine_mode_ready(status)
        if not ready:
            if not self.settings.sunshine_ensure_enabled:
                return finish(
                    "sunshine_management_disabled",
                    "sunshine_service",
                    "Sunshine ensure is not enabled",
                )
            # Ensure may start the service, so it is committed like every other
            # mutation: under the generation lock, with the full broker budget.
            with self._mode_commit_lock:
                stale = superseded("sunshine_service")
                if stale is not None:
                    return stale
                if event.is_set():
                    return finish("cancelled", "sunshine_service", "workflow cancelled")
                timeout = transition.mutation_timeout()
                if timeout is None:
                    return finish(
                        "desktop_mode_timeout",
                        "sunshine_service",
                        "not enough time remains to ensure Sunshine safely",
                    )
                ensured = self.operations.ensure_sunshine_running(timeout)
                transition.committed("sunshine_ensure")
            result["sunshine_ensure_succeeded"] = ensured
            if not ensured:
                return finish(
                    "sunshine_ensure_failed",
                    "sunshine_service",
                    "the fixed Sunshine ensure operation failed",
                )
            status = self.operations.sunshine_status(self._remaining(deadline))
            self._record_sunshine_readiness(result, status)
            ready = self._sunshine_mode_ready(status)
        if not ready:
            if status is None:
                return finish(
                    "sunshine_readiness_unobservable",
                    "sunshine_capture_probe",
                    "Sunshine readiness could not be observed",
                )
            if status.get("capture_ready") is not True:
                return finish(
                    "sunshine_capture_unavailable",
                    "sunshine_capture",
                    "Sunshine is not capture-ready",
                )
            return finish(
                "virtual_display_unavailable",
                "virtual_display",
                "the configured virtual display is not attached and capture-ready",
            )

        failure = self._commit_monitor_power(transition, "off")
        if failure is not None:
            return failure
        if result["wake_sent"]:
            # The outlets were verified off before the wake. This call found
            # them still off, without changing them, unless something powered
            # one during startup.
            final = cast(Mapping[str, object], result["monitor_transition"])
            remained = (
                final.get("already_in_state") is True
                and self._monitors_off_verdict(final) == "verified"
            )
            result["monitors_remained_off"] = remained
            if not remained:
                cast(list[str], result["warnings"]).append(
                    "monitor_powered_during_startup: an outlet was not off after "
                    "the wake and was turned off again"
                )

        if event.is_set():
            return finish("cancelled", "post_transition", "workflow cancelled")
        post_status = self.operations.sunshine_status(self._remaining(deadline))
        self._record_sunshine_readiness(result, post_status)
        if not self._sunshine_mode_ready(post_status):
            result["safe_recovery"] = (
                "Leave verified monitor state unchanged to protect any active session; "
                "re-run the same mode after inspecting Sunshine and the virtual display."
            )
            return finish(
                "sunshine_lost_after_monitor_transition",
                "sunshine_capture",
                "Sunshine readiness was lost after monitor power changed",
            )
        result["verification_complete"] = True
        return finish()

    def _converge_local(
        self, transition: _Transition, end_remote_session: bool
    ) -> dict[str, object]:
        """Restore physical access first; the virtual display is considered last.

        Local never queries, starts, stops, or restarts Sunshine, and it never
        ends a stream. Only the explicit End Remote Session request closes the
        Sunshine application, and only after physical access is verified.
        """

        result = transition.result
        finish = transition.finish

        failure = self._commit_monitor_power(transition, "primary_on")
        if failure is not None:
            return failure
        monitor = result["monitor_transition"]
        if isinstance(monitor, Mapping):
            preserved = monitor.get("preserved_entity_unchanged")
            result["preserved_monitor_unchanged"] = (
                preserved if isinstance(preserved, bool) else None
            )

        if self.settings.display_status_enabled:
            verified = self._await_physical_display(transition)
            stale = transition.superseded("physical_display")
            if stale is not None:
                return stale
            if transition.event.is_set():
                return finish("cancelled", "physical_display", "workflow cancelled")
            result["physical_display_verified"] = verified
            if not verified:
                result["safe_recovery"] = (
                    "The primary outlet is on, but Windows did not report an active "
                    "physical display. The virtual display and any Sunshine session "
                    "were left in place; inspect the G27QC input and Windows display "
                    "settings before retrying."
                )
                return finish(
                    "physical_display_unverified",
                    "physical_display",
                    "Windows did not report an active physical display",
                )
        else:
            cast(list[str], result["warnings"]).append(
                "physical_display_unobserved: only the primary outlet state was verified"
            )

        if end_remote_session:
            failure = self._end_sunshine_session(transition)
            if failure is not None:
                return failure

        failure = self._consider_virtual_display_removal(transition)
        if failure is not None:
            return failure
        result["verification_complete"] = True
        return finish()

    def _commit_monitor_power(
        self, transition: _Transition, desired_state: str
    ) -> dict[str, object] | None:
        """Commit one bounded monitor broker call under the generation lock."""

        result = transition.result
        finish = transition.finish
        with self._mode_commit_lock:
            stale = transition.superseded("monitor_outlets")
            if stale is not None:
                return stale
            if transition.event.is_set():
                return finish("cancelled", "monitor_outlets", "workflow cancelled")
            timeout = transition.mutation_timeout()
            if timeout is None:
                return finish(
                    "desktop_mode_timeout",
                    "monitor_outlets",
                    "not enough time remains to change monitor power safely",
                )
            monitor = self._set_monitor_power(desired_state, timeout)
            transition.committed("monitor_outlets")
        result["monitor_transition"] = monitor
        stale = transition.superseded("monitor_outlets")
        if stale is not None:
            result["safe_recovery"] = (
                "A newer explicit mode is converging next; do not reverse the "
                "verified outlet result from this superseded request."
            )
            return stale
        if monitor is None:
            result["safe_recovery"] = (
                "The broker outcome was not received. Observe the outlets in Home "
                "Assistant before retrying; they may already have changed."
            )
            return finish(
                "home_assistant_unavailable",
                "home_assistant",
                "monitor state could not be observed or changed",
            )
        if monitor.get("broker_ok") is False or monitor.get("accepted") is not True:
            result["safe_recovery"] = (
                "Retry the same explicit mode after Home Assistant recovers; "
                "do not automatically reverse outlets that already converged."
            )
            if desired_state == "primary_on":
                return finish(
                    "monitor_transition_failed",
                    "monitor_outlets",
                    "the primary monitor outlet did not verify on",
                )
            return finish(
                "monitor_transition_partial"
                if monitor.get("partial") is True
                else "monitor_transition_failed",
                "monitor_outlets",
                "both monitor outlets did not verify the requested state",
            )
        return None

    def _verify_monitors_off_before_wake(
        self, transition: _Transition
    ) -> dict[str, object] | None:
        """Prove, from reads made by this request, that both outlets are off.

        Returns None only once a broker result that changed nothing found both
        verified outlets "off" with no metered draw. The off command's own
        acknowledgement does not count when it changed an outlet: a separate,
        later read has to agree. Nothing observed before this request can
        satisfy it. Every failure sends no wake, leaves the outlets as they
        are, and is not retried here.
        """

        result = transition.result
        finish = transition.finish
        failure = self._commit_monitor_power(transition, "off")
        if failure is not None:
            return failure
        confirm_deadline = min(
            transition.deadline, self.clock() + PRE_WAKE_MONITOR_CONFIRM_SECONDS
        )
        reads = 1
        while True:
            evidence = cast(Mapping[str, object], result["monitor_transition"])
            verdict = self._monitors_off_verdict(evidence)
            unchanged = evidence.get("already_in_state") is True
            if verdict == "contradicted":
                result["safe_recovery"] = (
                    "No wake was sent. Inspect both monitor outlets in Home "
                    "Assistant; do not wake the desktop until both read off."
                )
                return finish(
                    "monitor_off_unverified",
                    "monitor_outlets",
                    "both monitor outlets were not verified off before the wake",
                )
            if reads > 1 and not unchanged:
                # A confirmation found an outlet on again and turned it off:
                # something else is driving it, so no read can be trusted yet.
                result["safe_recovery"] = (
                    "No wake was sent. A monitor outlet turned back on while it "
                    "was being verified; check Home Assistant automations first."
                )
                return finish(
                    "monitor_off_unstable",
                    "monitor_outlets",
                    "a monitor outlet did not stay off while it was verified",
                )
            if verdict == "verified" and unchanged:
                power = evidence.get("power_w")
                metered = isinstance(power, Mapping) and all(
                    isinstance(power.get(entity), (int, float))
                    for entity in FixedBrokerOperations.MONITOR_ENTITIES
                )
                result["pre_wake_monitor_verification"] = {
                    "verified_at": self.clock(),
                    "reads": reads,
                    "entities": dict(cast(Mapping[str, object], evidence["entities"])),
                    "power_w": dict(power) if isinstance(power, Mapping) else None,
                    "power_evidence": "metered" if metered else "unavailable",
                }
                if not metered:
                    cast(list[str], result["warnings"]).append(
                        "monitor_power_unmetered: outlet state alone verified off"
                    )
                return None
            if self.clock() >= confirm_deadline:
                result["safe_recovery"] = (
                    "No wake was sent. Both outlets read off but a meter still "
                    "reported a draw; inspect the plugs before retrying."
                    if verdict == "unsettled"
                    else "No wake was sent. The outlets changed but no later read "
                    "confirmed them off in time."
                )
                return finish(
                    "monitor_power_unsettled"
                    if verdict == "unsettled"
                    else "monitor_off_unconfirmed",
                    "monitor_outlets",
                    "both monitor outlets were not confirmed off before the wake",
                )
            if verdict == "unsettled":
                # A meter reports on its own poll; give it one interval.
                transition.event.wait(
                    min(
                        self.settings.poll_interval_seconds,
                        max(0.0, confirm_deadline - self.clock()),
                    )
                )
            failure = self._commit_monitor_power(transition, "off")
            if failure is not None:
                return failure
            reads += 1

    @staticmethod
    def _monitors_off_verdict(monitor: Mapping[str, object]) -> str:
        """Classify one monitor-off broker result as wake evidence.

        "verified": the broker decided, exactly the two verified outlets read
        "off", and no available meter disagrees. "unsettled": the outlets read
        off but a meter still reports a draw. "contradicted": anything else,
        including a missing, failed or partial acknowledgement and any outlet
        that is missing, extra, or in another state such as "unavailable".
        """

        entities = monitor.get("entities")
        if (
            monitor.get("broker_ok") is not True
            or monitor.get("accepted") is not True
            or monitor.get("partial") is not False
            or not isinstance(entities, Mapping)
            or dict(entities)
            != dict.fromkeys(FixedBrokerOperations.MONITOR_ENTITIES, "off")
        ):
            return "contradicted"
        power = monitor.get("power_w")
        if isinstance(power, Mapping) and any(
            isinstance(watts, (int, float))
            and not isinstance(watts, bool)
            and watts > MONITOR_OFF_POWER_CEILING_W
            for watts in (
                power.get(entity) for entity in FixedBrokerOperations.MONITOR_ENTITIES
            )
        ):
            return "unsettled"
        return "verified"

    def _activate_virtual_display(
        self, transition: _Transition
    ) -> dict[str, object] | None:
        result = transition.result
        finish = transition.finish
        with self._mode_commit_lock:
            stale = transition.superseded("virtual_display")
            if stale is not None:
                return stale
            if transition.event.is_set():
                return finish("cancelled", "virtual_display", "workflow cancelled")
            timeout = transition.mutation_timeout()
            if timeout is None:
                return finish(
                    "desktop_mode_timeout",
                    "virtual_display",
                    "not enough time remains to activate the virtual display safely",
                )
            activation = self.operations.activate_virtual_display(timeout)
            transition.committed("virtual_display_activate")
        result["virtual_display_activation"] = activation
        stale = transition.superseded("virtual_display")
        if stale is not None:
            return stale
        if not (
            activation is not None
            and activation.get("broker_ok") is True
            and activation.get("accepted") is True
            and activation.get("virtual_display_active") is True
            and activation.get("virtual_mode_verified") is True
        ):
            result["safe_recovery"] = (
                "No outlet was changed. Inspect the MTT virtual display with the "
                "read-only audit before retrying."
            )
            return finish(
                "virtual_display_activation_failed",
                "virtual_display",
                "the MTT virtual display was not activated and verified at 2560x1440@60",
            )
        result["virtual_display_action"] = (
            "already_active" if activation.get("already_active") is True else "activated"
        )
        return None

    def _await_physical_display(self, transition: _Transition) -> bool:
        result = transition.result
        settle_deadline = min(
            transition.deadline, self.clock() + PHYSICAL_DISPLAY_SETTLE_SECONDS
        )

        def active(remaining: float) -> bool:
            status = self.operations.display_status(remaining)
            result["display_status"] = status
            return bool(status and status.get("physical_display_active") is True)

        return self._wait_for(
            active, settle_deadline, transition.event, stop=transition.is_superseded
        )

    def _session_state(
        self, transition: _Transition, limit: float = SESSION_OBSERVATION_SECONDS
    ) -> str:
        status = self.operations.sunshine_session_status(
            min(self._remaining(transition.deadline), limit)
        )
        transition.result["sunshine_session"] = status
        state = status.get("session_state") if isinstance(status, Mapping) else None
        return str(state) if state in SUNSHINE_SESSION_STATES else "unknown"

    def _end_sunshine_session(
        self, transition: _Transition
    ) -> dict[str, object] | None:
        result = transition.result
        finish = transition.finish
        if result["physical_display_verified"] is not True:
            # An outlet that reports on is not a usable display. Closing the
            # stream without one could leave no way to use the desktop at all.
            result["safe_recovery"] = (
                "The Sunshine session was left running. Enable and validate "
                "Windows display observation before ending a remote session."
            )
            return finish(
                "physical_display_unverified",
                "physical_display",
                "a usable physical display was not verified; nothing was closed",
            )
        state = self._session_state(transition)
        if state == "unknown":
            return finish(
                "sunshine_session_unobservable",
                "sunshine_session",
                "Sunshine session state could not be established; nothing was closed",
            )
        if state != "terminated":
            with self._mode_commit_lock:
                stale = transition.superseded("sunshine_session")
                if stale is not None:
                    return stale
                if transition.event.is_set():
                    return finish("cancelled", "sunshine_session", "workflow cancelled")
                timeout = transition.mutation_timeout()
                if timeout is None:
                    return finish(
                        "desktop_mode_timeout",
                        "sunshine_session",
                        "not enough time remains to close the Sunshine session safely",
                    )
                result["sunshine_session_end"] = self.operations.end_sunshine_session(
                    timeout
                )
                transition.committed("sunshine_session_end")
            # The close response alone is never proof. Re-observe until the
            # application is gone and no stream socket remains bound.
            verify_deadline = min(
                transition.deadline, self.clock() + SESSION_END_VERIFY_SECONDS
            )
            self._wait_for(
                lambda remaining: self._session_state(transition, remaining)
                == "terminated",
                verify_deadline,
                transition.event,
                stop=transition.is_superseded,
            )
            stale = transition.superseded("sunshine_session")
            if stale is not None:
                return stale
            if transition.event.is_set():
                return finish("cancelled", "sunshine_session", "workflow cancelled")
            state = str(
                cast(Mapping[str, object], result["sunshine_session"] or {}).get(
                    "session_state", "unknown"
                )
            )
        result["remote_session_terminated"] = state == "terminated"
        if state != "terminated":
            result["virtual_display_action"] = "retained"
            result["virtual_display_retained_reason"] = "sunshine_session_present"
            result["safe_recovery"] = (
                "Physical access is restored. Sunshine did not verify that its "
                "application ended, so the virtual display was kept for it."
            )
            return finish(
                "remote_session_not_terminated",
                "sunshine_session",
                "Sunshine did not verify that its application session ended",
            )
        return None

    def _consider_virtual_display_removal(
        self, transition: _Transition
    ) -> dict[str, object] | None:
        """Remove MTT only when nothing could still capture or resume onto it.

        A Moonlight Resume runs no prep command, so the pre-stream guard cannot
        protect it; Sunshine resumes whenever an application is still running.
        "terminated" (no application and no bound stream socket) is therefore
        the only state that permits removal. Every other or unknown state keeps
        the virtual display, and the Windows helper re-checks the same state
        immediately before it acts.
        """

        result = transition.result
        finish = transition.finish
        if not self.settings.virtual_display_deactivation_enabled:
            result["virtual_display_action"] = "not_managed"
            return None

        def retain(reason: str) -> None:
            result["virtual_display_action"] = "retained"
            result["virtual_display_retained_reason"] = reason

        if result["physical_display_verified"] is not True:
            retain("physical_display_unverified")
            return None
        state = self._session_state(transition)
        if state == "unknown":
            retain("sunshine_session_unknown")
            return None
        if state != "terminated":
            retain("sunshine_session_present")
            return None
        with self._mode_commit_lock:
            stale = transition.superseded("virtual_display")
            if stale is not None:
                return stale
            if transition.event.is_set():
                return finish("cancelled", "virtual_display", "workflow cancelled")
            timeout = transition.mutation_timeout()
            if timeout is None:
                retain("insufficient_time")
                return None
            removal = self.operations.deactivate_virtual_display(timeout)
            transition.committed("virtual_display_deactivate")
        result["virtual_display_deactivation"] = removal
        if (
            removal is not None
            and removal.get("broker_ok") is True
            and removal.get("accepted") is True
            and removal.get("virtual_display_active") is False
            and removal.get("physical_display_active") is True
        ):
            result["virtual_display_action"] = (
                "already_inactive"
                if removal.get("already_inactive") is True
                else "deactivated"
            )
        else:
            # Local access is already verified; a kept virtual display is the
            # safe outcome, so this is reported rather than failing Local.
            result["virtual_display_action"] = "deactivation_failed"
            cast(list[str], result["warnings"]).append(
                "virtual_display_deactivation_failed: the virtual display may still be active"
            )
        return None

    def _set_monitor_power(
        self, desired_state: str, timeout_seconds: float
    ) -> dict[str, object] | None:
        method = getattr(self.operations, "set_monitor_power", None)
        if callable(method):
            return method(desired_state, timeout_seconds)
        if desired_state == "primary_on":
            # The legacy boolean operations only control both outlets together,
            # which would power the intentionally-off OLED.
            return None
        accepted = (
            self.operations.restore_headed_mode(timeout_seconds)
            if desired_state == "on"
            else self.operations.request_headless_mode(timeout_seconds)
        )
        return {
            "accepted": accepted,
            "desired_state": desired_state,
            "partial": False,
            "entities": {},
        }

    @staticmethod
    def _sunshine_mode_ready(status: Mapping[str, object] | None) -> bool:
        return bool(
            status
            and status.get("plausibly_ready") is True
            and status.get("capture_ready") is True
            and status.get("virtual_display_ready") is True
        )

    @staticmethod
    def _record_sunshine_readiness(
        result: dict[str, object], status: Mapping[str, object] | None
    ) -> None:
        result["sunshine_service_ready"] = (
            None
            if status is None
            else bool(
                status.get("service_running")
                and status.get("host_process_present")
                and status.get("web_listener_present")
                and status.get("stream_listeners_present")
            )
        )
        result["sunshine_capture_ready"] = (
            None if status is None else status.get("capture_ready")
        )
        result["virtual_display_ready"] = (
            None if status is None else status.get("virtual_display_ready")
        )
        result["sunshine_ready"] = (
            None if status is None else status.get("plausibly_ready")
        )

    def _wait_for(
        self,
        predicate: Callable[[float], bool],
        deadline: float,
        event: threading.Event,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> bool:
        # `stop` lets a superseded request leave a long wake/SSH wait at once
        # instead of holding the execution lock until its own window expires.
        while not self._cancelled(event, deadline):
            if stop is not None and stop():
                return False
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

    def _sunshine_ready(self, deadline: float) -> bool | None:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            return None
        return self.operations.sunshine_ready(remaining)

    def _sunshine_service_ready(self, deadline: float) -> bool | None:
        now = self.clock()
        if self._sunshine_service_cache is not None:
            observed_monotonic, _observed_wall, status = self._sunshine_service_cache
            if now - observed_monotonic <= self.settings.overview_cache_seconds:
                return None if status is None else bool(status.get("service_ready"))
        remaining = self._remaining(deadline)
        method = getattr(self.operations, "sunshine_service_status", None)
        if remaining <= 0 or not callable(method):
            return None
        status = method(remaining)
        self._sunshine_service_cache = (now, self.wall_clock(), status)
        return None if status is None else bool(status.get("service_ready"))

    def _agent_observation(self) -> tuple[bool, float | None]:
        if self.agent_status is None:
            return False, None
        try:
            status = self.agent_status()
        except Exception:  # a read provider cannot break the workflow
            return False, None
        connected = status.get("state") == "connected" and status.get(
            "agent_connected"
        ) is True
        age = status.get("last_authenticated_activity_age_seconds")
        return connected, float(age) if isinstance(age, (int, float)) else None

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self.clock())

    def _cancelled(self, event: threading.Event, deadline: float) -> bool:
        return event.is_set() or self.clock() >= deadline

    def _require_machine(self, machine: str) -> None:
        if not self.settings.enabled:
            raise IntegrationError("unavailable", "desktop integration is disabled")
        if machine != self.settings.machine or machine != "desktop":
            raise IntegrationError("policy_denied", "machine is not allow-listed")


class _Transition:
    """One accepted request: its result record, deadline, and generation."""

    def __init__(
        self,
        workflow: DesktopWorkflow,
        result: dict[str, object],
        event: threading.Event,
        deadline: float,
        generation: int,
        started: float,
    ) -> None:
        self.workflow = workflow
        self.result = result
        self.event = event
        self.deadline = deadline
        self.generation = generation
        self.started = started

    def is_superseded(self) -> bool:
        with self.workflow._mode_state_lock:
            return self.generation != self.workflow._mode_generation

    def superseded(self, stage: str) -> dict[str, object] | None:
        if not self.is_superseded():
            return None
        return self.finish(
            "mode_transition_superseded",
            stage,
            "a newer explicit desktop mode transition took precedence",
        )

    def committed(self, side_effect: str) -> None:
        """Record a mutation that was sent, whether or not its result arrived."""

        committed = cast(list[str], self.result["committed_side_effects"])
        if side_effect not in committed:
            committed.append(side_effect)

    def mutation_timeout(self) -> float | None:
        """The full broker request budget, or None if it no longer fits.

        A mutation is committed only while the whole client request timeout
        remains. Otherwise the client could give up while the root broker is
        still changing state, and a change that happened would be reported as
        a failure that invites a retry.
        """

        budget = self.workflow.mutation_budget_seconds
        remaining = self.workflow._remaining(self.deadline)
        if remaining <= 0 or remaining < budget:
            return None
        return min(remaining, self.workflow.broker_settings.request_timeout_seconds)

    def finish(
        self,
        code: str | None = None,
        dependency: str | None = None,
        error: str | None = None,
    ) -> dict[str, object]:
        result = self.result
        result["failed_stage"] = dependency
        result["failure_code"] = code
        result["failed_dependency"] = dependency
        result["error"] = error
        result["elapsed_ms"] = round((self.workflow.clock() - self.started) * 1000)
        committed = bool(
            result.get("monitor_transition") or result.get("committed_side_effects")
        )
        if code is None:
            result["status"] = "ready"
        elif committed:
            # Includes cancellation after a mutation was sent: the request did
            # not finish, but state may already have changed.
            result["status"] = "partial"
        elif code == "cancelled":
            result["status"] = "cancelled"
        else:
            result["status"] = "failed"
        return result
