"""Strict enumerated Unix-socket protocol for privileged fixed operations."""

from __future__ import annotations

import json
import secrets
import socket
import struct
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from butters.assistant_config import BrokerSettings


class BrokerOperation(str, Enum):
    DESKTOP_WAKE = "desktop.wake"
    DESKTOP_ENTER_REMOTE = "desktop.enter_remote"
    DESKTOP_RESTORE_LOCAL = "desktop.restore_local"
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
    ) -> None:
        self.operations = dict(operations)
        self.expected_uid = expected_uid
        self.protocol_version = protocol_version
        self.max_message_bytes = max_message_bytes
        self.dedupe_capacity = dedupe_capacity
        # Ordered so eviction drops the oldest request ID. A plain set evicts an
        # arbitrary member, which can retire a just-seen ID and reopen its replay
        # window while much older IDs are retained.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def handle(self, connection: socket.socket) -> None:
        request_id = "invalid"
        try:
            self._require_peer(connection)
            raw = _read_line(connection, self.max_message_bytes)
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
            handler = self.operations.get(operation)
            if handler is None:
                raise BrokerError(
                    "operation_unavailable", "operation is not provisioned"
                )
            status = handler()
            if not isinstance(status, dict):
                raise BrokerError("malformed_result", "operation result is invalid")
            response = _response(self.protocol_version, request_id, True, status, None)
        except (BrokerError, ValueError) as exc:
            code = exc.code if isinstance(exc, BrokerError) else "unknown_operation"
            response = _response(self.protocol_version, request_id, False, {}, code)
        except Exception:  # noqa: BLE001 - privileged boundary never leaks details
            response = _response(
                self.protocol_version, request_id, False, {}, "operation_failed"
            )
        encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > self.max_message_bytes:
            encoded = (
                json.dumps(
                    _response(
                        self.protocol_version,
                        request_id,
                        False,
                        {},
                        "result_too_large",
                    ),
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
        connection.sendall(encoded)

    def _require_peer(self, connection: socket.socket) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            raise BrokerError(
                "peer_credentials_unavailable", "peer credentials are unavailable"
            )
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
        if uid != self.expected_uid:
            raise BrokerError("peer_denied", "broker peer is not authorized")


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

    def __init__(
        self,
        config: FixedBrokerConfig,
        *,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.config = config
        self.runner = runner

    def handlers(self) -> dict[BrokerOperation, Callable[[], dict[str, object]]]:
        candidates = {
            BrokerOperation.DESKTOP_WAKE: self.desktop_wake,
            BrokerOperation.DESKTOP_ENTER_REMOTE: lambda: self._desktop_task(
                "Enter Remote Mode"
            ),
            BrokerOperation.DESKTOP_RESTORE_LOCAL: lambda: self._desktop_task(
                "Restore Local Mode"
            ),
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

    def _desktop_task(self, task: str) -> dict[str, object]:
        return self._desktop_fixed(f'schtasks.exe /Run /TN "{task}"')

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


def _read_line(connection: socket.socket, maximum: int) -> bytes:
    chunks = bytearray()
    while len(chunks) <= maximum:
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


def _identifier(value: str) -> bool:
    return 16 <= len(value) <= 128 and all(
        character.isalnum() or character in "-_" for character in value
    )
