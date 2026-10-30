"""Fixed host, NAS, and environmental capability adapters."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from butters.actions.broker import BrokerClient, BrokerError, BrokerOperation
from butters.actions.store import ActionStateStore
from butters.assistant_config import ActionSettings, BrokerSettings, KnownDeviceSettings
from butters.integrations import kr260_jtag
from butters.integrations.kr260 import Kr260Observer
from butters.integrations.model import IntegrationError, SensorSnapshotProvider
from butters.integrations.nas_status import NasStatusObserver


class HostStatusAdapter:
    def __init__(
        self,
        broker_settings: BrokerSettings,
        *,
        runner: Any = subprocess.run,
        root: Path = Path("/"),
    ) -> None:
        self.broker_settings = broker_settings
        self.runner = runner
        self.root = root

    def host(self) -> dict[str, object]:
        uptime = _first_float(Path("/proc/uptime"))
        memory = _memory()
        disk = shutil.disk_usage(self.root)
        temperature = _temperature()
        load = os.getloadavg()
        return {
            "uptime_seconds": uptime,
            "load_1m": round(load[0], 3),
            "load_5m": round(load[1], 3),
            "load_15m": round(load[2], 3),
            "memory_total_bytes": memory.get("MemTotal"),
            "memory_available_bytes": memory.get("MemAvailable"),
            "root_total_bytes": disk.total,
            "root_free_bytes": disk.free,
            "cpu_temperature_c": temperature,
        }

    def service(self) -> dict[str, object]:
        return self._systemctl_show("butters-web.service")

    def dependencies(self) -> dict[str, object]:
        return {
            name: self._systemctl_show(unit)
            for name, unit in (
                ("dashboard", "home-sensor-dashboard.service"),
                ("influxdb", "influxdb.service"),
                ("mqtt", "mosquitto.service"),
                ("tailscale", "tailscaled.service"),
            )
        }

    def broker(self) -> dict[str, object]:
        path = self.broker_settings.socket_path
        return {
            "configured": self.broker_settings.enabled,
            "socket_present": path.exists() if self.broker_settings.enabled else False,
            "protocol_version": self.broker_settings.protocol_version,
            "ready": self.broker_settings.enabled and path.exists(),
        }

    def _systemctl_show(self, unit: str) -> dict[str, object]:
        try:
            result = self.runner(
                [
                    "/usr/bin/systemctl",
                    "show",
                    unit,
                    "--property=ActiveState,NRestarts",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"state": "unknown", "nrestarts": None}
        lines = str(getattr(result, "stdout", "")).splitlines()
        return {
            "state": lines[0] if lines and lines[0] else "unknown",
            "nrestarts": int(lines[1])
            if len(lines) > 1 and lines[1].isdigit()
            else None,
        }


class FixedActionAdapter:
    def __init__(self, broker: BrokerClient) -> None:
        self.broker = broker

    def execute(
        self,
        operation: BrokerOperation,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, object]:
        try:
            result = self.broker.request(
                operation,
                request_id=secrets.token_urlsafe(18),
                cancel_event=cancel_event,
            )
        except BrokerError as exc:
            raise IntegrationError(exc.code, str(exc)) from exc
        if not result.ok:
            raise IntegrationError(
                result.error_code or "operation_failed", "fixed broker operation failed"
            )
        return {"operation": operation.value, "accepted": True, **result.status}


class NasAdapter:
    """One fixed NAS: observe it, wake it, or power it off.

    The adapter owns no addressing of its own. Wake and shutdown name a broker
    operation and nothing else; observation is delegated to the configured
    status observer. There is no call here that accepts a host, address, port,
    URL, credential, command, or argv from any caller.
    """

    def __init__(
        self,
        settings: KnownDeviceSettings,
        actions: FixedActionAdapter,
        *,
        observer: NasStatusObserver | None = None,
        shutdown_settings: KnownDeviceSettings | None = None,
    ) -> None:
        self.settings = settings
        self.actions = actions
        self.observer = observer
        # Shutdown is gated separately from wake so enabling wake can never
        # imply the ability to power the NAS off.
        self.shutdown_settings = shutdown_settings or KnownDeviceSettings()

    def status(self) -> dict[str, object]:
        base: dict[str, object] = {
            "device": "nas",
            "configured": self.settings.configured,
            "enabled": self.settings.enabled,
            "shutdown_configured": self.shutdown_settings.configured,
            "shutdown_enabled": self.shutdown_settings.enabled,
            "network_reachable": None,
            "status": "unconfigured" if not self.settings.configured else "unknown",
        }
        if self.observer is None or not self.observer.configured:
            base["observed"] = False
            return base
        observation = self.observer.observe()
        base["observed"] = True
        base["status"] = observation.aggregate.value
        base["network_reachable"] = observation.lan.value == "reachable"
        base.update(observation.safe_dict())
        return base

    def observe(
        self, *, wake_requested_at: float | None = None, refresh: bool = False
    ) -> dict[str, object]:
        if self.observer is None or not self.observer.configured:
            raise IntegrationError(
                "capability_unavailable", "NAS observation is not configured"
            )
        return self.observer.observe(
            wake_requested_at=wake_requested_at, refresh=refresh
        ).safe_dict()

    def wake(self, cancel_event: threading.Event | None = None) -> dict[str, object]:
        self._require()
        result = self.actions.execute(
            BrokerOperation.NAS_WAKE, cancel_event=cancel_event
        )
        # Truthful wording: a magic packet left the host. Whether the NAS boots
        # is decided by the status observations, never by this result.
        return {**result, "outcome": "wake_packet_sent"}

    def shutdown(
        self, cancel_event: threading.Event | None = None
    ) -> dict[str, object]:
        if not self.shutdown_settings.enabled or not self.shutdown_settings.configured:
            raise IntegrationError(
                "capability_unavailable", "NAS shutdown is not configured"
            )
        result = self.actions.execute(
            BrokerOperation.NAS_SHUTDOWN, cancel_event=cancel_event
        )
        return {**result, "outcome": "shutdown_requested"}

    def _require(self) -> None:
        if not self.settings.enabled or not self.settings.configured:
            raise IntegrationError(
                "capability_unavailable", "NAS wake is not configured"
            )


class Kr260Adapter:
    """One identity-verified KR260: observation, kexec restart, JTAG recovery."""

    # A reboot that may have been sent blocks another until the board reports
    # a different boot ID or this long has passed: a K26 normally boots in ~2
    # minutes, and a second request against the dying sshd would be ambiguous.
    REBOOT_COOLDOWN_SECONDS = 300.0
    # Failures decided before the restart could be handed off on the board,
    # including every refusal of the kexec wrapper.
    _NOT_SENT = frozenset(
        {
            "identity_mismatch",
            "host_key_mismatch",
            "ssh_auth_failed",
            "host_unreachable",
            "reboot_unauthorized",
            "restart_already_staged",
            "restart_precondition_failed",
            "watchdog_active",
            "pl_workload_active",
            "restart_failed",
            "operation_unavailable",
            "broker_unconfigured",
            "broker_unavailable",
            "cancelled",
        }
    )
    # Failure codes of a reboot_kr260 job that sent nothing: the broker's
    # refusals above plus this adapter's own checks before sending.
    RESTART_NOT_SENT = _NOT_SENT | frozenset(
        {"capability_unavailable", "identity_unverified", "reboot_in_progress"}
    )
    _JTAG_CHECK = "kr260.jtag_por.check"
    _JTAG_RESET = "kr260.jtag_por.reset"

    def __init__(
        self,
        observer: Kr260Observer,
        actions: FixedActionAdapter,
        reboot_settings: KnownDeviceSettings,
        *,
        jtag_settings: KnownDeviceSettings | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.observer = observer
        self.actions = actions
        self.reboot_settings = reboot_settings
        self.jtag_settings = jtag_settings or KnownDeviceSettings()
        # The Desktop Agent hub, attached once the web runtime has built it.
        self.jtag: Any | None = None
        self.monotonic = monotonic
        self.clock = clock
        self._lock = threading.Lock()
        self._observe_lock = threading.Lock()
        # (monotonic time, boot ID before) of the last reboot that may have run.
        self._last_reboot: tuple[float, str | None] | None = None
        # The last JTAG attempt, and the last one that may have reset the board.
        self._jtag_attempt: kr260_jtag.Attempt | None = None
        self._last_jtag: kr260_jtag.Attempt | None = None
        # Wall-clock times of the last reset and the last kexec restart job
        # that may have run, from the persistent job store, so the interval and
        # the quiet period after a restart both survive a Butters restart.
        self._persisted_reset_at: Callable[[], float | None] = lambda: None
        self._persisted_kexec_at: Callable[[], float | None] = lambda: None
        self._dark = kr260_jtag.DarkRun()
        self._recorded_at: object = None
        self._last_boot_id: str | None = None

    def attach_jtag(
        self,
        hub: Any,
        *,
        persisted_reset_at: Callable[[], float | None] | None = None,
        persisted_kexec_at: Callable[[], float | None] | None = None,
    ) -> None:
        self.jtag = hub
        if persisted_reset_at is not None:
            self._persisted_reset_at = persisted_reset_at
        if persisted_kexec_at is not None:
            self._persisted_kexec_at = persisted_kexec_at

    def _jtag_available(self) -> bool:
        return bool(
            self.jtag_settings.enabled
            and self.jtag_settings.configured
            and self.jtag is not None
            and self.jtag.configured
        )

    def _observe(self, *, refresh: bool) -> dict[str, object]:
        value = self.observer.observe(refresh=refresh)
        with self._observe_lock:
            # The observer caches; count each probe once, when first seen. Two
            # probes may share a timestamp, so the outcome is part of the key.
            stamp = value.get("observed_at")
            key = (stamp, value.get("operational_state"), value.get("error"))
            if stamp is not None and key != self._recorded_at:
                self._recorded_at = key
                self._dark.record(value, self.monotonic())
            boot_id = value.get("boot_id")
            if value.get("identity_verified") and isinstance(boot_id, str) and boot_id:
                self._last_boot_id = boot_id
        return value

    def status(self, *, refresh: bool = False) -> dict[str, object]:
        value = self._observe(refresh=refresh)
        value["capability"] = {
            "monitoring": self.observer.settings.enabled,
            "reboot": self.reboot_settings.enabled and self.reboot_settings.configured,
            "shutdown": False,
            "jtag_reset": self._jtag_available(),
        }
        value["jtag_recovery"] = self._jtag_summary(value)
        last = self._last_reboot
        if last is not None:
            booted = value.get("boot_id")
            age = self.monotonic() - last[0]
            # None until the board is observed again; never inferred.
            changed = (
                None
                if not value.get("identity_verified") or not booted
                else booted != last[1]
            )
            value["last_reboot_request"] = {
                "age_seconds": round(age, 1),
                "previous_boot_id": last[1],
                "boot_changed": changed,
                # A request is only confirmed by a new boot ID on the same board.
                "state": "confirmed"
                if changed
                else "requested"
                if age < self.REBOOT_COOLDOWN_SECONDS
                else "unconfirmed",
            }
        return value

    def reboot(self, cancel_event: threading.Event | None = None) -> dict[str, object]:
        if not self.reboot_settings.enabled or not self.reboot_settings.configured:
            raise IntegrationError(
                "capability_unavailable", "KR260 reboot is not configured"
            )
        # Serialized, so two concurrent requests cannot both pass the checks.
        with self._lock:
            before = self._observe(refresh=True)
            if not before.get("identity_verified"):
                raise IntegrationError(
                    "identity_unverified", "KR260 identity could not be verified"
                )
            last = self._last_reboot
            if (
                last is not None
                and self.monotonic() - last[0] < self.REBOOT_COOLDOWN_SECONDS
                and before.get("boot_id") == last[1]
            ):
                raise IntegrationError(
                    "reboot_in_progress",
                    "a KR260 reboot was already requested and has not completed",
                )
            try:
                result = self.actions.execute(
                    BrokerOperation.KR260_REBOOT, cancel_event=cancel_event
                )
            except IntegrationError as exc:
                if exc.code not in self._NOT_SENT:
                    # The outcome is unknown, so treat it as possibly sent.
                    self._last_reboot = (self.monotonic(), _boot_id(before))
                    self.observer.invalidate()
                raise
            self._last_reboot = (self.monotonic(), _boot_id(before))
            self.observer.invalidate()
        return {
            **result,
            # Accepted for execution only; status() reports it confirmed once
            # the board answers with a different boot ID.
            "outcome": "restart_requested",
            "restart_confirmed": False,
            "previous_boot_id": before.get("boot_id"),
        }

    # ----- JTAG power-on reset through the Desktop Agent -------------------

    def jtag_check(
        self, cancel_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Attach-only JTAG check through the Desktop Agent, dark board only."""

        result = self._jtag_run("check", cancel_event, None)
        return {**result, "outcome": "check_passed"}

    def jtag_reset(
        self,
        cancel_event: threading.Event | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """One JTAG power-on reset of a board Butters has seen dark for minutes.

        Success means only that the agent's wrapper printed RST_ISSUED. Whether
        the board restarted is decided later by status() from boot evidence.
        """

        result = self._jtag_run("reset", cancel_event, idempotency_key)
        attempt = self._jtag_attempt
        return {
            **result,
            "outcome": "reset_requested",
            "reset_confirmed": False,
            "previous_boot_id": attempt.previous_boot_id if attempt else None,
        }

    def _jtag_run(
        self,
        operation: str,
        cancel_event: threading.Event | None,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        if not self._jtag_available():
            raise IntegrationError(
                "capability_unavailable", "KR260 JTAG recovery is not configured"
            )
        # A second request while one runs is refused, not queued behind it.
        if not self._lock.acquire(blocking=False):
            raise IntegrationError("busy", "a KR260 operation is already running")
        try:
            # A fresh probe: the board may have come back since authorization.
            self._observe(refresh=True)
            code = self._jtag_refusal(reset=operation == "reset")
            if code is not None:
                raise IntegrationError(code, "KR260 JTAG recovery precondition failed")
            previous = self._last_boot_id
            action = self._JTAG_RESET if operation == "reset" else self._JTAG_CHECK
            # Before sending: a reset happens during the request, and uptime is
            # compared with the time since this moment.
            sent_at = self.monotonic()
            try:
                result = self.jtag.kr260_jtag(
                    action, cancel=cancel_event, idempotency_key=idempotency_key
                )
            except Exception:  # noqa: BLE001 - an unknown outcome may have reset.
                result = {
                    "action": action,
                    "success": False,
                    "error": "transport_error",
                }
            may_have_reset = operation == "reset" and self._may_have_reset(result)
            error = None if result.get("success") else self._jtag_error_code(result)
            code_seen = result.get("jtag_result")
            issued_at = result.get("reset_issued_at")
            attempt = kr260_jtag.Attempt(
                operation=operation,
                at=sent_at,
                previous_boot_id=previous,
                may_have_reset=may_have_reset,
                error=error,
                jtag_result=code_seen if isinstance(code_seen, str) else None,
                issued_at=issued_at if isinstance(issued_at, str) else None,
            )
            self._jtag_attempt = attempt
            if may_have_reset:
                self._last_jtag = attempt
                self.observer.invalidate()
        finally:
            self._lock.release()
        if error is not None:
            raise IntegrationError(error, f"KR260 JTAG {operation} failed")
        return result

    def _agent_state(self) -> str:
        hub = self.jtag
        if hub is None or not hub.configured:
            return "unconfigured"
        state = hub.status().get("state")
        if state == "not_configured":
            return "unconfigured"
        if state != "connected":
            # disconnected, awaiting_heartbeat, heartbeat_aging, heartbeat_stale
            return str(state or "disconnected")
        # A connected agent advertises the actions only with its local gate on.
        return "connected" if hub.supports(self._JTAG_RESET) else "jtag_not_offered"

    def _jtag_refusal(self, *, reset: bool) -> str | None:
        now = self.monotonic()
        return kr260_jtag.refusal(
            now=now,
            dark=self._dark,
            last_kexec_at=self._latest(
                now,
                self._last_reboot[0] if self._last_reboot else None,
                self._persisted_kexec_at(),
            ),
            last_reset_at=self._latest(
                now,
                self._last_jtag.at if self._last_jtag else None,
                self._persisted_reset_at(),
            ),
            reset=reset,
        )

    def _latest(
        self, now: float, in_memory: float | None, persisted: float | None
    ) -> float | None:
        """The later of an in-memory monotonic time and a stored wall time."""

        if persisted is None:
            return in_memory
        # Express the stored wall-clock time on this process's monotonic clock.
        mirrored = now - max(0.0, self.clock() - persisted)
        return mirrored if in_memory is None else max(in_memory, mirrored)

    def _jtag_summary(self, value: dict[str, object]) -> dict[str, object]:
        """What the Admin page shows; it derives nothing itself."""

        now = self.monotonic()
        gate = bool(self.jtag_settings.enabled and self.jtag_settings.configured)
        agent = self._agent_state()

        def blocked(reset: bool) -> str | None:
            if not gate:
                return "gate_closed"
            if agent != "connected":
                return "agent_" + agent
            return self._jtag_refusal(reset=reset)

        summary: dict[str, object] = {
            "configured": gate,
            "agent": agent,
            "board": kr260_jtag.board_state(value, self._dark, now),
            "dark_seconds": round(self._dark.seconds(now), 1),
            "dark_probes": self._dark.count,
            "required_dark_seconds": kr260_jtag.DARK_SECONDS,
            "last_known_boot_id": self._last_boot_id,
            "reset_blocked_reason": blocked(True),
            "check_blocked_reason": blocked(False),
            "last_attempt": None,
        }
        summary["reset_permitted"] = summary["reset_blocked_reason"] is None
        summary["check_permitted"] = summary["check_blocked_reason"] is None
        attempt = self._jtag_attempt
        if attempt is not None:
            summary["last_attempt"] = self._attempt_view(attempt, value, now)
        return summary

    def _attempt_view(
        self, attempt: kr260_jtag.Attempt, value: dict[str, object], now: float
    ) -> dict[str, object]:
        age = now - attempt.at
        if attempt.operation == "check":
            state, evidence = (
                ("check_failed", "none") if attempt.error else ("check_passed", "none")
            )
        elif not attempt.may_have_reset:
            state, evidence = "failed", "not_sent"
        elif attempt.final is not None:
            state, evidence = attempt.final
        else:
            state, evidence = kr260_jtag.reset_state(
                value, age_seconds=age, previous_boot_id=attempt.previous_boot_id
            )
            # Evidence of a boot (or of none) is kept, even if the board later
            # goes dark again; "unconfirmed" can still become "confirmed".
            if state in {"confirmed", "booted", "no_effect"}:
                attempt.final = (state, evidence)
        return {
            "operation": attempt.operation,
            "state": state,
            "evidence": evidence,
            "age_seconds": round(age, 1),
            "error": attempt.error,
            "jtag_result": attempt.jtag_result,
            "reset_issued_at": attempt.issued_at,
            "previous_boot_id": attempt.previous_boot_id,
            "boot_id": value.get("boot_id") if value.get("identity_verified") else None,
        }

    @staticmethod
    def _may_have_reset(result: dict[str, object]) -> bool:
        if result.get("success") is True:
            return True
        may_have_run = result.get("reset_may_have_run")
        if type(may_have_run) is bool:
            return may_have_run
        return Kr260Adapter._jtag_error_code(result) not in kr260_jtag.RESET_NOT_SENT

    @staticmethod
    def _jtag_error_code(result: dict[str, object]) -> str:
        code = result.get("jtag_result")
        if isinstance(code, str) and not result.get("success"):
            return f"jtag_{code.lower()}"
        error = result.get("error")
        return error if isinstance(error, str) else "agent_action_failed"


def _boot_id(observation: dict[str, object]) -> str | None:
    value = observation.get("boot_id")
    return value if isinstance(value, str) else None


class EnvironmentControlAdapter:
    OPERATIONS: ClassVar[dict[tuple[str, str], BrokerOperation]] = {
        ("heater", "on"): BrokerOperation.HEATER_ON,
        ("heater", "off"): BrokerOperation.HEATER_OFF,
        ("dehumidifier", "on"): BrokerOperation.DEHUMIDIFIER_ON,
        ("dehumidifier", "off"): BrokerOperation.DEHUMIDIFIER_OFF,
        ("ventilation", "on"): BrokerOperation.VENTILATION_ON,
        ("ventilation", "off"): BrokerOperation.VENTILATION_OFF,
    }

    def __init__(
        self,
        settings: ActionSettings,
        actions: FixedActionAdapter,
        state: ActionStateStore,
        sensors: SensorSnapshotProvider,
        *,
        clock: Any = time.time,
    ) -> None:
        self.settings = settings
        self.actions = actions
        self.state = state
        self.sensors = sensors
        self.clock = clock

    def status(self) -> dict[str, object]:
        overrides = {item["device"]: item for item in self.state.overrides()}
        return {
            device: {
                "configured": config.configured,
                "enabled": config.enabled,
                "state": "unknown",
                "active_timed_override": overrides.get(device),
                "safety_interlock": self._interlock_status(config),
                "verification_state": "unobservable",
            }
            for device, config in self._devices()
        }

    def set(
        self,
        device: str,
        state: str,
        duration_minutes: int | None,
        *,
        cancel_event: threading.Event | None,
        job_id: str = "pending",
    ) -> dict[str, object]:
        config = dict(self._devices())[device]
        self._require_available(config)
        active_override = next(
            (item for item in self.state.overrides() if item["device"] == device),
            None,
        )
        if active_override is not None and state != "off":
            raise IntegrationError(
                "override_recovery_required",
                "a persisted override must be released before further control",
            )
        if state not in {"on", "off"}:
            raise IntegrationError("invalid_arguments", "state must be on or off")
        if duration_minutes is not None and (
            state != "on"
            or not 1 <= duration_minutes <= config.maximum_duration_minutes
        ):
            raise IntegrationError(
                "invalid_arguments", "duration exceeds the configured limit"
            )
        self._require_interlock(config)
        timed = state == "on" and duration_minutes is not None
        expires = self.clock() + (duration_minutes or 0) * 60
        if timed:
            # Persist the release obligation before the device can be energised.
            # Recording it afterwards leaves a window where a crash between the
            # accepted ON command and the write strands the device on with no
            # record for recover_overrides() to release.
            self.state.set_override(device, state, expires, job_id)
        result = self.actions.execute(
            self.OPERATIONS[(device, state)], cancel_event=cancel_event
        )
        if not timed:
            self.state.clear_override(device)
            return {
                "device": device,
                "state": state,
                "duration_minutes": None,
                **result,
            }
        event = cancel_event or threading.Event()
        cancelled = event.wait(max(0, expires - self.clock()))
        release = self.actions.execute(self.OPERATIONS[(device, "off")])
        self.state.clear_override(device)
        if cancelled:
            raise IntegrationError(
                "cancelled", "timed override was cancelled and released"
            )
        return {
            "device": device,
            "state": "off",
            "duration_minutes": duration_minutes,
            "expired": True,
            "release": release,
        }

    def recover_overrides(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for item in self.state.overrides():
            device = str(item["device"])
            try:
                self.actions.execute(self.OPERATIONS[(device, "off")])
            except IntegrationError:
                continue
            self.state.clear_override(device)
            recovered.append(device)
        return tuple(recovered)

    def _devices(self) -> tuple[tuple[str, KnownDeviceSettings], ...]:
        return (
            ("heater", self.settings.heater),
            ("dehumidifier", self.settings.dehumidifier),
            ("ventilation", self.settings.ventilation),
        )

    @staticmethod
    def _require_available(config: KnownDeviceSettings) -> None:
        if not config.enabled or not config.configured:
            raise IntegrationError(
                "capability_unavailable", "environment control is unconfigured"
            )
        if config.require_fresh_sensor and not config.safety_entity:
            raise IntegrationError(
                "safety_unconfigured", "required safety sensor is unconfigured"
            )

    def _interlock_status(self, config: KnownDeviceSettings) -> str:
        if not config.require_fresh_sensor:
            return "not_required"
        if not config.safety_entity:
            return "unconfigured"
        try:
            self._fresh_safety_record(config)
        except IntegrationError:
            return "blocked"
        return "ready"

    def _require_interlock(self, config: KnownDeviceSettings) -> None:
        if config.require_fresh_sensor:
            self._fresh_safety_record(config)

    def _fresh_safety_record(self, config: KnownDeviceSettings) -> object:
        snapshot = self.sensors.snapshot()
        record = next(
            (
                item
                for item in snapshot.records
                if item.source_id == config.safety_entity
            ),
            None,
        )
        if record is None or record.status != "online":
            raise IntegrationError(
                "safety_sensor_unavailable", "required safety sensor is unavailable"
            )
        try:
            if record.last_seen is None:
                raise ValueError("missing timestamp")
            observed = datetime.fromisoformat(record.last_seen.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IntegrationError(
                "safety_sensor_stale", "safety sensor timestamp is invalid"
            ) from exc
        age = datetime.now(timezone.utc).timestamp() - observed.timestamp()
        if age < 0 or age > config.safety_max_age_seconds:
            raise IntegrationError(
                "safety_sensor_stale", "required safety sensor is stale"
            )
        return record


def _first_float(path: Path) -> float | None:
    try:
        return float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _memory() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        name, _, raw = line.partition(":")
        first = raw.strip().split()
        if name in {"MemTotal", "MemAvailable"} and first and first[0].isdigit():
            values[name] = int(first[0]) * 1024
    return values


def _temperature() -> float | None:
    for path in (
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/hwmon/hwmon0/temp1_input"),
    ):
        value = _first_float(path)
        if value is not None:
            return round(value / 1000 if value > 200 else value, 2)
    return None
