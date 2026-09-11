"""Safe read-only status snapshots for the dashboard's fixed service allow-list."""

from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

SERVICE_DEFINITIONS = (
    ("Home Sensor dashboard", "home-sensor-dashboard.service", True),
    ("MQTT to InfluxDB bridge", "home-sensor-bridge.service", True),
    ("CSV export worker", "home-sensor-export-worker.service", True),
    # Enabled and running in production since the read-only X2D observer
    # shipped, but it was never listed here, so printer ingest was the one
    # core unit whose failure the dashboard could not show.
    ("Printer observer", "home-sensor-printer-observer.service", True),
    ("Mosquitto MQTT broker", "mosquitto.service", True),
    ("InfluxDB", "influxdb.service", True),
    ("Grafana", "grafana-server.service", True),
    # Neighbouring units on the same Pi. They are not required for sensor
    # ingest, so they are reported non-core: a Butters outage must never make
    # the sensor dashboard describe itself as broken.
    ("Butters web assistant", "butters-web.service", False),
    ("Butters action broker socket", "butters-action-broker.socket", False),
)
SYSTEMD_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "Description",
    "ActiveEnterTimestamp",
    # systemd exposes the machine-readable activation instant as
    # ActiveEnterTimestampMonotonic (microseconds on CLOCK_MONOTONIC since
    # boot). There is no ActiveEnterTimestampUSec property: asking for one
    # makes systemd silently omit it, which is why uptime_seconds was null for
    # every unit in production. It is still parsed when present so a systemd
    # that does provide it keeps working.
    "ActiveEnterTimestampMonotonic",
    "ActiveEnterTimestampUSec",
)


Runner = Callable[..., Any]


def _monotonic_since_boot() -> float:
    """Seconds on the same clock systemd stamps *TimestampMonotonic with."""

    return time.clock_gettime(time.CLOCK_MONOTONIC)


class SystemStatusProvider:
    """Inspect only project-approved systemd units; never accepts a unit parameter."""

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        services: Sequence[tuple[str, str, bool]] = SERVICE_DEFINITIONS,
        monotonic: Callable[[], float] = _monotonic_since_boot,
    ) -> None:
        self.runner = runner
        self.services = tuple(services)
        self.monotonic = monotonic

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
                    "uptime_seconds": self._uptime_seconds(values, checked),
                }
            )
            if getattr(completed, "returncode", 0) != 0 and not values:
                base["load_state"] = "not-found"
                base["active_state"] = "inactive"
                base["sub_state"] = "dead"
        except (OSError, subprocess.SubprocessError) as exc:
            base["error"] = type(exc).__name__
        return base


    def _uptime_seconds(
        self, values: dict[str, str], checked: datetime
    ) -> int | None:
        """Prefer the monotonic stamp so an NTP step cannot invent uptime.

        This Pi has no battery-backed clock, so wall time can jump by hours the
        first time NTP settles after a boot. CLOCK_MONOTONIC is immune to that,
        and it is the clock systemd itself stamps the unit with.
        """

        monotonic_uptime = _monotonic_uptime_seconds(
            values.get("ActiveEnterTimestampMonotonic"), self.monotonic
        )
        if monotonic_uptime is not None:
            return monotonic_uptime
        return _uptime_seconds(values.get("ActiveEnterTimestampUSec"), checked)


def _monotonic_uptime_seconds(
    value: str | None, monotonic: Callable[[], float]
) -> int | None:
    if not value:
        return None
    try:
        entered = int(value) / 1_000_000
    except ValueError:
        return None
    # systemd reports 0 for a unit that has never entered the active state.
    if entered <= 0:
        return None
    try:
        now = monotonic()
    except OSError:
        return None
    return max(0, int(now - entered))


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
