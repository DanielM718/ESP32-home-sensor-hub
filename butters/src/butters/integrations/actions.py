"""Fixed host, NAS, and environmental capability adapters."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from butters.actions.broker import BrokerClient, BrokerError, BrokerOperation
from butters.actions.store import ActionStateStore
from butters.assistant_config import ActionSettings, BrokerSettings, KnownDeviceSettings
from butters.integrations.model import IntegrationError, SensorSnapshotProvider


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
    def __init__(
        self, settings: KnownDeviceSettings, actions: FixedActionAdapter
    ) -> None:
        self.settings = settings
        self.actions = actions

    def status(self) -> dict[str, object]:
        return {
            "device": "nas",
            "configured": self.settings.configured,
            "enabled": self.settings.enabled,
            "network_reachable": None,
            "status": "unconfigured" if not self.settings.configured else "unknown",
        }

    def wake(self, cancel_event: threading.Event | None = None) -> dict[str, object]:
        self._require()
        return self.actions.execute(BrokerOperation.NAS_WAKE, cancel_event=cancel_event)

    def _require(self) -> None:
        if not self.settings.enabled or not self.settings.configured:
            raise IntegrationError(
                "capability_unavailable", "NAS wake is not configured"
            )


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
