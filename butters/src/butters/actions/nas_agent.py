"""Authenticated hub for the single, separately credentialed NAS Agent."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from butters.actions import nas_protocol as protocol
from butters.actions.file_security import require_private_regular_file
from butters.assistant_config import NasAgentIngressSettings

LOG = logging.getLogger("butters.actions.nas_agent")


@dataclass(frozen=True, slots=True)
class _Credentials:
    agent_id: str
    token_sha256: str
    command_key: bytes
    protocol_version: int


@dataclass(slots=True)
class _Pending:
    connection_id: str
    action: str
    acknowledged: asyncio.Event
    result: asyncio.Future[dict[str, object]]
    started_at: float
    sent_at: float | None = None
    acknowledged_at: float | None = None


@dataclass(frozen=True, slots=True)
class _TimedOut:
    action: str
    expires_at: float


class NasAgentHub:
    """One exact identity, one exact schema set, and four typed operations."""

    _ACK_TIMEOUT_SECONDS = 3.0
    _LATE_RESULT_TTL_SECONDS = 30.0
    _LATE_RESULT_CAPACITY = 128
    _PUBLIC_ERRORS = frozenset(
        {
            "agent_disconnected",
            "agent_unavailable",
            "backend_unavailable",
            "busy",
            "cancelled",
            "duplicate_request",
            "invalid_action",
            "invalid_parameter",
            "invalid_request_id",
            "operation_disabled",
            "shutdown_credential_unavailable",
            "shutdown_result_indeterminate",
            "superseded_connection",
            "timeout",
            "transport_error",
            "truenas_authentication_failed",
            "truenas_malformed_response",
            "truenas_refused",
            "truenas_unavailable",
            "unsupported_action",
        }
    )

    def __init__(
        self,
        settings: NasAgentIngressSettings,
        *,
        monotonic: Any = time.monotonic,
        wall_clock: Any = time.time,
    ) -> None:
        self.settings = settings
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._state_lock = threading.Lock()
        self._request_gate = threading.Lock()
        self._credentials = (
            self._load_credentials(settings.config_path) if settings.enabled else None
        )
        self.reason = (
            "agent_disconnected" if self._credentials else "agent_not_configured"
        )
        self._socket: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, _Pending] = {}
        self._timed_out: OrderedDict[tuple[str, str], _TimedOut] = OrderedDict()
        self.connection_id: str | None = None
        self.connected_at: float | None = None
        self.last_authenticated_activity: float | None = None
        self._last_authenticated_wall: float | None = None
        self.version: str | None = None
        self.reconnect_count = 0
        self._system: dict[str, object] | None = None
        self._jellyfin: dict[str, object] | None = None
        self._shutdown_accepted_at: float | None = None

    @property
    def configured(self) -> bool:
        return self._credentials is not None

    def _agent_state(self, attached: bool, age: float | None) -> str:
        if not self.configured:
            return "not_configured"
        if not attached:
            return "disconnected"
        if age is None:
            return "awaiting_heartbeat"
        if age >= self.settings.heartbeat_stale_seconds:
            return "heartbeat_stale"
        if age >= self.settings.heartbeat_aging_seconds:
            return "heartbeat_aging"
        return "connected"

    def status(self) -> dict[str, object]:
        now = self._monotonic()
        with self._state_lock:
            attached = self._socket is not None
            last = self.last_authenticated_activity
            system = None if self._system is None else dict(self._system)
            jellyfin = None if self._jellyfin is None else dict(self._jellyfin)
            reason = self.reason
            connected_at = self.connected_at
            version = self.version
            reconnect_count = self.reconnect_count
            shutdown_at = self._shutdown_accepted_at
            observed_at = self._last_authenticated_wall
        age = None if last is None else max(0.0, now - last)
        state = self._agent_state(attached, age)
        # Disconnect/reconnect clears NAS-local observations. A consumer never
        # receives yesterday's healthy payload as if it described this socket.
        if not attached:
            system = None
            jellyfin = None
            observed_at = None
        return {
            "configured": self.configured,
            "identity": self._credentials.agent_id
            if self._credentials
            else "nas-primary",
            "state": state,
            "agent_connected": state == "connected",
            "reason": (
                None
                if state == "connected"
                else state
                if state in {"heartbeat_aging", "heartbeat_stale"}
                else reason
            ),
            "connected_since": connected_at,
            "last_authenticated_activity_age_seconds": age,
            "observed_at": observed_at,
            "version": version,
            "protocol": self.settings.protocol_version,
            "reconnect_count": reconnect_count,
            "system": system,
            "jellyfin": jellyfin,
            "shutdown_accepted_at": shutdown_at,
        }

    def agent_status(self) -> dict[str, object]:
        return self._request_public("nas.agent.status")

    def system_status(self) -> dict[str, object]:
        return self._request_public("nas.system.status")

    def jellyfin_status(self) -> dict[str, object]:
        return self._request_public("nas.jellyfin.status")

    def shutdown(
        self,
        *,
        cancel: threading.Event | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, object]:
        result = self._request(
            "nas.system.shutdown",
            {},
            cancel=cancel,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        projected = self._project_result("nas.system.shutdown", result)
        if projected is None:
            return self._failure("nas.system.shutdown", "malformed_result")
        if projected.get("success") and projected.get("accepted") is True:
            with self._state_lock:
                self._shutdown_accepted_at = self._wall_clock()
                if self._system is not None:
                    self._system = {**self._system, "system_state": "shutting_down"}
        return projected

    def _request_public(self, action: str) -> dict[str, object]:
        result = self._request(action, {})
        projected = self._project_result(action, result)
        return projected or self._failure(action, "malformed_result")

    def _request(
        self,
        action: str,
        values: dict[str, object],
        *,
        cancel: threading.Event | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, object]:
        try:
            values = protocol.parameters(action, values)
            rid = protocol.validate_request_id(request_id or str(uuid.uuid4()))
            idem = protocol.validate_request_id(idempotency_key or str(uuid.uuid4()))
        except protocol.ProtocolError as exc:
            return self._failure(action, str(exc))
        if cancel is not None and cancel.is_set():
            return self._failure(action, "cancelled")
        if self.status()["state"] != "connected":
            return self._failure(action, "agent_unavailable")
        with self._state_lock:
            websocket = self._socket
            loop = self._loop
            connection_id = self.connection_id
        if websocket is None or loop is None or connection_id is None:
            return self._failure(action, "agent_unavailable")
        if not self._request_gate.acquire(blocking=False):
            return self._failure(action, "busy")
        try:
            with suppress(RuntimeError):
                if asyncio.get_running_loop() is loop:
                    return self._failure(action, "transport_error")
            future = asyncio.run_coroutine_threadsafe(
                self._request_async(
                    websocket, connection_id, action, values, rid, idem
                ),
                loop,
            )
            try:
                return future.result(timeout=self.settings.request_timeout_seconds + 2)
            except concurrent.futures.TimeoutError:
                future.cancel()
                return self._failure(action, "timeout")
        finally:
            self._request_gate.release()

    async def _request_async(
        self,
        websocket: Any,
        connection_id: str,
        action: str,
        values: dict[str, object],
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        loop = asyncio.get_running_loop()
        pending = _Pending(
            connection_id,
            action,
            asyncio.Event(),
            loop.create_future(),
            self._monotonic(),
        )
        if (
            len(self._pending) >= self.settings.max_pending_requests
            or request_id in self._pending
        ):
            return self._failure(
                action,
                "busy" if request_id not in self._pending else "duplicate_request",
            )
        self._pending[request_id] = pending
        timeout = self.settings.request_timeout_seconds
        frame = protocol.envelope(
            "request",
            connection_id,
            request_id=request_id,
            action=action,
            target=self._credentials.agent_id,
            parameters=values,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout,
        )
        sent = False
        timed_out = False
        try:
            await websocket.send_text(
                protocol.canonical(
                    protocol.sign(frame, self._credentials.command_key)
                ).decode("ascii")
            )
            pending.sent_at = self._monotonic()
            LOG.info(
                json.dumps(
                    {
                        "event": "nas_agent_request_sent",
                        "request_id": request_id,
                        "action": action,
                        "send_ms": round(
                            max(0.0, pending.sent_at - pending.started_at) * 1000, 3
                        ),
                    },
                    sort_keys=True,
                )
            )
            sent = True
            started = loop.time()
            await asyncio.wait_for(
                pending.acknowledged.wait(), min(self._ACK_TIMEOUT_SECONDS, timeout)
            )
            remaining = timeout - (loop.time() - started)
            if remaining <= 0:
                raise asyncio.TimeoutError
            result = await asyncio.wait_for(asyncio.shield(pending.result), remaining)
            return {
                **result,
                "request_id": request_id,
                "idempotency_key": idempotency_key,
                "connection_id": connection_id,
                "transport": "nas_agent_wss",
            }
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            timed_out = True
            return self._failure(action, "timeout")
        except Exception as exc:  # noqa: BLE001 - transport exceptions are redacted.
            code = (
                str(exc)
                if isinstance(exc, protocol.ProtocolError)
                else "transport_error"
            )
            return self._failure(action, code)
        finally:
            if self._pending.get(request_id) is pending:
                self._pending.pop(request_id, None)
            if timed_out:
                self._remember_timeout(connection_id, request_id, action)
            if (
                sent
                and not pending.result.done()
                and self._is_current(websocket, connection_id)
            ):
                cancel_frame = protocol.envelope(
                    "cancel", connection_id, request_id=request_id
                )
                with suppress(Exception):
                    await websocket.send_text(
                        protocol.canonical(
                            protocol.sign(cancel_frame, self._credentials.command_key)
                        ).decode("ascii")
                    )

    async def socket(self, websocket: Any) -> None:
        if not self.configured or websocket.headers.get("origin") is not None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        current = False
        connection_id = ""
        try:
            hello = protocol.decode(
                await asyncio.wait_for(
                    websocket.receive_text(), self.settings.hello_timeout_seconds
                )
            )
            self._authenticate_hello(hello)
            with self._state_lock:
                old = self._socket
                old_connection_id = self.connection_id
                self._socket = websocket
                self._loop = asyncio.get_running_loop()
                self.connection_id = secrets.token_hex(32)
                self.connected_at = self._wall_clock()
                self.last_authenticated_activity = None
                self._last_authenticated_wall = None
                self._system = None
                self._jellyfin = None
                self._shutdown_accepted_at = None
                self.version = str(hello["version"])
                self.reconnect_count += 1
                self.reason = "awaiting_heartbeat"
                connection_id = self.connection_id
            current = True
            if old_connection_id is not None:
                self._fail_pending(old_connection_id, "superseded_connection")
                self._forget_timeouts(old_connection_id)
            if old is not None and old is not websocket:
                await old.close(code=1012)
            welcome = protocol.envelope(
                "welcome",
                connection_id,
                protocol=self.settings.protocol_version,
                server_time=self._wall_clock(),
            )
            await websocket.send_text(
                protocol.canonical(
                    protocol.sign(welcome, self._credentials.command_key)
                ).decode("ascii")
            )
            sequence = -1
            while self._is_current(websocket, connection_id):
                raw = await asyncio.wait_for(
                    websocket.receive_text(), self.settings.socket_idle_seconds
                )
                frame = protocol.verify(
                    protocol.decode(raw), self._credentials.command_key, connection_id
                )
                if not self._is_current(websocket, connection_id):
                    raise protocol.ProtocolError("superseded_connection")
                if frame.get("type") == "heartbeat":
                    sequence = self._handle_heartbeat(frame, sequence, websocket)
                elif frame.get("type") in {"ack", "result", "error"}:
                    self._handle_response(frame, websocket, connection_id)
                else:
                    raise protocol.ProtocolError("malformed_message")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - frames and secrets are never logged.
            reason = (
                str(exc)
                if isinstance(exc, protocol.ProtocolError)
                else type(exc).__name__
            )
            with self._state_lock:
                if current and self._socket is websocket:
                    self.reason = reason
        finally:
            if current:
                self._disconnect_if_current(websocket, connection_id)
            with suppress(Exception):
                await websocket.close(code=1008)

    def _handle_heartbeat(
        self, frame: dict[str, object], sequence: int, websocket: Any
    ) -> int:
        expected = {
            "type",
            "connection_id",
            "issued_at",
            "sig",
            "seq",
            "state",
            "version",
        }
        seq = frame.get("seq")
        state = frame.get("state")
        valid = (
            set(frame) == expected
            and frame.get("version") == self.version
            and type(seq) is int
            and seq > sequence
            and type(state) is dict
            and set(state) == {"system", "jellyfin"}
        )
        system = self._project_heartbeat_system(
            state.get("system") if isinstance(state, dict) else None
        )
        jellyfin = self._project_heartbeat_jellyfin(
            state.get("jellyfin") if isinstance(state, dict) else None
        )
        if not valid or system is None or jellyfin is None:
            code = (
                "replayed_message"
                if type(seq) is int and seq <= sequence
                else "malformed_message"
            )
            raise protocol.ProtocolError(code)
        with self._state_lock:
            if self._socket is not websocket:
                raise protocol.ProtocolError("superseded_connection")
            self._system = system
            self._jellyfin = jellyfin
            self.last_authenticated_activity = self._monotonic()
            self._last_authenticated_wall = self._wall_clock()
            self.reason = "agent_connected"
        return seq

    def _handle_response(
        self, frame: dict[str, object], websocket: Any, connection_id: str
    ) -> None:
        kind = frame.get("type")
        request_id = frame.get("request_id")
        if kind == "error" and request_id is None:
            if set(frame) != {"type", "connection_id", "issued_at", "sig", "error"}:
                raise protocol.ProtocolError("malformed_message")
            return
        try:
            request_id = protocol.validate_request_id(request_id)
        except protocol.ProtocolError as exc:
            raise protocol.ProtocolError("wrong_request_id") from exc
        pending = self._pending.get(request_id)
        if pending is None:
            if self._ignore_late_terminal(frame, websocket, connection_id, request_id):
                return
            raise protocol.ProtocolError("wrong_request_id")
        if pending.connection_id != connection_id or not self._is_current(
            websocket, connection_id
        ):
            raise protocol.ProtocolError("wrong_request_id")
        base = {"type", "connection_id", "issued_at", "sig", "request_id"}
        if kind == "ack":
            if set(frame) != base or pending.acknowledged.is_set():
                raise protocol.ProtocolError("replayed_message")
            pending.acknowledged_at = self._monotonic()
            pending.acknowledged.set()
            LOG.info(
                json.dumps(
                    {
                        "event": "nas_agent_ack_received",
                        "request_id": request_id,
                        "action": pending.action,
                        "elapsed_ms": round(
                            max(0.0, pending.acknowledged_at - pending.started_at)
                            * 1000,
                            3,
                        ),
                    },
                    sort_keys=True,
                )
            )
            return
        if kind == "result":
            value = frame.get("result")
            if (
                set(frame) != base | {"result", "duplicate"}
                or type(frame.get("duplicate")) is not bool
                or not pending.acknowledged.is_set()
                or pending.result.done()
                or self._project_result(pending.action, value) is None
            ):
                raise protocol.ProtocolError("malformed_result")
            pending.result.set_result(dict(value))
            received_at = self._monotonic()
            LOG.info(
                json.dumps(
                    {
                        "event": "nas_agent_result_received",
                        "request_id": request_id,
                        "action": pending.action,
                        "ack_ms": (
                            None
                            if pending.acknowledged_at is None
                            else round(
                                max(
                                    0.0,
                                    pending.acknowledged_at - pending.started_at,
                                )
                                * 1000,
                                3,
                            )
                        ),
                        "total_ms": round(
                            max(0.0, received_at - pending.started_at) * 1000, 3
                        ),
                    },
                    sort_keys=True,
                )
            )
            return
        if kind == "error":
            error = frame.get("error")
            if (
                set(frame) != base | {"error"}
                or not isinstance(error, str)
                or not error
                or len(error) > 64
            ):
                raise protocol.ProtocolError("malformed_result")
            pending.acknowledged.set()
            pending.result.set_result(self._failure(pending.action, error))
            return
        raise protocol.ProtocolError("malformed_message")

    def _authenticate_hello(self, hello: dict[str, object]) -> None:
        credentials = self._credentials
        assert credentials is not None
        token = hello.get("token")
        valid = (
            set(hello)
            == {"type", "protocol", "schema", "agent_id", "version", "token", "actions"}
            and hello.get("type") == "hello"
            and hello.get("protocol") == credentials.protocol_version
            and hello.get("schema") == protocol.ACTION_SCHEMA_VERSION
            and hello.get("agent_id") == "nas-primary" == credentials.agent_id
            and isinstance(token, str)
            and len(token) == 64
            and hmac.compare_digest(
                hashlib.sha256(token.encode()).hexdigest(), credentials.token_sha256
            )
            and hello.get("actions") == sorted(protocol.SCHEMAS)
            and isinstance(hello.get("version"), str)
            and 0 < len(str(hello["version"])) <= 32
        )
        if not valid:
            raise protocol.ProtocolError("unauthorized")

    def _fail_pending(self, connection_id: str, code: str) -> None:
        for pending in tuple(self._pending.values()):
            if pending.connection_id != connection_id or pending.result.done():
                continue
            was_acknowledged = pending.acknowledged.is_set()
            pending.acknowledged.set()
            if (
                pending.action == "nas.system.shutdown"
                and code == "agent_disconnected"
                and was_acknowledged
            ):
                pending.result.set_result(
                    {
                        **self._failure(
                            pending.action, "shutdown_result_indeterminate"
                        ),
                        "indeterminate": True,
                    }
                )
            else:
                pending.result.set_result(self._failure(pending.action, code))

    def _disconnect_if_current(self, websocket: Any, connection_id: str) -> None:
        with self._state_lock:
            if self._socket is not websocket or self.connection_id != connection_id:
                return
            reason = self.reason
            self._socket = None
            self._loop = None
            self.connection_id = None
            self.connected_at = None
            self.last_authenticated_activity = None
            self._last_authenticated_wall = None
            self._system = None
            self._jellyfin = None
            self.version = None
            self.reason = reason
        self._fail_pending(connection_id, "agent_disconnected")
        self._forget_timeouts(connection_id)

    def _is_current(self, websocket: Any, connection_id: str) -> bool:
        with self._state_lock:
            return self._socket is websocket and self.connection_id == connection_id

    def _remember_timeout(
        self, connection_id: str, request_id: str, action: str
    ) -> None:
        self._prune_timeouts()
        self._timed_out[(connection_id, request_id)] = _TimedOut(
            action, self._monotonic() + self._LATE_RESULT_TTL_SECONDS
        )
        while len(self._timed_out) > self._LATE_RESULT_CAPACITY:
            self._timed_out.popitem(last=False)

    def _ignore_late_terminal(
        self,
        frame: dict[str, object],
        websocket: Any,
        connection_id: str,
        request_id: str,
    ) -> bool:
        if not self._is_current(websocket, connection_id):
            return False
        self._prune_timeouts()
        key = (connection_id, request_id)
        timed_out = self._timed_out.get(key)
        if timed_out is None:
            return False
        kind = frame.get("type")
        base = {"type", "connection_id", "issued_at", "sig", "request_id"}
        if kind == "ack":
            if set(frame) != base:
                raise protocol.ProtocolError("replayed_message")
            return True
        valid = False
        if kind == "result":
            valid = (
                set(frame) == base | {"result", "duplicate"}
                and type(frame.get("duplicate")) is bool
                and self._project_result(timed_out.action, frame.get("result"))
                is not None
            )
        elif kind == "error":
            error = frame.get("error")
            valid = (
                set(frame) == base | {"error"}
                and isinstance(error, str)
                and 0 < len(error) <= 64
            )
        if not valid:
            raise protocol.ProtocolError("malformed_result")
        self._timed_out.pop(key, None)
        return True

    def _prune_timeouts(self) -> None:
        now = self._monotonic()
        for key, value in tuple(self._timed_out.items()):
            if value.expires_at > now:
                break
            self._timed_out.pop(key, None)

    def _forget_timeouts(self, connection_id: str) -> None:
        for key in tuple(self._timed_out):
            if key[0] == connection_id:
                self._timed_out.pop(key, None)

    @classmethod
    def _project_result(cls, action: str, value: object) -> dict[str, object] | None:
        if (
            type(value) is not dict
            or value.get("action") != action
            or type(value.get("success")) is not bool
        ):
            return None
        metadata = {
            key: value[key]
            for key in ("request_id", "idempotency_key", "transport")
            if key in value and isinstance(value[key], str)
        }
        common = {
            "action",
            "target",
            "started_at",
            "success",
            "completed_at",
            "duration_seconds",
            "request_id",
            "idempotency_key",
            "connection_id",
            "transport",
        }
        payload = {key: item for key, item in value.items() if key not in common}
        if value["success"] is False:
            error = value.get("error")
            code = (
                error
                if isinstance(error, str) and error in cls._PUBLIC_ERRORS
                else "agent_action_failed"
            )
            result = {**metadata, "action": action, "success": False, "error": code}
            if value.get("indeterminate") is True and action == "nas.system.shutdown":
                result["indeterminate"] = True
            return result
        if action == "nas.agent.status":
            if set(payload) != {
                "agent_version",
                "protocol_version",
                "schema_version",
                "hostname",
                "uptime_seconds",
                "connection_count",
                "connection_uptime_seconds",
                "last_heartbeat_sequence",
            }:
                return None
            fields = {
                "agent_version": cls._text(value.get("agent_version"), 32),
                "protocol_version": cls._integer(value.get("protocol_version"), 1, 1),
                "schema_version": cls._integer(value.get("schema_version"), 1, 1),
                "hostname": cls._nullable_text(value.get("hostname"), 255),
                "uptime_seconds": cls._number(value.get("uptime_seconds")),
                "connection_count": cls._integer(
                    value.get("connection_count"), 1, 2**31 - 1
                ),
                "connection_uptime_seconds": cls._number(
                    value.get("connection_uptime_seconds")
                ),
                "last_heartbeat_sequence": cls._nullable_integer(
                    value.get("last_heartbeat_sequence")
                ),
            }
        elif action == "nas.system.status":
            fields = cls._project_heartbeat_system(payload)
        elif action == "nas.jellyfin.status":
            fields = cls._project_heartbeat_jellyfin(payload)
        else:
            fields = (
                {"accepted": True, "state": "scheduled", "method": "system.shutdown"}
                if set(payload) == {"accepted", "state", "method"}
                and payload.get("accepted") is True
                and payload.get("state") == "scheduled"
                and payload.get("method") == "system.shutdown"
                else None
            )
        if fields is None or any(item is cls._INVALID for item in fields.values()):
            return None
        return {**metadata, "action": action, "success": True, **fields}

    _INVALID = object()

    @classmethod
    def _project_heartbeat_system(cls, value: object) -> dict[str, object] | None:
        if type(value) is not dict:
            return None
        if value.get("reachable") is False:
            if set(value) != {"reachable", "error"} or not cls._safe_error(
                value.get("error")
            ):
                return None
            return {"reachable": False, "error": value["error"]}
        expected = {
            "reachable",
            "hostname",
            "version",
            "uptime_seconds",
            "system_state",
        }
        if set(value) != expected:
            return None
        fields = {
            "reachable": value.get("reachable")
            if type(value.get("reachable")) is bool
            else cls._INVALID,
            "hostname": cls._nullable_text(value.get("hostname"), 255),
            "version": cls._nullable_text(value.get("version"), 128),
            "uptime_seconds": cls._nullable_number(value.get("uptime_seconds")),
            "system_state": value.get("system_state")
            if value.get("system_state") in {"unknown", "online", "shutting_down"}
            else cls._INVALID,
        }
        return None if any(item is cls._INVALID for item in fields.values()) else fields

    @classmethod
    def _project_heartbeat_jellyfin(cls, value: object) -> dict[str, object] | None:
        if type(value) is not dict:
            return None
        if value.get("reachable") is False and set(value) == {"reachable", "error"}:
            return dict(value) if cls._safe_error(value.get("error")) else None
        expected = {"reachable", "ready", "version", "http_status"}
        if set(value) != expected:
            return None
        status = value.get("http_status")
        fields = {
            "reachable": value.get("reachable")
            if type(value.get("reachable")) is bool
            else cls._INVALID,
            "ready": value.get("ready")
            if type(value.get("ready")) is bool
            else cls._INVALID,
            "version": cls._nullable_text(value.get("version"), 64),
            "http_status": status
            if status is None or type(status) is int and 100 <= status <= 599
            else cls._INVALID,
        }
        return None if any(item is cls._INVALID for item in fields.values()) else fields

    @classmethod
    def _safe_error(cls, value: object) -> bool:
        return isinstance(value, str) and value in cls._PUBLIC_ERRORS

    @classmethod
    def _text(cls, value: object, limit: int) -> object:
        return (
            value
            if isinstance(value, str) and 0 < len(value) <= limit
            else cls._INVALID
        )

    @classmethod
    def _nullable_text(cls, value: object, limit: int) -> object:
        return (
            value
            if value is None or isinstance(value, str) and 0 < len(value) <= limit
            else cls._INVALID
        )

    @classmethod
    def _number(cls, value: object) -> object:
        return (
            value
            if type(value) in (int, float) and math.isfinite(value) and value >= 0
            else cls._INVALID
        )

    @classmethod
    def _nullable_number(cls, value: object) -> object:
        return value if value is None else cls._number(value)

    @classmethod
    def _integer(cls, value: object, low: int, high: int) -> object:
        return value if type(value) is int and low <= value <= high else cls._INVALID

    @classmethod
    def _nullable_integer(cls, value: object) -> object:
        return (
            value
            if value is None or type(value) is int and value >= 0
            else cls._INVALID
        )

    @staticmethod
    def _failure(action: str, code: str) -> dict[str, object]:
        return {"action": action, "success": False, "error": code}

    @staticmethod
    def _load_credentials(path: Path) -> _Credentials | None:
        try:
            require_private_regular_file(path, "nas_agent_configuration")
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if set(data) != {
                "schema_version",
                "agent_id",
                "token_sha256",
                "command_key_file",
                "protocol_version",
            }:
                raise ValueError("invalid_agent_configuration")
            key_path = Path(data["command_key_file"])
            if not key_path.is_absolute():
                raise ValueError("unsafe_command_key")
            require_private_regular_file(key_path, "nas_agent_command_key")
            key = bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
            token_hash = data["token_sha256"]
            if (
                data.get("schema_version") != 1
                or data.get("protocol_version") != 1
                or data.get("agent_id") != "nas-primary"
                or not isinstance(token_hash, str)
                or len(token_hash) != 64
                or len(key) != 32
            ):
                raise ValueError("invalid_agent_configuration")
            int(token_hash, 16)
            return _Credentials("nas-primary", token_hash.lower(), key, 1)
        except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
            return None
