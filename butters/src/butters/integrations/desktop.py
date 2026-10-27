"""Fixed, bounded Windows desktop observation and headless-session workflow."""

from __future__ import annotations

import secrets
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from butters.actions.broker import BrokerClient, BrokerError, BrokerOperation
from butters.assistant_config import BrokerSettings, DesktopSettings
from butters.integrations.model import IntegrationError

NETWORK_ATTEMPT_TIMEOUT_SECONDS = 2.0
SSH_ATTEMPT_TIMEOUT_SECONDS = 1.0
# Failing name resolution is not covered by either probe's own timeout. The
# configured desktop is named by mDNS, and resolving a `.local` name for a host
# that is powered off blocks in getaddrinfo before create_connection's timeout
# can begin to apply; the ping helper has to resolve the same name and is capped
# only by its subprocess timeout. Measured against the real host while it was
# off: getaddrinfo 5.02s then gaierror, create_connection(timeout=1.0) 5.02s
# then gaierror, and ping -c1 -W1 exceeding its 2.0s cap -- about 7.0s for one
# status observation.
NAME_RESOLUTION_STALL_SECONDS = 5.0
# What DesktopWorkflow.status() can cost when the desktop is actually off: one
# stalled SSH resolution plus the separately-capped ping. Parsec is only probed
# when SSH succeeded, so it adds nothing to this path.
STATUS_WORST_CASE_SECONDS = (
    NAME_RESOLUTION_STALL_SECONDS + NETWORK_ATTEMPT_TIMEOUT_SECONDS
)
# Registered ceiling for the read skill, with headroom over that worst case. It
# has to exceed it: a skill deadline shorter than one observation makes every
# field `unknown` and hides the powered-off state the probes actually measured.
STATUS_TIMEOUT_SECONDS = 10.0
POST_HEADLESS_SETTLE_SECONDS = 5.0


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
        operation = (
            BrokerOperation.DESKTOP_MONITORS_OFF
            if desired_state == "off"
            else BrokerOperation.DESKTOP_MONITORS_ON
        )
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
        self._sunshine_service_cache: (
            tuple[float, float, dict[str, object] | None] | None
        ) = None

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
        """Converge one explicit mode without changing sessions or displays.

        Agent, SSH and ICMP are independent evidence.  Only Sunshine readiness
        is allowed to claim capture readiness, and monitor success requires the
        broker's verified per-outlet acknowledgement.  A failure after a
        successful monitor transition is never automatically reversed because
        doing so could disrupt an active user session.
        """

        self._require_machine(machine)
        selected_mode = (
            mode if isinstance(mode, DesktopAccessMode) else DesktopAccessMode(mode)
        )
        event = cancel_event or threading.Event()
        started = self.clock()
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
            "monitor_transition": None,
            "verification_complete": False,
            "elapsed_ms": 0,
            "failed_stage": None,
            "failure_code": None,
            "failed_dependency": None,
            "error": None,
            "safe_recovery": None,
        }

        def finish(
            code: str | None = None,
            dependency: str | None = None,
            error: str | None = None,
        ) -> dict[str, object]:
            result["failed_stage"] = dependency
            result["failure_code"] = code
            result["failed_dependency"] = dependency
            result["error"] = error
            result["elapsed_ms"] = round((self.clock() - started) * 1000)
            if code is None:
                result["status"] = "ready"
            elif code == "cancelled":
                result["status"] = "cancelled"
            elif result.get("monitor_transition"):
                result["status"] = "partial"
            else:
                result["status"] = "failed"
            return result

        if event.is_set():
            return finish("cancelled", "workflow", "workflow cancelled")
        if self.clock() >= deadline:
            return finish("desktop_mode_timeout", "workflow", "workflow deadline expired")

        def observe_online(observation_deadline: float) -> bool:
            agent, age = self._agent_observation()
            ssh_now = self._ssh_ready(observation_deadline)
            # A fresh authenticated heartbeat already proves Windows is online.
            # Do not pay for (or depend on) mDNS + ICMP after stronger evidence.
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
        if not online:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return finish("desktop_mode_timeout", "workflow", "workflow deadline expired")
            if not self.operations.send_wake(remaining):
                return finish("desktop_wake_failed", "wake_on_lan", "fixed wake operation failed")
            result["wake_sent"] = True
            result["machine_state"] = "starting"
            wake_deadline = min(
                deadline,
                self.clock() + self.settings.wake_readiness_timeout_seconds,
            )
            online = self._wait_for(
                lambda _remaining: observe_online(wake_deadline), wake_deadline, event
            )
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

        if selected_mode is not DesktopAccessMode.LOCAL:
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
                )
            if not result["ssh_ready"]:
                if event.is_set():
                    return finish("cancelled", "sunshine_management", "workflow cancelled")
                return finish(
                    "sunshine_management_unavailable",
                    "ssh",
                    "Windows is online, but no provisioned Sunshine management path is available",
                )
            result["management_path"] = "ssh"

            status = self.operations.sunshine_status(self._remaining(deadline))
            self._record_sunshine_readiness(result, status)
            ready = self._sunshine_mode_ready(status)
            if ready:
                result["sunshine_reused"] = True
            else:
                if not self.settings.sunshine_ensure_enabled:
                    return finish(
                        "sunshine_management_disabled",
                        "sunshine_service",
                        "Sunshine ensure is not enabled",
                    )
                ensured = self.operations.ensure_sunshine_running(
                    self._remaining(deadline)
                )
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

        remaining = self._remaining(deadline)
        if remaining <= 0:
            return finish("desktop_mode_timeout", "workflow", "workflow deadline expired")
        transition = self._set_monitor_power(
            str(result["monitor_desired_state"]), remaining
        )
        result["monitor_transition"] = transition
        if transition is None:
            return finish(
                "home_assistant_unavailable",
                "home_assistant",
                "monitor state could not be observed or changed",
            )
        if transition.get("accepted") is not True:
            result["safe_recovery"] = (
                "Retry the same explicit mode after Home Assistant recovers; "
                "do not automatically reverse outlets that already converged."
            )
            return finish(
                "monitor_transition_partial"
                if transition.get("partial") is True
                else "monitor_transition_failed",
                "monitor_outlets",
                "both monitor outlets did not verify the requested state",
            )

        if selected_mode is not DesktopAccessMode.LOCAL:
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

    def _set_monitor_power(
        self, desired_state: str, timeout_seconds: float
    ) -> dict[str, object] | None:
        method = getattr(self.operations, "set_monitor_power", None)
        if callable(method):
            return method(desired_state, timeout_seconds)
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
