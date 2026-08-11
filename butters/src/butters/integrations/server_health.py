"""Fixed local server-health adapter with no caller-supplied command surface."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from butters.integrations.model import ServerHealthSnapshot, ServiceHealth

SERVICE_ALLOWLIST = (
    ("MQTT", "mosquitto.service"),
    ("InfluxDB", "influxdb.service"),
    ("Grafana", "grafana-server.service"),
    ("Sensor bridge", "home-sensor-bridge.service"),
    ("Dashboard", "home-sensor-dashboard.service"),
    ("Export worker", "home-sensor-export-worker.service"),
    ("Docker", "docker.service"),
    ("containerd", "containerd.service"),
    ("Tailscale", "tailscaled.service"),
)


Runner = Callable[..., Any]


class LocalServerHealthAdapter:
    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        root: Path = Path("/"),
    ) -> None:
        self._runner = runner
        self._root = root

    def snapshot(self) -> ServerHealthSnapshot:
        load_1m, load_5m, load_15m = os.getloadavg()
        memory = _meminfo()
        disk = shutil.disk_usage(self._root)
        return ServerHealthSnapshot(
            uptime_seconds=_uptime(),
            load_1m=load_1m,
            load_5m=load_5m,
            load_15m=load_15m,
            available_memory_bytes=_kib_to_bytes(memory.get("MemAvailable")),
            swap_used_bytes=_swap_used(memory),
            disk_free_bytes=disk.free,
            disk_total_bytes=disk.total,
            temperature_c=_temperature(),
            throttled=self._throttled(),
            services=self._services(),
        )

    def _services(self) -> tuple[ServiceHealth, ...]:
        units = [unit for _, unit in SERVICE_ALLOWLIST]
        states = ["unknown"] * len(units)
        try:
            result = self._runner(
                ["systemctl", "is-active", *units],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            output = str(getattr(result, "stdout", ""))
            parsed = [line.strip() for line in output.splitlines()]
            for index, state in enumerate(parsed[: len(states)]):
                states[index] = state or "unknown"
        except (OSError, subprocess.SubprocessError):
            pass
        return tuple(
            ServiceHealth(name, unit, state == "active", state)
            for (name, unit), state in zip(SERVICE_ALLOWLIST, states, strict=True)
        )

    def _throttled(self) -> str | None:
        try:
            result = self._runner(
                ["vcgencmd", "get_throttled"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"throttled=(0x[0-9a-fA-F]+)", str(result.stdout))
        return match.group(1).lower() if match else None


def _meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        name, separator, value = line.partition(":")
        if separator and name in {"MemAvailable", "SwapTotal", "SwapFree"}:
            try:
                result[name] = int(value.split()[0])
            except (IndexError, ValueError):
                continue
    return result


def _uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _temperature() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text(encoding="utf-8")
        return int(raw.strip()) / 1000
    except (OSError, ValueError):
        return None


def _kib_to_bytes(value: int | None) -> int | None:
    return value * 1024 if value is not None else None


def _swap_used(memory: dict[str, int]) -> int | None:
    total = memory.get("SwapTotal")
    free = memory.get("SwapFree")
    return (total - free) * 1024 if total is not None and free is not None else None
