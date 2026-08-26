"""Strict enumerated Unix-socket protocol for privileged fixed operations."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import socket
import struct
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from butters.assistant_config import BrokerSettings

LOGGER = logging.getLogger(__name__)

# The broker answers connections serially, so peer I/O has to be bounded. The
# client spends at most 2s connecting and waits 30s for a reply, and a request
# is a single small write issued immediately after connect(); 5s is generous for
# a healthy peer while keeping a stalled one from holding the boundary open.
DEFAULT_PEER_TIMEOUT_SECONDS = 5.0

# Only broker-local literals are ever logged, never peer-supplied text.
_CODE_PATTERN = re.compile(r"\A[a-z][a-z_]{0,47}\Z")


class BrokerOperation(str, Enum):
    DESKTOP_WAKE = "desktop.wake"
    DESKTOP_MONITORS_OFF = "desktop.monitors_off"
    DESKTOP_MONITORS_ON = "desktop.monitors_on"
    DESKTOP_LOCK = "desktop.lock"
    DESKTOP_SLEEP = "desktop.sleep"
    DESKTOP_RESTART = "desktop.restart"
    DESKTOP_SHUTDOWN = "desktop.shutdown"
    HOST_RESTART_BUTTERS = "host.restart_butters"
    HOST_REBOOT = "host.reboot"
    HOST_SHUTDOWN = "host.shutdown"
    NAS_WAKE = "nas.wake"
    HEATER_ON = "environment.heater_on"
    HEATER_OFF = "environment.heater_off"
    DEHUMIDIFIER_ON = "environment.dehumidifier_on"
    DEHUMIDIFIER_OFF = "environment.dehumidifier_off"
    VENTILATION_ON = "environment.ventilation_on"
    VENTILATION_OFF = "environment.ventilation_off"


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
    ) -> None:
        self.settings = settings
        self.connector = connector

    def request(
        self,
        operation: BrokerOperation,
        *,
        request_id: str,
        cancel_event: threading.Event | None = None,
    ) -> BrokerResult:
        if not self.settings.enabled:
            raise BrokerError("broker_unconfigured", "action broker is not enabled")
        if not _identifier(request_id):
            raise BrokerError("invalid_request_id", "broker request ID is invalid")
        if cancel_event is not None and cancel_event.is_set():
            raise BrokerError("cancelled", "action was cancelled")
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
            connection.settimeout(self.settings.connect_timeout_seconds)
            connection.connect(str(self.settings.socket_path))
            connection.settimeout(self.settings.request_timeout_seconds)
            connection.sendall(encoded)
            raw = _read_line(connection, self.settings.max_message_bytes)
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
        encoded = _encoded(response)
        if len(encoded) > self.max_message_bytes:
            response = _response(
                self.protocol_version, request_id, False, {}, "result_too_large"
            )
            encoded = _encoded(response)
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
    enabled_operations: frozenset[BrokerOperation] = frozenset()
    home_assistant_url: str = "http://127.0.0.1:8123"

    @property
    def desktop_known_hosts(self) -> Path:
        """Pinned host key beside the credential in the same root-owned directory.

        The desktop credential can power off a machine, so the broker must not
        fall back to trust-on-first-use. Deriving the path keeps the pin and the
        key provisioned together rather than adding a separately forgettable
        configuration field.
        """

        return self.desktop_key.parent / "known_hosts"


class FixedBrokerOperations:
    """Fixed reviewed argv templates; no caller-controlled field reaches argv."""

    MONITOR_ENTITIES = (
        "switch.desktop_gigabyte",
        "switch.desktop_oled",
    )

    def __init__(
        self,
        config: FixedBrokerConfig,
        *,
        runner: Callable[..., Any] = subprocess.run,
        home_assistant_token: str = "",
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.config = config
        self.runner = runner
        self.home_assistant_token = home_assistant_token.strip()
        self.opener = opener

    def handlers(self) -> dict[BrokerOperation, Callable[[], dict[str, object]]]:
        candidates = {
            BrokerOperation.DESKTOP_WAKE: self.desktop_wake,
            BrokerOperation.DESKTOP_MONITORS_OFF: self.desktop_monitors_off,
            BrokerOperation.DESKTOP_MONITORS_ON: self.desktop_monitors_on,
            BrokerOperation.DESKTOP_LOCK: lambda: self._desktop_fixed(
                "rundll32.exe user32.dll,LockWorkStation"
            ),
            BrokerOperation.DESKTOP_SLEEP: lambda: self._desktop_fixed(
                "shutdown.exe /h"
            ),
            BrokerOperation.DESKTOP_RESTART: lambda: self._desktop_fixed(
                "shutdown.exe /r /t 0"
            ),
            BrokerOperation.DESKTOP_SHUTDOWN: lambda: self._desktop_fixed(
                "shutdown.exe /s /t 0"
            ),
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
        return {
            operation: handler
            for operation, handler in candidates.items()
            if operation in self.config.enabled_operations
        }

    def desktop_wake(self) -> dict[str, object]:
        return self._run(
            [
                "/usr/bin/wakeonlan",
                "-i",
                self.config.desktop_broadcast,
                self.config.desktop_mac,
            ],
            10,
        )

    def desktop_monitors_off(self) -> dict[str, object]:
        return self._set_desktop_monitors("off")

    def desktop_monitors_on(self) -> dict[str, object]:
        return self._set_desktop_monitors("on")

    def _set_desktop_monitors(self, desired_state: str) -> dict[str, object]:
        """Set and verify only the two reviewed monitor outlets through HA."""

        if not self.home_assistant_token:
            raise BrokerError(
                "home_assistant_unconfigured",
                "Home Assistant authentication is not configured",
            )
        before = self._monitor_states()
        if all(before.get(entity) == desired_state for entity in self.MONITOR_ENTITIES):
            return {
                "accepted": True,
                "desired_state": desired_state,
                "already_in_state": True,
                "partial": False,
                "entities": before,
            }

        self._home_assistant_request(
            f"/api/services/switch/turn_{desired_state}",
            method="POST",
            body={"entity_id": list(self.MONITOR_ENTITIES)},
        )
        after = self._monitor_states()
        matching = sum(
            after.get(entity) == desired_state for entity in self.MONITOR_ENTITIES
        )
        return {
            "accepted": matching == len(self.MONITOR_ENTITIES),
            "desired_state": desired_state,
            "already_in_state": False,
            "partial": 0 < matching < len(self.MONITOR_ENTITIES),
            "entities": after,
        }

    def _monitor_states(self) -> dict[str, str]:
        states: dict[str, str] = {}
        for entity in self.MONITOR_ENTITIES:
            payload = self._home_assistant_request(f"/api/states/{entity}")
            state = payload.get("state") if isinstance(payload, dict) else None
            states[entity] = str(state) if state is not None else "unknown"
        return states

    def _home_assistant_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
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
        try:
            with self.opener(request, timeout=5.0) as response:
                if getattr(response, "status", 200) not in {200, 201}:
                    raise BrokerError(
                        "home_assistant_failed", "Home Assistant request failed"
                    )
                raw = response.read(65537)
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
        if len(raw) > 65536:
            raise BrokerError(
                "home_assistant_failed", "Home Assistant response exceeded its limit"
            )
        try:
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BrokerError(
                "home_assistant_failed", "Home Assistant returned malformed JSON"
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

    def _desktop_fixed(self, command: str) -> dict[str, object]:
        return self._run(
            [
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
                # The pin is a root-owned provisioned file, so the client must
                # never learn or write additional host keys into it. OpenSSH
                # already implies this when UserKnownHostsFile is overridden;
                # stating it keeps the pin immune to a default change.
                "-o",
                "UpdateHostKeys=no",
                "-o",
                f"UserKnownHostsFile={self.config.desktop_known_hosts}",
                f"{self.config.desktop_user}@{self.config.desktop_host}",
                command,
            ],
            20,
        )

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
    connection: socket.socket, maximum: int, *, deadline: float | None = None
) -> bytes:
    chunks = bytearray()
    while len(chunks) <= maximum:
        if deadline is not None:
            remaining = deadline - time.monotonic()
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
