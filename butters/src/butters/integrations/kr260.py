"""Bounded KR260 observation through the privileged fixed-operation broker."""

from __future__ import annotations

import ipaddress
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from butters.actions.broker import BrokerClient, BrokerError, BrokerOperation
from butters.assistant_config import Kr260Settings

# An explicit refresh still re-probes at most this often, so a repeatedly
# clicked Admin refresh cannot turn into an SSH loop against an offline board.
MIN_REFRESH_INTERVAL_SECONDS = 10.0

# Broker failure codes and what they say about the board. Anything else is an
# unclassified SSH failure. Only these fixed literals reach the result.
_BROKER_FAILURES: dict[str, tuple[str, bool]] = {
    # code -> (error, the board answered SSH as some host)
    "identity_mismatch": ("identity_mismatch", True),
    "host_key_mismatch": ("host_key_mismatch", True),
    "ssh_auth_failed": ("ssh_auth_failed", False),
    "host_unreachable": ("host_unreachable", False),
    "operation_timeout": ("ssh_timeout", False),
    "malformed_result": ("malformed_result", False),
    "operation_unavailable": ("capability_disabled", False),
}


# The fixed telemetry command reads only this interface.
_INTERFACE = "eth1"
# One `ip -o addr` record: "<index>: <ifname>    inet[6] <address>/<prefix> ...".
_ADDRESS_RECORD = re.compile(
    r"\d+:\s+(?P<ifname>[^\s:]+)\s+(?P<family>inet6?)\s+(?P<cidr>\S+)"
)
# Each field is `printf 'key='; command`, so a command that prints nothing
# (no address, missing interface) leaves the next field glued to its line.
_GLUED_FIELD = re.compile(r"[a-z][a-z0-9_]*=")
_MAX_ADDRESSES = 16
# Reviewed PL designs by the SHA-256 of their applied overlay (.dtbo): the same
# allowlist as SAFE_OVERLAYS in config/kr260/kr260-kexec-restart.sh.
_KNOWN_PL_DESIGNS = {
    "1e998ffd5d33a05da3746df3603e65603e1628006bc878209a836cc9bd3c4ae1": (
        "k26-starter-kits"
    ),
}
# Linux FPGA Manager states (drivers/fpga/fpga-mgr.c).
_FPGA_STATES = frozenset(
    {
        "unknown",
        "power off",
        "power up",
        "reset",
        "firmware request",
        "firmware request error",
        "write init",
        "write init error",
        "write",
        "write error",
        "write complete",
        "write complete error",
        "operating",
    }
)
# "fpga_overlay=<configfs name> <status> <sha256 of /lib/firmware/<path>|->".
_FPGA_OVERLAY = re.compile(
    r"fpga_overlay=([A-Za-z0-9_.-]{1,64}) ([a-z_]{0,16}) ([0-9a-f]{64}|-)"
)
_MAX_OVERLAYS = 4


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
            if cached is not None:
                age = self.monotonic() - cached.cached_at
                if age < (
                    MIN_REFRESH_INTERVAL_SECONDS
                    if refresh
                    else self.settings.poll_interval_seconds
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
            error, answered = _BROKER_FAILURES.get(
                str(result.error_code), ("ssh_failed", False)
            )
            return self._offline(observed_at, ping, error, ssh=answered)
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
                "interface": _INTERFACE,
                "state": parsed.get("if_state"),
                "mac": parsed.get("if_mac"),
                "ipv4": _addresses(telemetry, 4),
                "ipv6": _addresses(telemetry, 6),
                "default_route": parsed.get("default_route"),
                "rx_bytes": _int(parsed.get("if_rx")),
                "tx_bytes": _int(parsed.get("if_tx")),
            },
            "fpga": _fpga(parsed, telemetry),
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

    def invalidate(self) -> None:
        """Drop the cached observation, e.g. after a reboot was requested."""

        with self._lock:
            self._cached = None

    def _offline(
        self, observed_at: float, ping: bool, error: str, *, ssh: bool = False
    ) -> dict[str, object]:
        return {
            **self._unavailable(error),
            "operational_state": (
                "identity_failed"
                if ssh
                else "disabled"
                if error == "capability_disabled"
                else "offline"
            ),
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
            while _GLUED_FIELD.match(value):
                parsed[key] = ""
                key, _, value = value.partition("=")
            parsed[key] = value.strip()[:4096]
            continue
        if ":" in line:
            name, _, raw = line.partition(":")
            if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                parsed[name] = raw.strip().split()[0]
    return parsed


def _addresses(output: str, version: int) -> list[str]:
    """Global addresses of the fixed interface as CIDR strings, in board order.

    Records are found by shape rather than by which `key=` line they follow, so
    extra addresses on unprefixed lines and glued empty fields are both handled.
    """

    family = "inet6" if version == 6 else "inet"
    found: list[str] = []
    for line in output.splitlines():
        while _GLUED_FIELD.match(line):
            line = line.partition("=")[2]
        record = _ADDRESS_RECORD.match(line.strip())
        if (
            record is None
            or record["ifname"] != _INTERFACE
            or record["family"] != family
        ):
            continue
        try:
            address = ipaddress.ip_interface(record["cidr"])
        except ValueError:
            continue
        if address.version == version and str(address) not in found:
            found.append(str(address))
        if len(found) >= _MAX_ADDRESSES:
            break
    return found


def _fpga(parsed: dict[str, str], output: str) -> dict[str, object]:
    """Read-only PL state: the FPGA Manager and the applied overlays.

    The design is identified by its overlay's SHA-256 only; the bitstream the
    overlay names is not hashed on every poll. Telemetry from a broker without
    these lines reads as "unavailable".
    """

    if "fpga_state" not in parsed:
        return {
            "state": "unavailable",
            "manager_state": None,
            "overlays": [],
            "design": None,
            "starter_kit_loaded": False,
            "status": None,
        }
    raw_state = parsed.get("fpga_state", "")
    manager = raw_state if raw_state in _FPGA_STATES else None
    status = parsed.get("fpga_status", "").strip()
    overlays = []
    for line in output.splitlines():
        match = _FPGA_OVERLAY.fullmatch(line.strip())
        if match is None:
            continue
        sha256 = None if match[3] == "-" else match[3]
        overlays.append(
            {
                "name": match[1],
                "status": match[2] or "unknown",
                "sha256": sha256,
                "design": _KNOWN_PL_DESIGNS.get(sha256 or ""),
            }
        )
        if len(overlays) >= _MAX_OVERLAYS:
            break
    applied = [item for item in overlays if item["status"] == "applied"]
    designs = {item["design"] for item in applied}
    if manager is None:
        state = "unavailable" if not raw_state else "unrecognized"
    elif "error" in manager or status:
        state = "error"
    elif not applied:
        state = (
            "unconfigured" if manager != "operating" else "operating_without_overlay"
        )
    elif None in designs:
        state = "unreviewed_design"
    else:
        state = manager
    return {
        "state": state,
        "manager_state": manager,
        # The ZynqMP driver's error text, letters and spaces only.
        "status": status
        if status.replace(" ", "").isalpha()
        else ("" if not status else "unrecognized"),
        "overlays": overlays,
        "design": next(iter(designs)) if len(designs) == 1 else None,
        "starter_kit_loaded": "k26-starter-kits" in designs and None not in designs,
    }


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
