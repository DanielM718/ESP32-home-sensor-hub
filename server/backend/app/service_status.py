"""Safe read-only status snapshots for the dashboard's fixed service allow-list."""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

SERVICE_DEFINITIONS = (
    ("Home Sensor dashboard", "home-sensor-dashboard.service", True),
    ("MQTT to InfluxDB bridge", "home-sensor-bridge.service", True),
    ("CSV export worker", "home-sensor-export-worker.service", True),
    ("Mosquitto MQTT broker", "mosquitto.service", True),
    ("InfluxDB", "influxdb.service", True),
    ("Grafana", "grafana-server.service", True),
)
SYSTEMD_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "Description",
    "ActiveEnterTimestamp",
    "ActiveEnterTimestampUSec",
)


Runner = Callable[..., Any]


class SystemStatusProvider:
    """Inspect only project-approved systemd units; never accepts a unit parameter."""

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        services: Sequence[tuple[str, str, bool]] = SERVICE_DEFINITIONS,
    ) -> None:
        self.runner = runner
        self.services = tuple(services)

    def snapshot(self) -> dict[str, Any]:
        checked = datetime.now(timezone.utc)
        return {
            "checked_at_utc": _iso_utc(checked),
            "hostname": socket.gethostname(),
            "backend": {"status": "ok"},
            "services": [
                self._service_status(display_name, unit, core, checked)
                for display_name, unit, core in self.services
            ],
        }

    def _service_status(
        self,
        display_name: str,
        unit: str,
        core: bool,
        checked: datetime,
    ) -> dict[str, Any]:
        base = {
            "display_name": display_name,
            "unit": unit,
            "core": core,
            "installed": False,
            "active": False,
            "load_state": "unknown",
            "active_state": "unknown",
            "sub_state": "unknown",
            "description": None,
            "state_entered_at": None,
            "uptime_seconds": None,
            "checked_at_utc": _iso_utc(checked),
            "commands": {
                "status": f"systemctl status {unit} --no-pager",
                "logs": f"journalctl -u {unit} -n 100 --no-pager",
            },
        }
        try:
            completed = self.runner(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--no-pager",
                    "--property=" + ",".join(SYSTEMD_PROPERTIES),
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            values = _parse_properties(getattr(completed, "stdout", ""))
            load_state = values.get("LoadState", "not-found")
            active_state = values.get("ActiveState", "inactive")
            base.update(
                {
                    "installed": load_state not in {"not-found", "error", "unknown"},
                    "active": active_state == "active",
                    "load_state": load_state,
                    "active_state": active_state,
                    "sub_state": values.get("SubState", "unknown"),
                    "description": values.get("Description") or None,
                    "state_entered_at": values.get("ActiveEnterTimestamp") or None,
                    "uptime_seconds": _uptime_seconds(
                        values.get("ActiveEnterTimestampUSec"), checked
                    ),
                }
            )
            if getattr(completed, "returncode", 0) != 0 and not values:
                base["load_state"] = "not-found"
                base["active_state"] = "inactive"
                base["sub_state"] = "dead"
        except (OSError, subprocess.SubprocessError) as exc:
            base["error"] = type(exc).__name__
        return base


def _parse_properties(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in SYSTEMD_PROPERTIES:
            result[key] = value
    return result


def _uptime_seconds(value: str | None, checked: datetime) -> int | None:
    if not value:
        return None
    try:
        entered = int(value) / 1_000_000
    except ValueError:
        return None
    return max(0, int(checked.timestamp() - entered)) if entered > 0 else None


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
