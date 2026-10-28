"""Bounded KR260 observation through the privileged fixed-operation broker."""

from __future__ import annotations

import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from butters.actions.broker import BrokerClient, BrokerError, BrokerOperation
from butters.assistant_config import Kr260Settings


@dataclass(frozen=True, slots=True)
class Kr260Observation:
    value: dict[str, object]
    cached_at: float


class Kr260Observer:
    """Observe exactly one configured board; no host or command is callable."""

    def __init__(
        self,
        settings: Kr260Settings,
        *,
        broker: BrokerClient,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.runner = runner
        self.clock = clock
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: Kr260Observation | None = None

    def observe(self, *, refresh: bool = False) -> dict[str, object]:
        if not self.settings.enabled:
            return self._unavailable("disabled")
        with self._lock:
            cached = self._cached
            if (
                cached is not None
                and not refresh
                and self.monotonic() - cached.cached_at < self.settings.poll_interval_seconds
            ):
                return dict(cached.value)
            value = self._probe()
            self._cached = Kr260Observation(dict(value), self.monotonic())
            return value

    def _probe(self) -> dict[str, object]:
        observed_at = self.clock()
        ping = self._ping()
        try:
            result = self.broker.request(
                BrokerOperation.KR260_STATUS,
                request_id=secrets.token_urlsafe(18),
                timeout_seconds=self.settings.command_timeout_seconds + 2,
            )
        except BrokerError:
            return self._offline(observed_at, ping, "ssh_unavailable")
        if not result.ok:
            error = (
                "identity_mismatch"
                if result.error_code == "identity_mismatch"
                else "ssh_failed"
            )
            return self._offline(
                observed_at,
                ping,
                error,
                ssh=error == "identity_mismatch",
            )
        telemetry = result.status.get("telemetry")
        if not isinstance(telemetry, str):
            return self._offline(observed_at, ping, "malformed_result")
        return self._from_telemetry(telemetry, observed_at, ping)

    def _from_telemetry(
        self, telemetry: str, observed_at: float, ping: bool
    ) -> dict[str, object]:
        parsed = _parse(telemetry)
        identity_ok = (
            parsed.get("hostname") == self.settings.expected_hostname
            and parsed.get("machine_id") == self.settings.expected_machine_id
        )
        if not identity_ok:
            return self._offline(observed_at, ping, "identity_mismatch", ssh=True)
        memory_total = _kib(parsed.get("MemTotal"))
        memory_available = _kib(parsed.get("MemAvailable"))
        cpu = _cpu_percent(parsed.get("cpu_start"), parsed.get("cpu_end"))
        return {
            "device": "kr260",
            "operational_state": "online",
            "online": True,
            "network_reachable": bool(ping),
            "ssh_available": True,
            "identity_verified": True,
            "hostname": parsed.get("hostname"),
            "machine_id": parsed.get("machine_id"),
            "boot_id": parsed.get("boot_id"),
            "kernel": parsed.get("kernel"),
            "uptime_seconds": _float(parsed.get("uptime")),
            "cpu_utilization_percent": cpu,
            "load_1m": _load(parsed.get("load"), 0),
            "load_5m": _load(parsed.get("load"), 1),
            "load_15m": _load(parsed.get("load"), 2),
            "memory_total_bytes": memory_total,
            "memory_available_bytes": memory_available,
            "memory_utilization_percent": _used_percent(memory_total, memory_available),
            "swap_total_bytes": _kib(parsed.get("SwapTotal")),
            "swap_free_bytes": _kib(parsed.get("SwapFree")),
            "soc_temperature_c": _millic(parsed.get("soc_temp_millic")),
            "network": {
                "interface": "eth1",
                "state": parsed.get("if_state"),
                "mac": parsed.get("if_mac"),
                "ipv4": parsed.get("ipv4"),
                "ipv6": parsed.get("ipv6"),
                "default_route": parsed.get("default_route"),
                "rx_bytes": _int(parsed.get("if_rx")),
                "tx_bytes": _int(parsed.get("if_tx")),
            },
            "reboot_required": parsed.get("reboot_required") == "true",
            "linux_state": parsed.get("system_state") or "unknown",
            "observed_at": observed_at,
            "error": None,
        }

    def _ping(self) -> bool:
        try:
            completed = self.runner(
                ["/usr/bin/ping", "-c", "1", "-W", "1", self.settings.host],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def _offline(
        self, observed_at: float, ping: bool, error: str, *, ssh: bool = False
    ) -> dict[str, object]:
        return {
            **self._unavailable(error),
            "operational_state": "identity_failed" if ssh else "offline",
            "online": bool(ping),
            "network_reachable": bool(ping),
            "ssh_available": ssh,
            "identity_verified": False,
            "observed_at": observed_at,
        }

    @staticmethod
    def _unavailable(error: str) -> dict[str, object]:
        return {
            "device": "kr260",
            "operational_state": "unconfigured" if error == "disabled" else "offline",
            "online": False,
            "network_reachable": False,
            "ssh_available": False,
            "identity_verified": False,
            "observed_at": None,
            "error": error,
        }


def _parse(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key and key.replace("_", "").isalnum():
            parsed[key] = value.strip()[:4096]
            continue
        if ":" in line:
            name, _, raw = line.partition(":")
            if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                parsed[name] = raw.strip().split()[0]
    return parsed


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    try:
        return round(float(str(value)), 2)
    except (TypeError, ValueError):
        return None


def _kib(value: object) -> int | None:
    number = _int(value)
    return None if number is None else number * 1024


def _millic(value: object) -> float | None:
    number = _float(value)
    return None if number is None else round(number / 1000.0, 2)


def _load(value: object, index: int) -> float | None:
    try:
        return round(float(str(value).split()[index]), 3)
    except (TypeError, ValueError, IndexError):
        return None


def _used_percent(total: int | None, available: int | None) -> float | None:
    if total is None or available is None or total <= 0 or available > total:
        return None
    return round((total - available) * 100.0 / total, 2)


def _cpu_percent(start: object, end: object) -> float | None:
    try:
        first = [int(item) for item in str(start).split()[1:]]
        second = [int(item) for item in str(end).split()[1:]]
        total = sum(second) - sum(first)
        idle = sum(second[3:5]) - sum(first[3:5])
    except (TypeError, ValueError):
        return None
    if total <= 0 or idle < 0:
        return None
    return round(max(0.0, min(100.0, (total - idle) * 100.0 / total)), 2)
