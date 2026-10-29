"""Strict enumerated Unix-socket protocol for privileged fixed operations."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import secrets
import socket
import struct
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from butters.assistant_config import BrokerSettings

LOGGER = logging.getLogger(__name__)

# The broker answers connections serially, so peer I/O has to be bounded. The
# client spends at most 2s connecting and waits 30s for a reply, and a request
# is a single small write issued immediately after connect(); 5s is generous for
# a healthy peer while keeping a stalled one from holding the boundary open.
DEFAULT_PEER_TIMEOUT_SECONDS = 5.0

# switch.turn_on/turn_off returns as soon as Home Assistant dispatches the call,
# but the two reviewed outlets settle independently. Production measured ~1.1s of
# skew powering off and ~15s for switch.desktop_gigabyte powering on, so a single
# immediate read reported a partial failure for an operation that had in fact
# fully succeeded. Poll to a fixed deadline instead: 20s clears the observed 15s
# with margin while leaving the client's 30s request_timeout_seconds (see
# BrokerSettings) ample room for the two reads and the service call around it.
# Both values are broker-local constants; no peer or configuration input reaches
# them.
MONITOR_SETTLE_DEADLINE_SECONDS = 20.0
MONITOR_SETTLE_POLL_SECONDS = 0.5

# One bound for the whole monitor operation: the two reads before, the service
# call, and every poll of the settling loop. The settle deadline alone was only
# checked BETWEEN iterations, so each iteration was free to spend its own full
# budget on top of it. Measured against a Home Assistant that was merely slow --
# 9s per request, never wedged, so no per-exchange bound ever tripped -- one
# operation took 63.5s against a declared 20s: the same shape as the
# reachability overrun DesktopWorkflow already fixed, and the same fix.
#
# 25s keeps the whole operation inside the client's 30s request_timeout_seconds.
# Beyond that the caller gives up while this root process is still mutating,
# which is the state that invites a retry of an operation already in flight.
MONITOR_OPERATION_DEADLINE_SECONDS = 25.0

# desktop.wake never transmits a packet unless a fresh read, taken by the wake
# operation itself, finds both monitor outlets off. Windows enumerates whatever
# physical display is powered while it boots, which defeats the virtual-display
# capture every remote mode depends on, so the order is an invariant enforced
# here, below every caller. The precondition only reads; turning the outlets
# off stays the caller's explicit, separately audited desktop.monitors_off.
WAKE_PRECONDITION_DEADLINE_SECONDS = 8.0
# A P115 whose relay is open meters 0.0 W. A reading above this contradicts a
# reported "off" -- typically the plug has not reported since the outlet
# changed -- so it blocks a wake until a later reading agrees. A missing or
# non-numeric reading is not evidence either way; the switch state decides.
MONITOR_OFF_POWER_CEILING_W = 1.0

# urlopen's timeout is per socket operation, not a wall-clock bound, so a peer
# that sends one byte just inside every timeout window holds a single read open
# for as long as it likes. The broker answers connections serially, so that one
# read would block every other privileged operation, including host.reboot,
# with no watchdog and Restart=no to recover it. Bound the whole exchange.
HOME_ASSISTANT_DEADLINE_SECONDS = 10.0
HOME_ASSISTANT_READ_CHUNK_BYTES = 8192
HOME_ASSISTANT_MAX_RESPONSE_BYTES = 65536
# The NAS shutdown transport is one fixed TrueNAS middleware call. It is
# bounded exactly like the Home Assistant exchange so a hung NAS cannot hold
# the serial broker open.
NAS_API_DEADLINE_SECONDS = 10.0
NAS_SHUTDOWN_PATH = "/api/v2.0/system/shutdown"

# Fixed KR260 restart: exactly the argv the board's sudoers rule permits. The
# board's warm reboot (`systemctl reboot` -> PMUFW system reset) wedges it until
# a power cycle, so the restart is a kexec through a root-owned wrapper
# (config/kr260/kr260-kexec-restart.sh) that refuses rather than reboots.
_KR260_RESTART_WRAPPER = "/usr/local/sbin/butters-kr260-kexec-restart"
_KR260_REBOOT_COMMAND = f"/usr/bin/sudo -n {_KR260_RESTART_WRAPPER}"
# The wrapper's refusals. Each is decided before the kexec handoff, so nothing
# was restarted.
_KR260_RESTART_REFUSALS: dict[int, str] = {
    10: "restart_already_staged",
    11: "restart_precondition_failed",
    12: "restart_precondition_failed",
    13: "restart_precondition_failed",
    14: "watchdog_active",
    15: "pl_workload_active",
    21: "restart_failed",
    22: "restart_failed",
}
KR260_CONNECT_TIMEOUT_SECONDS = 5

# Fixed KR260 observation script. It has no caller-controlled interpolation and
# returns only bounded OS/proc/sysfs data for parsing by the unprivileged tier.
_KR260_TELEMETRY_COMMAND = r"""LC_ALL=C
printf 'hostname='; hostname
printf 'machine_id='; cat /etc/machine-id
printf 'boot_id='; cat /proc/sys/kernel/random/boot_id
printf 'kernel='; uname -sr
printf 'uptime='; cut -d' ' -f1 /proc/uptime
printf 'load='; cat /proc/loadavg
printf 'cpu_start='; head -n1 /proc/stat
sleep 0.1
printf 'cpu_end='; head -n1 /proc/stat
grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
printf 'reboot_required='; if test -f /var/run/reboot-required; then echo true; else echo false; fi
printf 'system_state='; systemctl is-system-running 2>/dev/null || true
printf 'if_state='; cat /sys/class/net/eth1/operstate
printf 'if_mac='; cat /sys/class/net/eth1/address
printf 'if_rx='; cat /sys/class/net/eth1/statistics/rx_bytes
printf 'if_tx='; cat /sys/class/net/eth1/statistics/tx_bytes
printf 'ipv4='; ip -o -4 addr show dev eth1 scope global
printf 'ipv6='; ip -o -6 addr show dev eth1 scope global
printf 'default_route='; ip route show default | head -n1
for hw in /sys/class/hwmon/hwmon*; do
  test -r "$hw/name" || continue
  test "$(cat "$hw/name")" = ams || continue
  test -r "$hw/temp1_input" && { printf 'soc_temp_millic='; cat "$hw/temp1_input"; }
  break
done
printf 'fpga_state=%s\n' "$(cat /sys/class/fpga_manager/fpga0/state 2>/dev/null)"
printf 'fpga_status=%s\n' "$(cat /sys/class/fpga_manager/fpga0/status 2>/dev/null | tr '\n' ' ')"
n=0
for o in /sys/kernel/config/device-tree/overlays/*/; do
  test -d "$o" || continue
  n=$((n + 1)); test "$n" -le 4 || break
  p=$(cat "$o/path" 2>/dev/null)
  case "$p" in ""|/*|*..*) s=- ;; *) s=$(sha256sum < "/lib/firmware/$p" 2>/dev/null | cut -d' ' -f1) ;; esac
  printf 'fpga_overlay=%s %s %s\n' "$(basename "$o")" "$(cat "$o/status" 2>/dev/null)" "${s:--}"
done
"""


class _NoRedirect(HTTPRedirectHandler):
    """Refuse every redirect on the Home Assistant client.

    urllib copies all request headers except content-length/content-type onto a
    redirect target, on any host and any scheme -- so a single 302 from Home
    Assistant would make this root process re-send the long-lived
    ``Authorization: Bearer`` token to an address the redirect chose. Home
    Assistant is a separate trust domain that loads third-party integrations,
    and broker_main only validates the *configured* URL, not a redirect target,
    so the loopback restriction there does not cover this.

    Returning None makes urllib fall through to HTTPDefaultErrorHandler, which
    raises HTTPError for the 3xx. _home_assistant_request already maps that to
    a bounded BrokerError, so the operation fails closed.
    """

    def redirect_request(self, *_arguments: Any, **_keywords: Any) -> None:
        return None


_HOME_ASSISTANT_OPENER = build_opener(_NoRedirect).open

# Only broker-local literals are ever logged, never peer-supplied text.
_CODE_PATTERN = re.compile(r"\A[a-z][a-z_]{0,47}\Z")


class BrokerOperation(str, Enum):
    DESKTOP_WAKE = "desktop.wake"
    DESKTOP_MONITORS_OFF = "desktop.monitors_off"
    DESKTOP_MONITORS_ON = "desktop.monitors_on"
    DESKTOP_PRIMARY_MONITOR_ON = "desktop.primary_monitor_on"
    DESKTOP_PARSEC_STATUS = "desktop.parsec_status"
    DESKTOP_PARSEC_ENSURE = "desktop.parsec_ensure"
    DESKTOP_PARSEC_RESTART = "desktop.parsec_restart"
    DESKTOP_SUNSHINE_SERVICE_STATUS = "desktop.sunshine_service_status"
    DESKTOP_SUNSHINE_STATUS = "desktop.sunshine_status"
    DESKTOP_SUNSHINE_ENSURE = "desktop.sunshine_ensure"
    DESKTOP_SUNSHINE_SESSION_STATUS = "desktop.sunshine_session_status"
    DESKTOP_SUNSHINE_SESSION_END = "desktop.sunshine_session_end"
    DESKTOP_DISPLAY_STATUS = "desktop.display_status"
    DESKTOP_VIRTUAL_DISPLAY_ACTIVATE = "desktop.virtual_display_activate"
    DESKTOP_VIRTUAL_DISPLAY_DEACTIVATE = "desktop.virtual_display_deactivate"
    DESKTOP_LOCK = "desktop.lock"
    DESKTOP_SLEEP = "desktop.sleep"
    DESKTOP_RESTART = "desktop.restart"
    DESKTOP_SHUTDOWN = "desktop.shutdown"
    HOST_RESTART_BUTTERS = "host.restart_butters"
    HOST_REBOOT = "host.reboot"
    HOST_SHUTDOWN = "host.shutdown"
    NAS_WAKE = "nas.wake"
    NAS_SHUTDOWN = "nas.shutdown"
    KR260_STATUS = "kr260.status"
    KR260_REBOOT = "kr260.reboot"
    HEATER_ON = "environment.heater_on"
    HEATER_OFF = "environment.heater_off"
    DEHUMIDIFIER_ON = "environment.dehumidifier_on"
    DEHUMIDIFIER_OFF = "environment.dehumidifier_off"
    VENTILATION_ON = "environment.ventilation_on"
    VENTILATION_OFF = "environment.ventilation_off"


# Every broker operation that runs the fixed Windows helper over SSH. broker_main
# validates the root-owned credential and pinned known_hosts whenever any of
# these is enabled, so a new SSH operation cannot be enabled without them.
DESKTOP_SSH_OPERATIONS = frozenset(
    {
        BrokerOperation.DESKTOP_PARSEC_STATUS,
        BrokerOperation.DESKTOP_PARSEC_ENSURE,
        BrokerOperation.DESKTOP_PARSEC_RESTART,
        BrokerOperation.DESKTOP_SUNSHINE_SERVICE_STATUS,
        BrokerOperation.DESKTOP_SUNSHINE_STATUS,
        BrokerOperation.DESKTOP_SUNSHINE_ENSURE,
        BrokerOperation.DESKTOP_SUNSHINE_SESSION_STATUS,
        BrokerOperation.DESKTOP_SUNSHINE_SESSION_END,
        BrokerOperation.DESKTOP_DISPLAY_STATUS,
        BrokerOperation.DESKTOP_VIRTUAL_DISPLAY_ACTIVATE,
        BrokerOperation.DESKTOP_VIRTUAL_DISPLAY_DEACTIVATE,
        BrokerOperation.DESKTOP_LOCK,
        BrokerOperation.DESKTOP_SLEEP,
        BrokerOperation.DESKTOP_RESTART,
        BrokerOperation.DESKTOP_SHUTDOWN,
    }
)


class _DesktopControlOperation(str, Enum):
    PARSEC_STATUS = "ParsecStatus"
    PARSEC_ENSURE = "ParsecEnsure"
    PARSEC_RESTART = "ParsecRestart"
    SUNSHINE_SERVICE_STATUS = "SunshineServiceStatus"
    SUNSHINE_STATUS = "SunshineStatus"
    SUNSHINE_ENSURE = "SunshineEnsure"
    SUNSHINE_SESSION_STATUS = "SunshineSessionStatus"
    SUNSHINE_SESSION_END = "SunshineSessionEnd"
    # Implemented by the Windows virtual-display activation helper (unpublished
    # branch fix/mtt-virtual-display-activation). Until that helper is wired
    # into desktop-control.ps1 its ValidateSet rejects these names, the SSH
    # command exits non-zero, and every caller fails closed.
    DISPLAY_STATUS = "DisplayStatus"
    VIRTUAL_DISPLAY_ACTIVATE = "VirtualDisplayActivate"
    VIRTUAL_DISPLAY_DEACTIVATE = "VirtualDisplayDeactivate"
    LOCK = "Lock"
    SLEEP = "Sleep"
    RESTART = "Restart"
    SHUTDOWN = "Shutdown"


class BrokerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BrokerResult:
    operation: BrokerOperation
    ok: bool
    status: dict[str, object]
    error_code: str | None = None


class BrokerClient:
    def __init__(
        self,
        settings: BrokerSettings,
        *,
        connector: Callable[..., socket.socket] = socket.socket,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.connector = connector
        self.monotonic = monotonic

    def request(
        self,
        operation: BrokerOperation,
        *,
        request_id: str,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> BrokerResult:
        if not self.settings.enabled:
            raise BrokerError("broker_unconfigured", "action broker is not enabled")
        if not _identifier(request_id):
            raise BrokerError("invalid_request_id", "broker request ID is invalid")
        if cancel_event is not None and cancel_event.is_set():
            raise BrokerError("cancelled", "action was cancelled")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise BrokerError("request_timeout", "broker request timed out")
        deadline = (
            None if timeout_seconds is None else self.monotonic() + timeout_seconds
        )
        request = {
            "version": self.settings.protocol_version,
            "request_id": request_id,
            "operation": operation.value,
        }
        encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > self.settings.max_message_bytes:
            raise BrokerError("request_too_large", "broker request exceeds its limit")
        connection = self.connector(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(
                self._remaining_timeout(self.settings.connect_timeout_seconds, deadline)
            )
            connection.connect(str(self.settings.socket_path))
            connection.settimeout(
                self._remaining_timeout(self.settings.request_timeout_seconds, deadline)
            )
            connection.sendall(encoded)
            raw = _read_line(
                connection,
                self.settings.max_message_bytes,
                deadline=deadline,
                clock=self.monotonic,
            )
        except (OSError, TimeoutError) as exc:
            raise BrokerError(
                "broker_unavailable", "action broker is unavailable"
            ) from exc
        finally:
            connection.close()
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerError(
                "malformed_response", "broker response is invalid"
            ) from exc
        if not isinstance(value, dict) or set(value) != {
            "version",
            "request_id",
            "ok",
            "status",
            "error_code",
        }:
            raise BrokerError("malformed_response", "broker response schema is invalid")
        if (
            value["version"] != self.settings.protocol_version
            or value["request_id"] != request_id
        ):
            raise BrokerError("protocol_mismatch", "broker response binding is invalid")
        if not isinstance(value["ok"], bool) or not isinstance(value["status"], dict):
            raise BrokerError("malformed_response", "broker response types are invalid")
        error_code = value["error_code"]
        if error_code is not None and not isinstance(error_code, str):
            raise BrokerError("malformed_response", "broker error code is invalid")
        return BrokerResult(operation, value["ok"], value["status"], error_code)

    def _remaining_timeout(
        self, configured_timeout: float, deadline: float | None
    ) -> float:
        if deadline is None:
            return configured_timeout
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise BrokerError("request_timeout", "broker request timed out")
        return min(configured_timeout, remaining)


class BrokerServer:
    """One-request handler; listener lifecycle belongs to systemd socket activation."""

    def __init__(
        self,
        operations: dict[BrokerOperation, Callable[[], dict[str, object]]],
        *,
        expected_uid: int,
        protocol_version: int = 1,
        max_message_bytes: int = 8192,
        dedupe_capacity: int = 1024,
        peer_timeout_seconds: float = DEFAULT_PEER_TIMEOUT_SECONDS,
    ) -> None:
        self.operations = dict(operations)
        self.expected_uid = expected_uid
        self.protocol_version = protocol_version
        self.max_message_bytes = max_message_bytes
        self.dedupe_capacity = dedupe_capacity
        self.peer_timeout_seconds = peer_timeout_seconds
        # Ordered so eviction drops the oldest request ID. A plain set evicts an
        # arbitrary member, which can retire a just-seen ID and reopen its replay
        # window while much older IDs are retained.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def handle(self, connection: socket.socket) -> None:
        request_id = "invalid"
        operation_value = ""
        peer_pid = -1
        peer_uid = -1
        try:
            # Bound every byte of peer I/O before reading anything. Connections
            # are served serially, so a peer that connects and then stalls would
            # otherwise hold the privileged boundary open indefinitely; the
            # client-side timeout protects the client, not the broker.
            connection.settimeout(self.peer_timeout_seconds)
            deadline = time.monotonic() + self.peer_timeout_seconds
            peer_pid, peer_uid = _peer_credentials(connection)
            # SO_PEERCRED remains the in-process gate behind the socket's own
            # ownership and mode, and it runs before any parsing.
            if peer_uid != self.expected_uid:
                raise BrokerError("peer_denied", "broker peer is not authorized")
            raw = _read_line(connection, self.max_message_bytes, deadline=deadline)
            request = _parse_request(raw, self.protocol_version)
            request_id = str(request["request_id"])
            with self._lock:
                if request_id in self._seen:
                    raise BrokerError(
                        "replayed_request", "broker request was already used"
                    )
                while len(self._seen) >= self.dedupe_capacity:
                    self._seen.popitem(last=False)
                self._seen[request_id] = None
            operation = BrokerOperation(str(request["operation"]))
            operation_value = operation.value
            handler = self.operations.get(operation)
            if handler is None:
                raise BrokerError(
                    "operation_unavailable", "operation is not provisioned"
                )
            status = handler()
            if not isinstance(status, dict):
                raise BrokerError("malformed_result", "operation result is invalid")
            response = _response(self.protocol_version, request_id, True, status, None)
        except TimeoutError:
            response = _response(
                self.protocol_version, request_id, False, {}, "request_timeout"
            )
        except (BrokerError, ValueError) as exc:
            code = exc.code if isinstance(exc, BrokerError) else "unknown_operation"
            response = _response(self.protocol_version, request_id, False, {}, code)
        except Exception:  # noqa: BLE001 - privileged boundary never leaks details
            response = _response(
                self.protocol_version, request_id, False, {}, "operation_failed"
            )
        encoded = _render(response)
        if len(encoded) > self.max_message_bytes:
            # The outcome is already decided. Drop the oversized detail but keep
            # the decision, so a mutation that ran is never reported as failed
            # and retried.
            response = _response(
                self.protocol_version,
                request_id,
                bool(response["ok"]),
                {},
                response["error_code"]
                if response["ok"] is False
                else "result_too_large",
            )
            encoded = _render(response)
        self._audit(
            peer_uid=peer_uid,
            peer_pid=peer_pid,
            request_id=request_id,
            operation=operation_value,
            response=response,
        )
        try:
            # Re-armed so the reply is not charged the receive deadline that has
            # already elapsed, while staying bounded rather than blocking on a
            # peer that never reads.
            connection.settimeout(self.peer_timeout_seconds)
            connection.sendall(encoded)
        except OSError:
            # The outcome is already decided and audited. A peer that vanishes or
            # stops reading must not take the serial broker down with it.
            LOGGER.warning("butters.broker response was not delivered to the peer")

    def _audit(
        self,
        *,
        peer_uid: int,
        peer_pid: int,
        request_id: str,
        operation: str,
        response: dict[str, object],
    ) -> None:
        """Record one sanitized event per connection at the privilege boundary.

        The web layer audits its own side, but the root broker is a separate
        trust boundary and has to be able to show, from the journal alone, which
        peer asked for which enumerated operation and how it was resolved.

        Every field is an integer from SO_PEERCRED, an enumerated operation, a
        validated request ID, or a fixed result code. Nothing derived from the
        peer's payload, no operation status, and no exception text is logged,
        and a logging failure never turns a rejection into a permission.
        """

        ok = bool(response["ok"])
        code = response["error_code"]
        # A journal failure has nowhere else to be reported and must not become a
        # reason to permit, refuse, or retry the operation decided above.
        with contextlib.suppress(Exception):
            LOGGER.log(
                logging.INFO if ok else logging.WARNING,
                "butters.broker request outcome=%s result=%s operation=%s "
                "request_id=%s peer_uid=%d peer_pid=%d",
                "accepted" if ok else "rejected",
                _fixed_code("ok" if code is None else str(code)),
                _enumerated_operation(operation),
                _identifier_tag(request_id),
                peer_uid,
                peer_pid,
            )


@dataclass(frozen=True, slots=True)
class FixedBrokerConfig:
    desktop_host: str
    desktop_user: str
    desktop_mac: str
    desktop_broadcast: str
    desktop_key: Path
    nas_mac: str = ""
    nas_broadcast: str = ""
    nas_api_url: str = ""
    enabled_operations: frozenset[BrokerOperation] = frozenset()
    home_assistant_url: str = "http://127.0.0.1:8123"
    kr260_host: str = ""
    kr260_user: str = "ubuntu"
    kr260_key: Path = Path("/nonexistent")
    kr260_expected_machine_id: str = ""

    @property
    def desktop_known_hosts(self) -> Path:
        """Pinned host key beside the credential in the same root-owned directory.

        The desktop credential can power off a machine, so the broker must not
        fall back to trust-on-first-use. Deriving the path keeps the pin and the
        key provisioned together rather than adding a separately forgettable
        configuration field.
        """

        return self.desktop_key.parent / "known_hosts"

    @property
    def kr260_known_hosts(self) -> Path:
        return self.kr260_key.parent / "known_hosts"


class FixedBrokerOperations:
    """Fixed reviewed argv templates; no caller-controlled field reaches argv."""

    MONITOR_ENTITIES = (
        "switch.desktop_gigabyte",
        "switch.desktop_oled",
    )
    # Local use needs one usable physical display, and the G27QC is the primary
    # one. The AORUS OLED is intentionally off during ordinary local use, so
    # desktop.primary_monitor_on observes it but never changes it.
    PRIMARY_MONITOR_ENTITY = "switch.desktop_gigabyte"
    PRESERVED_MONITOR_ENTITY = "switch.desktop_oled"
    # Each outlet's own P115 meter, read only as supporting evidence for "off".
    MONITOR_POWER_SENSORS: ClassVar[dict[str, str]] = {
        "switch.desktop_gigabyte": "sensor.desktop_gigabyte_current_consumption",
        "switch.desktop_oled": "sensor.desktop_oled_current_consumption",
    }

    def __init__(
        self,
        config: FixedBrokerConfig,
        *,
        runner: Callable[..., Any] = subprocess.run,
        home_assistant_token: str = "",
        nas_api_key: str = "",
        opener: Callable[..., Any] = _HOME_ASSISTANT_OPENER,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        home_assistant_deadline_seconds: float = HOME_ASSISTANT_DEADLINE_SECONDS,
    ) -> None:
        self.config = config
        self.runner = runner
        self.home_assistant_token = home_assistant_token.strip()
        self.nas_api_key = nas_api_key.strip()
        self.opener = opener
        # Seams for the settling tests only, alongside runner/opener. The
        # deadline and interval themselves stay fixed module constants.
        self.sleeper = sleeper
        self.monotonic = monotonic
        # One worker for every Home Assistant exchange, created on first use and
        # never joined; see _bounded_exchange.
        self.home_assistant_deadline_seconds = float(home_assistant_deadline_seconds)
        self._exchange_lock = threading.Lock()
        self._exchange_worker: ThreadPoolExecutor | None = None
        self._exchange_pending: Any = None

    def handlers(self) -> dict[BrokerOperation, Callable[[], dict[str, object]]]:
        candidates = {
            BrokerOperation.DESKTOP_WAKE: self.desktop_wake,
            BrokerOperation.DESKTOP_MONITORS_OFF: self.desktop_monitors_off,
            BrokerOperation.DESKTOP_MONITORS_ON: self.desktop_monitors_on,
            BrokerOperation.DESKTOP_PRIMARY_MONITOR_ON: self.desktop_primary_monitor_on,
            BrokerOperation.DESKTOP_PARSEC_STATUS: self.desktop_parsec_status,
            BrokerOperation.DESKTOP_PARSEC_ENSURE: self.desktop_parsec_ensure,
            BrokerOperation.DESKTOP_PARSEC_RESTART: self.desktop_parsec_restart,
            BrokerOperation.DESKTOP_SUNSHINE_SERVICE_STATUS: self.desktop_sunshine_service_status,
            BrokerOperation.DESKTOP_SUNSHINE_STATUS: self.desktop_sunshine_status,
            BrokerOperation.DESKTOP_SUNSHINE_ENSURE: self.desktop_sunshine_ensure,
            BrokerOperation.DESKTOP_SUNSHINE_SESSION_STATUS: self.desktop_sunshine_session_status,
            BrokerOperation.DESKTOP_SUNSHINE_SESSION_END: self.desktop_sunshine_session_end,
            BrokerOperation.DESKTOP_DISPLAY_STATUS: self.desktop_display_status,
            BrokerOperation.DESKTOP_VIRTUAL_DISPLAY_ACTIVATE: self.desktop_virtual_display_activate,
            BrokerOperation.DESKTOP_VIRTUAL_DISPLAY_DEACTIVATE: self.desktop_virtual_display_deactivate,
            BrokerOperation.DESKTOP_LOCK: self.desktop_lock,
            BrokerOperation.DESKTOP_SLEEP: self.desktop_sleep,
            BrokerOperation.DESKTOP_RESTART: self.desktop_restart,
            BrokerOperation.DESKTOP_SHUTDOWN: self.desktop_shutdown,
            BrokerOperation.HOST_RESTART_BUTTERS: self.restart_butters,
            BrokerOperation.HOST_REBOOT: lambda: self._run(
                ["/usr/bin/systemctl", "reboot"], 5
            ),
            BrokerOperation.HOST_SHUTDOWN: lambda: self._run(
                ["/usr/bin/systemctl", "poweroff"], 5
            ),
        }
        if self.config.nas_mac and self.config.nas_broadcast:
            candidates[BrokerOperation.NAS_WAKE] = self.nas_wake
        if self.config.nas_api_url and self.nas_api_key:
            candidates[BrokerOperation.NAS_SHUTDOWN] = self.nas_shutdown
        if self.config.kr260_host and self.config.kr260_expected_machine_id:
            candidates[BrokerOperation.KR260_STATUS] = self.kr260_status
            candidates[BrokerOperation.KR260_REBOOT] = self.kr260_reboot
        return {
            operation: handler
            for operation, handler in candidates.items()
            if operation in self.config.enabled_operations
        }

    def desktop_wake(self) -> dict[str, object]:
        evidence = self._require_monitors_off_for_wake()
        status = self._run(
            [
                "/usr/bin/wakeonlan",
                "-i",
                self.config.desktop_broadcast,
                self.config.desktop_mac,
            ],
            10,
        )
        return {**status, **evidence}

    def _require_monitors_off_for_wake(self) -> dict[str, object]:
        """Refuse the wake unless both outlets read off right now.

        Every failure raises before wakeonlan runs: no Home Assistant token, a
        failed or malformed read, an outlet in any state but "off" (including
        "unavailable"), or a meter that still reports a draw.
        """

        if not self.home_assistant_token:
            raise BrokerError(
                "home_assistant_unconfigured",
                "Home Assistant authentication is not configured",
            )
        deadline = self.monotonic() + WAKE_PRECONDITION_DEADLINE_SECONDS
        states = self._monitor_states(deadline)
        if any(states.get(entity) != "off" for entity in self.MONITOR_ENTITIES):
            raise BrokerError(
                "monitors_not_off", "both monitor outlets must be off before a wake"
            )
        power = self._monitor_power(deadline)
        if any(
            watts is not None and watts > MONITOR_OFF_POWER_CEILING_W
            for watts in power.values()
        ):
            raise BrokerError(
                "monitor_power_present", "a monitor outlet still meters power"
            )
        return {
            "monitors_verified_off": True,
            "monitor_entities": states,
            "monitor_power_w": power,
        }

    def desktop_monitors_off(self) -> dict[str, object]:
        return self._set_desktop_monitors("off")

    def desktop_monitors_on(self) -> dict[str, object]:
        return self._set_desktop_monitors("on")

    def desktop_primary_monitor_on(self) -> dict[str, object]:
        """Turn on only the primary G27QC outlet; never change the OLED outlet."""

        if not self.home_assistant_token:
            raise BrokerError(
                "home_assistant_unconfigured",
                "Home Assistant authentication is not configured",
            )
        primary = self.PRIMARY_MONITOR_ENTITY
        preserved = self.PRESERVED_MONITOR_ENTITY
        deadline = self.monotonic() + MONITOR_OPERATION_DEADLINE_SECONDS
        # The OLED is only observed. A failed read of it must not stop the
        # primary display from being restored, so it is read separately.
        before = self._monitor_states(deadline, entities=(primary,))
        before.update(self._preserved_state(deadline))
        already = before.get(primary) == "on"
        if not already:
            # The service call names exactly one entity. The OLED is read for
            # the result but is never part of any request body.
            self._home_assistant_request(
                "/api/services/switch/turn_on",
                method="POST",
                body={"entity_id": [primary]},
                deadline=deadline,
            )
            after, _matching = self._await_monitor_convergence(
                "on", deadline, entities=(primary,)
            )
            # Report the OLED as it is now. The primary outcome is already
            # decided, so a failed extra read cannot turn it into a failure.
            after = {**after, **self._preserved_state(deadline)}
        else:
            after = before
        observed = (before.get(preserved), after.get(preserved))
        return {
            "accepted": after.get(primary) == "on",
            "desired_state": "on",
            "already_in_state": already,
            "partial": False,
            "entities": after,
            "scope": "primary",
            # None when either read failed: unknown is not "changed".
            "preserved_entity_unchanged": (
                None if "unknown" in observed else observed[0] == observed[1]
            ),
        }

    def _preserved_state(self, deadline: float) -> dict[str, str]:
        preserved = self.PRESERVED_MONITOR_ENTITY
        try:
            return self._monitor_states(deadline, entities=(preserved,))
        except BrokerError:
            return {preserved: "unknown"}

    def desktop_parsec_status(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.PARSEC_STATUS)

    def desktop_parsec_ensure(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.PARSEC_ENSURE)

    def desktop_parsec_restart(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.PARSEC_RESTART)

    def desktop_sunshine_status(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.SUNSHINE_STATUS)

    def desktop_sunshine_service_status(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.SUNSHINE_SERVICE_STATUS)

    def desktop_sunshine_ensure(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.SUNSHINE_ENSURE)

    def desktop_sunshine_session_status(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.SUNSHINE_SESSION_STATUS)

    def desktop_sunshine_session_end(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.SUNSHINE_SESSION_END)

    def desktop_display_status(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.DISPLAY_STATUS)

    def desktop_virtual_display_activate(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.VIRTUAL_DISPLAY_ACTIVATE)

    def desktop_virtual_display_deactivate(self) -> dict[str, object]:
        return self._desktop_control(
            _DesktopControlOperation.VIRTUAL_DISPLAY_DEACTIVATE
        )

    def desktop_lock(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.LOCK)

    def desktop_sleep(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.SLEEP)

    def desktop_restart(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.RESTART)

    def desktop_shutdown(self) -> dict[str, object]:
        return self._desktop_control(_DesktopControlOperation.SHUTDOWN)

    def _set_desktop_monitors(self, desired_state: str) -> dict[str, object]:
        """Set and verify only the two reviewed monitor outlets through HA."""

        if not self.home_assistant_token:
            raise BrokerError(
                "home_assistant_unconfigured",
                "Home Assistant authentication is not configured",
            )
        deadline = self.monotonic() + MONITOR_OPERATION_DEADLINE_SECONDS
        before = self._monitor_states(deadline)
        if all(before.get(entity) == desired_state for entity in self.MONITOR_ENTITIES):
            return {
                "accepted": True,
                "desired_state": desired_state,
                "already_in_state": True,
                "partial": False,
                "entities": before,
                **self._off_power_evidence(desired_state, deadline),
            }

        self._home_assistant_request(
            f"/api/services/switch/turn_{desired_state}",
            method="POST",
            body={"entity_id": list(self.MONITOR_ENTITIES)},
            deadline=deadline,
        )
        after, matching = self._await_monitor_convergence(desired_state, deadline)
        return {
            "accepted": matching == len(self.MONITOR_ENTITIES),
            "desired_state": desired_state,
            "already_in_state": False,
            "partial": 0 < matching < len(self.MONITOR_ENTITIES),
            "entities": after,
            **self._off_power_evidence(desired_state, deadline),
        }

    def _off_power_evidence(
        self, desired_state: str, deadline: float
    ) -> dict[str, object]:
        # Metering only corroborates "off"; turning on reports no power.
        if desired_state != "off":
            return {}
        return {"power_w": self._monitor_power(deadline)}

    def _monitor_power(self, deadline: float) -> dict[str, float | None]:
        """Best-effort metered draw per outlet; None where it cannot be read.

        Power is supporting evidence, so an unreadable meter never fails the
        operation. Only the two fixed sensors beside the fixed outlets are read.
        """

        power: dict[str, float | None] = {}
        for entity in self.MONITOR_ENTITIES:
            try:
                payload = self._home_assistant_request(
                    f"/api/states/{self.MONITOR_POWER_SENSORS[entity]}",
                    deadline=deadline,
                )
            except BrokerError:
                power[entity] = None
                continue
            power[entity] = _metered_watts(payload)
        return power

    def _await_monitor_convergence(
        self,
        desired_state: str,
        operation_deadline: float,
        *,
        entities: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, str], int]:
        """Poll the two fixed outlets until both settle, or the deadline expires.

        Returns the last observed states and how many matched, so the caller can
        still distinguish full success from a partial settle. Read failures are
        deliberately not swallowed: a Home Assistant auth, availability, or
        malformed-response error raises out of here so the operation fails closed
        instead of being reported as a benign timeout. A state the outlet never
        reaches -- including "unavailable" or a missing entity, which
        _monitor_states records as "unknown" -- simply never matches, so it ends
        as a bounded partial/failed result rather than blocking.
        """

        watched = self.MONITOR_ENTITIES if entities is None else entities
        deadline = min(
            self.monotonic() + MONITOR_SETTLE_DEADLINE_SECONDS, operation_deadline
        )
        while True:
            states = self._monitor_states(deadline, entities=watched)
            matching = sum(states.get(entity) == desired_state for entity in watched)
            if matching == len(watched):
                return states, matching
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return states, matching
            # Never overshoot the deadline just to complete a whole interval.
            self.sleeper(min(MONITOR_SETTLE_POLL_SECONDS, remaining))
            if self.monotonic() >= deadline:
                # Sleeping landed exactly on the deadline. Starting another read
                # here would leave it no budget, turning a bounded partial
                # result into an unavailable error.
                return states, matching

    def _monitor_states(
        self,
        deadline: float | None = None,
        *,
        entities: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        states: dict[str, str] = {}
        for entity in self.MONITOR_ENTITIES if entities is None else entities:
            payload = self._home_assistant_request(
                f"/api/states/{entity}", deadline=deadline
            )
            state = payload.get("state") if isinstance(payload, dict) else None
            states[entity] = str(state) if state is not None else "unknown"
        return states

    def _home_assistant_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
        deadline: float | None = None,
    ) -> object:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.config.home_assistant_url.rstrip("/") + path,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.home_assistant_token}",
                **({"Content-Type": "application/json"} if encoded is not None else {}),
            },
        )
        budget = self.home_assistant_deadline_seconds
        if deadline is not None:
            # Never let one exchange spend more than the operation has left.
            budget = min(budget, deadline - self.monotonic())
            if budget <= 0:
                raise BrokerError(
                    "home_assistant_unavailable", "Home Assistant is unavailable"
                )
        read_deadline = self.monotonic() + budget

        def exchange() -> bytes:
            with self.opener(request, timeout=5.0) as response:
                if getattr(response, "status", 200) not in {200, 201}:
                    raise BrokerError(
                        "home_assistant_failed", "Home Assistant request failed"
                    )
                return _read_bounded(response, read_deadline, self.monotonic)

        try:
            raw = self._bounded_exchange(exchange, budget)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise BrokerError(
                    "home_assistant_auth_failed",
                    "Home Assistant authentication was denied",
                ) from None
            raise BrokerError(
                "home_assistant_failed", "Home Assistant request failed"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise BrokerError(
                "home_assistant_unavailable", "Home Assistant is unavailable"
            ) from None
        if len(raw) > HOME_ASSISTANT_MAX_RESPONSE_BYTES:
            raise BrokerError(
                "home_assistant_failed", "Home Assistant response exceeded its limit"
            )
        try:
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BrokerError(
                "home_assistant_failed", "Home Assistant returned malformed JSON"
            ) from None

    def _bounded_exchange(self, exchange: Callable[[], bytes], budget: float) -> bytes:
        """Give the whole Home Assistant exchange one real wall-clock bound.

        urlopen's ``timeout`` is per socket operation, not a deadline. A peer
        that drips one byte of the status line or headers just inside every
        window holds the call open for as long as it likes -- measured at 106
        seconds for a 2s-per-byte drip against a 5s timeout, and unbounded if it
        never stops. _read_bounded only covers the body, which is reached after
        that phase, so it cannot help here. The broker answers connections
        serially, so one such call blocks every other privileged operation,
        including host.reboot, on a unit with Restart=no and no watchdog.

        Python's blocking socket API offers no absolute deadline, so the
        exchange runs on one long-lived worker and is abandoned rather than
        waited on when the bound expires. The guarantee is that the *broker*
        returns, not that the socket closed. A still-stuck exchange makes every
        later Home Assistant request fail immediately instead of queueing behind
        it, which both caps abandoned work at one thread and keeps the wake,
        SSH and host operations responsive while Home Assistant misbehaves.

        The bound is deliberately real time, not self.monotonic: the injected
        clock exists so the settling tests can advance a 20s deadline instantly,
        and supervising a thread with a clock that jumps would be meaningless.
        """

        with self._exchange_lock:
            pending = self._exchange_pending
            if pending is not None and not pending.done():
                raise BrokerError(
                    "home_assistant_unavailable", "Home Assistant is unavailable"
                )
            if self._exchange_worker is None:
                self._exchange_worker = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="butters-ha"
                )
            future = self._exchange_worker.submit(exchange)
            self._exchange_pending = future
        try:
            return future.result(timeout=max(0.0, budget))
        except FutureTimeout:
            raise BrokerError(
                "home_assistant_unavailable", "Home Assistant is unavailable"
            ) from None

    def nas_wake(self) -> dict[str, object]:
        return self._run(
            [
                "/usr/bin/wakeonlan",
                "-i",
                self.config.nas_broadcast,
                self.config.nas_mac,
            ],
            10,
        )

    def nas_shutdown(self) -> dict[str, object]:
        """Power the one configured NAS off through its own middleware API.

        This is deliberately not an SSH session. The TrueNAS middleware exposes
        ``system.shutdown`` as a fixed endpoint that takes no argument the
        broker needs to supply, so there is no shell, no argv, no remote command
        string, and nothing a caller could influence even if the socket request
        carried a field -- which it does not. The URL and the credential are
        root-owned configuration loaded at start-up.
        """

        if not self.config.nas_api_url or not self.nas_api_key:
            raise BrokerError(
                "nas_api_unconfigured", "NAS shutdown transport is not configured"
            )
        payload = self._nas_api_request(NAS_SHUTDOWN_PATH)
        return {
            "accepted": True,
            "transport": "truenas_api",
            # TrueNAS answers with a job identifier or null; never echo the body.
            "job_accepted": payload is not None,
        }

    def _nas_api_request(self, path: str) -> object:
        request = Request(
            self.config.nas_api_url.rstrip("/") + path,
            data=b"{}",
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.nas_api_key}",
            },
        )
        budget = NAS_API_DEADLINE_SECONDS
        read_deadline = self.monotonic() + budget

        def exchange() -> bytes:
            with self.opener(request, timeout=5.0) as response:
                if getattr(response, "status", 200) not in {200, 201, 202, 204}:
                    raise BrokerError("nas_api_failed", "NAS shutdown was refused")
                return _read_bounded(response, read_deadline, self.monotonic)

        try:
            raw = self._bounded_exchange(exchange, budget)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise BrokerError(
                    "nas_api_auth_failed", "NAS shutdown credential was rejected"
                ) from None
            raise BrokerError("nas_api_failed", "NAS shutdown was refused") from None
        except (URLError, TimeoutError, OSError):
            raise BrokerError("nas_api_unavailable", "NAS API is unavailable") from None
        if len(raw) > HOME_ASSISTANT_MAX_RESPONSE_BYTES:
            raise BrokerError("nas_api_failed", "NAS response exceeded its limit")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BrokerError("nas_api_failed", "NAS returned malformed JSON") from None

    def restart_butters(self) -> dict[str, object]:
        unit = "butters-web-restart-" + secrets.token_hex(6)
        return self._run(
            [
                "/usr/bin/systemd-run",
                "--quiet",
                f"--unit={unit}",
                "--on-active=2s",
                "/usr/bin/systemctl",
                "restart",
                "butters-web.service",
            ],
            15,
        )

    def kr260_reboot(self) -> dict[str, object]:
        """Identity-check then request one kexec restart over pinned SSH.

        Success means the wrapper staged the running kernel and systemd accepted
        the kexec, not that the board came back; the adapter confirms that from
        a changed boot ID.
        """

        identity = self._kr260_ssh("/usr/bin/cat /etc/machine-id", capture=True)
        if identity.strip() != self.config.kr260_expected_machine_id:
            raise BrokerError("identity_mismatch", "KR260 identity did not match")
        # The board grants this exact argv, with no arguments, through a
        # NOPASSWD sudoers rule (config/kr260/butters-kr260-reboot.sudoers);
        # -n makes an unprovisioned board fail at once as reboot_unauthorized
        # rather than prompt.
        self._kr260_ssh(
            _KR260_REBOOT_COMMAND, capture=False, refusals=_KR260_RESTART_REFUSALS
        )
        return {
            "accepted": True,
            "identity_verified": True,
            "restart_method": "kexec",
            "restart_confirmed": False,
        }

    def kr260_status(self) -> dict[str, object]:
        telemetry = self._kr260_ssh(_KR260_TELEMETRY_COMMAND, capture=True)
        if len(telemetry.encode("utf-8")) > 16384:
            raise BrokerError("malformed_result", "KR260 telemetry exceeded its limit")
        identities = [
            line.partition("=")[2].strip()
            for line in telemetry.splitlines()
            if line.partition("=")[:2] == ("machine_id", "=")
        ]
        if identities != [self.config.kr260_expected_machine_id]:
            raise BrokerError("identity_mismatch", "KR260 identity did not match")
        return {
            "accepted": True,
            "identity_verified": True,
            "telemetry": telemetry,
        }

    def _kr260_ssh(
        self,
        command: str,
        *,
        capture: bool,
        refusals: Mapping[int, str] | None = None,
    ) -> str:
        """Run one fixed command; raise a fixed code that names the failure.

        stderr is read only to choose among the fixed codes below. It is never
        returned, logged, or compared beyond those literal markers.
        """

        # command is selected only from fixed literals in this module. No request or
        # configuration value can become remote shell text.
        argv = [
            "/usr/bin/ssh",
            "-T",
            "-i",
            str(self.config.kr260_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            f"UserKnownHostsFile={self.config.kr260_known_hosts}",
            # Fail an unreachable board inside the 10s bound, as a classified
            # connect error instead of an anonymous subprocess timeout.
            "-o",
            f"ConnectTimeout={KR260_CONNECT_TIMEOUT_SECONDS}",
            f"{self.config.kr260_user}@{self.config.kr260_host}",
            command,
        ]
        try:
            result = self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrokerError("operation_timeout", "fixed operation timed out") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrokerError("operation_failed", "fixed operation failed") from exc
        returncode = int(getattr(result, "returncode", 1))
        if returncode != 0:
            code = _kr260_failure_code(
                returncode,
                str(getattr(result, "stderr", "") or "")[:4096],
                refusals or {},
            )
            raise BrokerError(code, "fixed KR260 operation failed")
        return str(getattr(result, "stdout", "")) if capture else ""

    def _desktop_control(
        self, operation: _DesktopControlOperation
    ) -> dict[str, object]:
        """Invoke one fixed Windows helper operation and validate its safe result.

        ``operation`` is an internal enum selected only by the handler table.
        The socket request has no argument field, and no caller value can reach
        the host, account, path, executable, service, task, or command line.
        """

        command = (
            "powershell.exe -NoLogo -NoProfile -NonInteractive "
            "-ExecutionPolicy Bypass "
            # The Windows OpenSSH account uses Git Bash as its default shell.
            # Forward slashes keep Git Bash from consuming the backslashes
            # before PowerShell receives this fixed Windows path.
            "-File C:/ProgramData/Butters/desktop-control.ps1 "
            f"-Operation {operation.value}"
        )
        argv = [
            "/usr/bin/ssh",
            # No PTY, no forwarding, one credential, and a pinned host key.
            # The unit runs with ProtectHome, so an implicit ~/.ssh lookup
            # would silently have no known_hosts to consult at all.
            "-T",
            "-i",
            str(self.config.desktop_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            f"UserKnownHostsFile={self.config.desktop_known_hosts}",
            f"{self.config.desktop_user}@{self.config.desktop_host}",
            command,
        ]
        try:
            result = self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrokerError("operation_failed", "fixed operation failed") from exc
        if int(getattr(result, "returncode", 1)) != 0:
            raise BrokerError("operation_failed", "fixed operation failed")
        raw = str(getattr(result, "stdout", ""))
        if len(raw.encode("utf-8")) > 16384:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise BrokerError(
                "malformed_result", "fixed operation result is invalid"
            ) from exc
        status = _desktop_control_result(operation, payload)
        if status.get("accepted") is False:
            code = status.get("error_code")
            allowed = {
                "parsec_not_installed",
                "parsec_start_timeout",
                "parsec_restart_timeout",
                "parsec_authentication_required",
                "sunshine_not_installed",
                "sunshine_start_timeout",
                "sunshine_capture_unavailable",
                "sunshine_api_unconfigured",
                "sunshine_api_unavailable",
                "sunshine_api_auth_failed",
                "sunshine_session_end_timeout",
                "sunshine_session_present",
                "virtual_display_unavailable",
                "virtual_display_identity_mismatch",
                "virtual_display_mode_unverified",
                "physical_display_unavailable",
            }
            raise BrokerError(
                str(code) if code in allowed else "operation_failed",
                "fixed operation failed",
            )
        return status

    def _run(self, argv: list[str], timeout: float) -> dict[str, object]:
        try:
            result = self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrokerError("operation_failed", "fixed operation failed") from exc
        if int(getattr(result, "returncode", 1)) != 0:
            raise BrokerError("operation_failed", "fixed operation failed")
        return {"accepted": True}


def _kr260_failure_code(
    returncode: int, stderr: str, refusals: Mapping[int, str]
) -> str:
    """Map an ssh failure to one fixed code. ssh itself exits 255."""

    if returncode == 255:
        if (
            "Host key verification failed" in stderr
            or "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr
        ):
            return "host_key_mismatch"
        if "Permission denied" in stderr:
            return "ssh_auth_failed"
        if any(
            marker in stderr
            for marker in (
                "No route to host",
                "Connection timed out",
                "Connection refused",
                "Network is unreachable",
                "Could not resolve hostname",
            )
        ):
            return "host_unreachable"
        return "ssh_failed"
    if "a password is required" in stderr or "Interactive authentication" in stderr:
        return "reboot_unauthorized"
    return refusals.get(returncode, "operation_failed")


def _metered_watts(payload: object) -> float | None:
    """A Home Assistant power state in watts, or None if it is not one."""

    if not isinstance(payload, dict):
        return None
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict) or attributes.get("unit_of_measurement") != "W":
        return None
    try:
        watts = float(str(payload.get("state")))
    except ValueError:
        return None
    return watts if math.isfinite(watts) and watts >= 0 else None


_PARSEC_STATE_KEYS = frozenset(
    {
        "installed",
        "installation_type",
        "service_present",
        "service_running",
        "service_startup",
        "service_process_present",
        "host_process_present",
        "system_host_process_present",
        "user_host_process_present",
        "authentication_error",
        "plausibly_ready",
    }
)

_SUNSHINE_STATE_KEYS = frozenset(
    {
        "installed",
        "service_present",
        "service_running",
        "service_startup",
        "host_process_present",
        "web_listener_present",
        "stream_listeners_present",
        "capture_ready",
        "virtual_display_ready",
        "plausibly_ready",
    }
)

_SUNSHINE_SESSION_STATES = frozenset(
    {"terminated", "resumable", "active", "ending", "unknown"}
)

_SUNSHINE_SESSION_KEYS = frozenset(
    {"api_reachable", "app_running", "stream_active", "session_state"}
)

# The removal result must carry the session state the helper re-checked
# immediately before acting, so the broker can enforce that re-check.
_DEACTIVATION_EVIDENCE_KEYS = frozenset({"session_state"})

_DISPLAY_STATE_KEYS = frozenset(
    {
        "virtual_display_active",
        "virtual_mode_verified",
        "primary_physical_active",
        "secondary_physical_active",
        "physical_display_active",
    }
)

_SUNSHINE_SERVICE_STATE_KEYS = frozenset(
    {
        "installed",
        "service_present",
        "service_running",
        "service_startup",
        "host_process_present",
        "web_listener_present",
        "stream_listeners_present",
        "service_ready",
    }
)


def _desktop_control_result(
    operation: _DesktopControlOperation, payload: object
) -> dict[str, object]:
    """Validate the helper's bounded public schema before crossing the broker."""

    if not isinstance(payload, dict) or not all(
        isinstance(key, str) for key in payload
    ):
        raise BrokerError("malformed_result", "fixed operation result is invalid")
    if operation in {
        _DesktopControlOperation.PARSEC_STATUS,
        _DesktopControlOperation.PARSEC_ENSURE,
        _DesktopControlOperation.PARSEC_RESTART,
    }:
        extras = (
            frozenset()
            if operation is _DesktopControlOperation.PARSEC_STATUS
            else frozenset({"accepted", "error_code", "elapsed_ms"})
            | (
                frozenset({"already_running"})
                if operation is _DesktopControlOperation.PARSEC_ENSURE
                else frozenset()
            )
        )
        if set(payload) != _PARSEC_STATE_KEYS | extras:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        for key in _PARSEC_STATE_KEYS - {"installation_type", "service_startup"}:
            if not isinstance(payload[key], bool):
                raise BrokerError(
                    "malformed_result", "fixed operation result is invalid"
                )
        if payload["installation_type"] not in {"machine", "absent"}:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        if payload["service_startup"] not in {
            "auto",
            "manual",
            "disabled",
            "absent",
        }:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        if extras:
            if not isinstance(payload["accepted"], bool) or not isinstance(
                payload["elapsed_ms"], int
            ):
                raise BrokerError(
                    "malformed_result", "fixed operation result is invalid"
                )
            error = payload["error_code"]
            if error is not None and not isinstance(error, str):
                raise BrokerError(
                    "malformed_result", "fixed operation result is invalid"
                )
        if "already_running" in extras and not isinstance(
            payload["already_running"], bool
        ):
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        return dict(payload)

    if operation is _DesktopControlOperation.SUNSHINE_SERVICE_STATUS:
        if set(payload) != _SUNSHINE_SERVICE_STATE_KEYS:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        for key in _SUNSHINE_SERVICE_STATE_KEYS - {"service_startup"}:
            if not isinstance(payload[key], bool):
                raise BrokerError("malformed_result", "fixed operation result is invalid")
        if payload["service_startup"] not in {"auto", "manual", "disabled", "absent"}:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        return dict(payload)

    if operation in {
        _DesktopControlOperation.SUNSHINE_STATUS,
        _DesktopControlOperation.SUNSHINE_ENSURE,
    }:
        extras = (
            frozenset()
            if operation is _DesktopControlOperation.SUNSHINE_STATUS
            else frozenset({"accepted", "error_code", "elapsed_ms", "already_running"})
        )
        if set(payload) != _SUNSHINE_STATE_KEYS | extras:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        for key in _SUNSHINE_STATE_KEYS - {"service_startup"}:
            if not isinstance(payload[key], bool):
                raise BrokerError(
                    "malformed_result", "fixed operation result is invalid"
                )
        if payload["service_startup"] not in {
            "auto",
            "manual",
            "disabled",
            "absent",
        }:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        if extras:
            if (
                not isinstance(payload["accepted"], bool)
                or not isinstance(payload["already_running"], bool)
                or not isinstance(payload["elapsed_ms"], int)
            ):
                raise BrokerError(
                    "malformed_result", "fixed operation result is invalid"
                )
            error = payload["error_code"]
            if error is not None and not isinstance(error, str):
                raise BrokerError(
                    "malformed_result", "fixed operation result is invalid"
                )
        return dict(payload)

    if operation in {
        _DesktopControlOperation.SUNSHINE_SESSION_STATUS,
        _DesktopControlOperation.SUNSHINE_SESSION_END,
    }:
        extras = (
            frozenset()
            if operation is _DesktopControlOperation.SUNSHINE_SESSION_STATUS
            else frozenset(
                {"accepted", "error_code", "elapsed_ms", "already_terminated"}
            )
        )
        if set(payload) != _SUNSHINE_SESSION_KEYS | extras:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        _require_session_consistency(payload)
        if extras:
            _require_mutation_fields(payload, "already_terminated")
            terminated = payload["session_state"] == "terminated"
            if (payload["accepted"] or payload["already_terminated"]) and not terminated:
                raise BrokerError("malformed_result", "fixed operation result is invalid")
        return dict(payload)

    if operation in {
        _DesktopControlOperation.DISPLAY_STATUS,
        _DesktopControlOperation.VIRTUAL_DISPLAY_ACTIVATE,
        _DesktopControlOperation.VIRTUAL_DISPLAY_DEACTIVATE,
    }:
        flag = {
            _DesktopControlOperation.DISPLAY_STATUS: None,
            _DesktopControlOperation.VIRTUAL_DISPLAY_ACTIVATE: "already_active",
            _DesktopControlOperation.VIRTUAL_DISPLAY_DEACTIVATE: "already_inactive",
        }[operation]
        extras = (
            frozenset()
            if flag is None
            else frozenset({"accepted", "error_code", "elapsed_ms", flag})
        )
        if operation is _DesktopControlOperation.VIRTUAL_DISPLAY_DEACTIVATE:
            extras |= _DEACTIVATION_EVIDENCE_KEYS
        if set(payload) != _DISPLAY_STATE_KEYS | extras:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        if not all(isinstance(payload[key], bool) for key in _DISPLAY_STATE_KEYS):
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        physical = payload["primary_physical_active"] or payload[
            "secondary_physical_active"
        ]
        if payload["physical_display_active"] is not physical or (
            payload["virtual_mode_verified"] and not payload["virtual_display_active"]
        ):
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        if flag is not None:
            _require_mutation_fields(payload, flag)
            _require_display_outcome(operation, payload, flag)
        return dict(payload)

    expected_transition = {
        _DesktopControlOperation.LOCK: "lock",
        _DesktopControlOperation.SLEEP: "sleep",
        _DesktopControlOperation.RESTART: "restart",
        _DesktopControlOperation.SHUTDOWN: "shutdown",
    }[operation]
    if set(payload) != {"accepted", "transition", "scheduled"} or (
        payload.get("accepted") is not True
        or payload.get("scheduled") is not True
        or payload.get("transition") != expected_transition
    ):
        raise BrokerError("malformed_result", "fixed operation result is invalid")
    return dict(payload)


def _require_session_consistency(payload: dict[str, object]) -> None:
    """Reject a Sunshine session report whose fields contradict its state.

    ``terminated`` is the only state any caller treats as permission to remove
    the virtual display, so it must be internally unambiguous: the API was
    reached, no application is running, and no stream socket is bound.
    """

    state = payload["session_state"]
    app_running = payload["app_running"]
    stream_active = payload["stream_active"]
    if (
        state not in _SUNSHINE_SESSION_STATES
        or not isinstance(payload["api_reachable"], bool)
        or not (app_running is None or isinstance(app_running, bool))
        or not (stream_active is None or isinstance(stream_active, bool))
    ):
        raise BrokerError("malformed_result", "fixed operation result is invalid")
    expected = {
        "terminated": (True, False, False),
        "resumable": (True, True, False),
        "active": (True, True, True),
        "ending": (True, False, True),
    }.get(str(state))
    if expected is None:
        # Unknown never carries an observation that could be mistaken for one.
        if app_running is not None or stream_active is not None:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
        return
    if (payload["api_reachable"], app_running, stream_active) != expected:
        raise BrokerError("malformed_result", "fixed operation result is invalid")


def _require_display_outcome(
    operation: _DesktopControlOperation, payload: dict[str, object], flag: str
) -> None:
    """``accepted`` and the idempotence flag must agree with the observation."""

    if operation is _DesktopControlOperation.VIRTUAL_DISPLAY_ACTIVATE:
        achieved = (
            payload["virtual_display_active"] is True
            and payload["virtual_mode_verified"] is True
        )
    else:
        achieved = (
            payload["virtual_display_active"] is False
            and payload["physical_display_active"] is True
            and payload["session_state"] == "terminated"
        )
        if payload["session_state"] not in _SUNSHINE_SESSION_STATES:
            raise BrokerError("malformed_result", "fixed operation result is invalid")
    if (payload["accepted"] or payload[flag]) and not achieved:
        raise BrokerError("malformed_result", "fixed operation result is invalid")


def _require_mutation_fields(payload: dict[str, object], flag: str) -> None:
    error = payload["error_code"]
    if payload["accepted"] is True and error is not None:
        raise BrokerError("malformed_result", "fixed operation result is invalid")
    if (
        not isinstance(payload["accepted"], bool)
        or not isinstance(payload[flag], bool)
        or not isinstance(payload["elapsed_ms"], int)
        or isinstance(payload["elapsed_ms"], bool)
        or not (error is None or isinstance(error, str))
    ):
        raise BrokerError("malformed_result", "fixed operation result is invalid")


def _read_bounded(response: Any, deadline: float, clock: Callable[[], float]) -> bytes:
    """Read a bounded body under a wall-clock deadline.

    Each chunk is still covered by the socket timeout; the deadline is what
    stops an arbitrarily long sequence of individually-timely chunks. One byte
    over the cap is read deliberately so the caller can still distinguish an
    oversized body from an exactly-sized one.
    """

    limit = HOME_ASSISTANT_MAX_RESPONSE_BYTES + 1
    raw = bytearray()
    while len(raw) < limit:
        if clock() > deadline:
            raise BrokerError(
                "home_assistant_unavailable", "Home Assistant is unavailable"
            )
        chunk = response.read(min(HOME_ASSISTANT_READ_CHUNK_BYTES, limit - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
    return bytes(raw)


def _parse_request(raw: bytes, version: int) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerError("malformed_request", "broker request is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "version",
        "request_id",
        "operation",
    }:
        raise BrokerError("malformed_request", "broker request schema is invalid")
    if value["version"] != version:
        raise BrokerError("protocol_mismatch", "broker protocol version is invalid")
    if not isinstance(value["request_id"], str) or not _identifier(value["request_id"]):
        raise BrokerError("invalid_request_id", "broker request ID is invalid")
    if not isinstance(value["operation"], str):
        raise BrokerError("unknown_operation", "broker operation is invalid")
    return value


def _peer_credentials(connection: socket.socket) -> tuple[int, int]:
    """Return the connected peer's (pid, uid) from SO_PEERCRED."""

    if not hasattr(socket, "SO_PEERCRED"):
        raise BrokerError(
            "peer_credentials_unavailable", "peer credentials are unavailable"
        )
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid, uid, _gid = struct.unpack("3i", raw)
    return pid, uid


def _read_line(
    connection: socket.socket,
    maximum: int,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    chunks = bytearray()
    while len(chunks) <= maximum:
        if deadline is not None:
            remaining = deadline - clock()
            if remaining <= 0:
                raise BrokerError("request_timeout", "broker request timed out")
            # Re-armed per recv against one shared deadline so a peer that drips
            # a byte at a time cannot extend the bound indefinitely.
            connection.settimeout(remaining)
        block = connection.recv(min(4096, maximum + 1 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        newline = chunks.find(b"\n")
        if newline >= 0:
            if newline != len(chunks) - 1:
                raise BrokerError(
                    "malformed_message", "broker message has trailing data"
                )
            return bytes(chunks[:newline])
    if len(chunks) > maximum:
        raise BrokerError("message_too_large", "broker message exceeds its limit")
    raise BrokerError("malformed_message", "broker message is incomplete")


def _response(
    version: int,
    request_id: str,
    ok: bool,
    status: dict[str, object],
    error_code: str | None,
) -> dict[str, object]:
    return {
        "version": version,
        "request_id": request_id,
        "ok": ok,
        "status": status,
        "error_code": error_code,
    }


def _encoded(response: dict[str, object]) -> bytes:
    return json.dumps(response, separators=(",", ":")).encode() + b"\n"


def _render(response: dict[str, object]) -> bytes:
    """Encode a decided response, never raising past the privilege boundary.

    Encoding sits after handle()'s except clauses, so a status a future handler
    made unserializable would escape as an unhandled exception: the operation
    would have already run, the peer would get nothing, and no audit line would
    be written. Substitute an empty status instead and preserve ``ok``, because
    reporting a mutation that succeeded as a failure invites the caller to
    retry it.
    """

    try:
        return _encoded(response)
    except (TypeError, ValueError):
        response["status"] = {}
        if response["ok"] is False and response["error_code"] is None:
            response["error_code"] = "malformed_result"
        return _encoded(response)


def _identifier(value: str) -> bool:
    return 16 <= len(value) <= 128 and all(
        character.isalnum() or character in "-_" for character in value
    )


def _identifier_tag(value: str) -> str:
    """A request ID reaches the journal only after passing its own validator.

    The ID is a single-use correlation nonce that has already been retired by
    the replay table when this runs, so recording the bounded, charset-checked
    value links the broker event to the web-side audit without logging a
    reusable capability.
    """

    return value if _identifier(value) else "-"


def _enumerated_operation(value: str) -> str:
    """Only an enumerated operation name is logged, never a peer-supplied string."""

    return value if value in _OPERATION_VALUES else "-"


def _fixed_code(value: str) -> str:
    """Result codes are broker-local literals; recheck the charset regardless."""

    return value if _CODE_PATTERN.match(value) else "unknown"


_OPERATION_VALUES = frozenset(item.value for item in BrokerOperation)
