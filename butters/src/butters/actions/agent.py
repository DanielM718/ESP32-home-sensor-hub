"""Authenticated Desktop Agent state and narrow application request transport.

The public effector surface is intentionally capability-specific. Executable
paths, arguments, commands, hosts, and environment data cannot enter it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
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

from butters.actions.file_security import require_private_regular_file
from butters.assistant_config import AgentIngressSettings
from butters.state import StateFacet, StateSnapshot


@dataclass(frozen=True, slots=True)
class _MachineCredentials:
    agent_id: str
    token_sha256: str
    command_key: bytes
    protocol_version: int


@dataclass(slots=True)
class _PendingRequest:
    connection_id: str
    action: str
    acknowledged: asyncio.Event
    result: asyncio.Future[dict[str, object]]


@dataclass(frozen=True, slots=True)
class _TimedOutRequest:
    action: str
    expires_at: float


class AgentHub:
    """Own one authenticated machine connection and three typed app operations."""

    _ACK_TIMEOUT_SECONDS = 3.0
    _LATE_RESULT_TTL_SECONDS = 30.0
    _LATE_RESULT_CAPACITY = 128
    _PUBLIC_ERRORS = frozenset(
        {
            "agent_action_failed",
            "agent_disconnected",
            "agent_unavailable",
            "app_not_installed",
            "busy",
            "cancelled",
            "duplicate_request",
            "interactive_session_unavailable",
            "invalid_action",
            "invalid_parameter",
            "invalid_request_id",
            "launch_failed",
            "malformed_result",
            "platform_error",
            "session_inactive",
            "superseded_connection",
            "timeout",
            "transport_error",
            "unknown_app",
            "unsupported_action",
        }
    )
    _PUBLIC_REASONS = frozenset(
        {
            "app_not_installed",
            "existing_process_has_no_visible_window",
            "invalid_registry_entry",
        }
    )

    def __init__(
        self,
        settings: AgentIngressSettings,
        *,
        monotonic: Any = time.monotonic,
        wall_clock: Any = time.time,
    ) -> None:
        self.settings = settings
        self._state_lock = threading.Lock()
        self._request_gate = threading.Lock()
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._credentials: _MachineCredentials | None = None
        self.reason = "agent_not_configured"
        if settings.enabled:
            self._credentials = self._load_credentials(settings.config_path)
            self.reason = (
                "agent_disconnected" if self._credentials else "agent_not_configured"
            )
        self._socket: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._actions: tuple[str, ...] = ()
        self._pending: dict[str, _PendingRequest] = {}
        self._timed_out: OrderedDict[tuple[str, str], _TimedOutRequest] = OrderedDict()
        self._app_catalog: dict[str, dict[str, object]] = {}
        self._catalog_connection_id: str | None = None
        self.connection_id: str | None = None
        self.connected_at: float | None = None
        self.last_authenticated_activity: float | None = None
        self._last_authenticated_wall: float | None = None
        self._session: dict[str, object] = {}
        self.version: str | None = None
        self.reconnect_count = 0

    @property
    def configured(self) -> bool:
        return self._credentials is not None

    def snapshot(self) -> StateSnapshot:
        now_mono = self._monotonic()
        now_wall = self._wall_clock()
        with self._state_lock:
            last_activity = self.last_authenticated_activity
            attached = self._socket is not None
            session = dict(self._session)
            last_wall = self._last_authenticated_wall
        age = None if last_activity is None else max(0.0, now_mono - last_activity)
        agent_state = self._agent_state(attached, age)
        if agent_state == "connected":
            interactive = (
                "present"
                if session.get("interactive_session") is True
                else "absent"
                if session.get("interactive_session") is False
                else "unknown"
            )
        else:
            interactive = "unknown"

        observed_at = last_wall or now_wall
        agent_confidence = (
            "stale"
            if agent_state in {"heartbeat_stale", "heartbeat_aging"}
            else "observed"
            if agent_state != "not_configured"
            else "unknown"
        )
        interactive_confidence = "observed" if interactive != "unknown" else "unknown"
        facets = (
            StateFacet(
                "desktop.agent", agent_state, agent_confidence, observed_at, age
            ),
            StateFacet(
                "desktop.interactive_session",
                interactive,
                interactive_confidence,
                observed_at,
                age if interactive != "unknown" else None,
            ),
        )
        incomplete = tuple(
            facet.name for facet in facets if facet.confidence in {"unknown", "stale"}
        )
        return StateSnapshot(facets, now_wall, incomplete)

    def status(self) -> dict[str, object]:
        snapshot = self.snapshot()
        facets = {facet.name: facet for facet in snapshot.facets}
        agent = facets["desktop.agent"]
        interactive = facets["desktop.interactive_session"]
        with self._state_lock:
            reason = self.reason
            connected_since = self.connected_at
            version = self.version
            reconnect_count = self.reconnect_count
        return {
            "configured": self.configured,
            "state": agent.value,
            "agent_connected": agent.value == "connected",
            "interactive_session": interactive.value,
            "reason": None if agent.value == "connected" else reason,
            "connected_since": connected_since,
            "last_authenticated_activity_age_seconds": agent.age_seconds,
            "version": version,
            "protocol": self.settings.protocol_version,
            "reconnect_count": reconnect_count,
        }

    def list_apps(self) -> dict[str, object]:
        """Return only symbolic names and bounded, non-executable metadata."""

        result = self._request("desktop.app.list", {})
        if not result.get("success"):
            return self._public_failure("desktop.app.list", result)
        raw_apps = result.get("apps")
        if not isinstance(raw_apps, list):
            return self._failure("desktop.app.list", "malformed_result")
        catalog: dict[str, dict[str, object]] = {}
        for raw in raw_apps:
            projected = self._project_app(raw)
            if projected is None or projected["app"] in catalog:
                return self._failure("desktop.app.list", "malformed_result")
            catalog[str(projected["app"])] = projected
        with self._state_lock:
            if self.connection_id != result.get("connection_id"):
                return self._failure("desktop.app.list", "superseded_connection")
            self._app_catalog = dict(catalog)
            self._catalog_connection_id = self.connection_id
        return {
            **self._transport_metadata(result),
            "action": "desktop.app.list",
            "success": True,
            "apps": [catalog[name] for name in sorted(catalog)],
        }

    def app_status(self, name: str) -> dict[str, object]:
        invalid = self._validate_app_name("desktop.app.status", name)
        if invalid is not None:
            return invalid
        known = self._known_app(name)
        if isinstance(known, dict) and known.get("success") is False:
            return {**known, "action": "desktop.app.status"}
        if not isinstance(known, str):
            return self._failure("desktop.app.status", "unknown_app")
        result = self._request(
            "desktop.app.status", {"app": name}, expected_connection_id=known
        )
        if not result.get("success"):
            return self._public_failure("desktop.app.status", result)
        app = self._project_app(result)
        if app is None or app["app"] != name:
            return self._failure("desktop.app.status", "malformed_result")
        return {
            **self._transport_metadata(result),
            "action": "desktop.app.status",
            "success": True,
            **app,
        }

    def launch_app(
        self,
        name: str,
        *,
        cancel: threading.Event | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, object]:
        """Launch one agent-allowlisted name; already-running is successful."""

        invalid = self._validate_app_name("desktop.app.launch", name)
        if invalid is not None:
            return invalid
        status = self.status()
        if status["state"] != "connected":
            return self._failure("desktop.app.launch", "agent_unavailable")
        if status["interactive_session"] != "present":
            return self._failure(
                "desktop.app.launch", "interactive_session_unavailable"
            )
        known = self._known_app(name)
        if isinstance(known, dict) and known.get("success") is False:
            return {**known, "action": "desktop.app.launch"}
        if not isinstance(known, str):
            return self._failure("desktop.app.launch", "unknown_app")
        result = self._request(
            "desktop.app.launch",
            {"app": name},
            cancel=cancel,
            idempotency_key=idempotency_key,
            request_id=request_id,
            expected_connection_id=known,
        )
        if not result.get("success"):
            return self._public_failure("desktop.app.launch", result)
        app = self._project_app(result)
        state = result.get("state")
        if (
            app is None
            or app["app"] != name
            or state not in {"running", "already_running"}
        ):
            return self._failure("desktop.app.launch", "malformed_result")
        return {
            **self._transport_metadata(result),
            "action": "desktop.app.launch",
            "success": True,
            "outcome": "already_running" if state == "already_running" else "launched",
            **app,
        }

    def _known_app(self, name: str) -> str | bool | dict[str, object]:
        with self._state_lock:
            current = self.connection_id
            if self._catalog_connection_id == current and current is not None:
                return current if name in self._app_catalog else False
        catalog = self.list_apps()
        if not catalog.get("success"):
            return catalog
        with self._state_lock:
            current = self.connection_id
            if (
                current is not None
                and self._catalog_connection_id == current
                and name in self._app_catalog
            ):
                return current
        return False

    def _request(
        self,
        action: str,
        values: dict[str, str],
        *,
        cancel: threading.Event | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        expected_connection_id: str | None = None,
    ) -> dict[str, object]:
        protocol = self._protocol()
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
            actions = self._actions
        if (
            expected_connection_id is not None
            and connection_id != expected_connection_id
        ):
            return self._failure(action, "superseded_connection")
        if (
            websocket is None
            or loop is None
            or connection_id is None
            or action not in actions
        ):
            return self._failure(action, "unsupported_action")
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
        values: dict[str, str],
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        protocol = self._protocol()
        loop = asyncio.get_running_loop()
        pending = _PendingRequest(
            connection_id, action, asyncio.Event(), loop.create_future()
        )
        if request_id in self._pending:
            return self._failure(action, "duplicate_request")
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
                "transport": "desktop_agent_wss",
            }
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            timed_out = True
            return self._failure(action, "timeout")
        except Exception as exc:  # noqa: BLE001 - never format transport exceptions.
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
                cancel = protocol.envelope(
                    "cancel", connection_id, request_id=request_id
                )
                with suppress(Exception):
                    await websocket.send_text(
                        protocol.canonical(
                            protocol.sign(cancel, self._credentials.command_key)
                        ).decode("ascii")
                    )

    async def socket(self, websocket: Any) -> None:
        """Authenticate the machine and process heartbeats plus correlated replies."""

        if not self.configured or websocket.headers.get("origin") is not None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        current = False
        connection_id = ""
        try:
            protocol = self._protocol()
            hello = protocol.decode(
                await asyncio.wait_for(
                    websocket.receive_text(), self.settings.hello_timeout_seconds
                )
            )
            self._authenticate_hello(hello, protocol.SCHEMAS)

            with self._state_lock:
                old = self._socket
                old_connection_id = self.connection_id
                self._socket = websocket
                self._loop = asyncio.get_running_loop()
                self.connection_id = secrets.token_hex(32)
                self.connected_at = self._wall_clock()
                self.last_authenticated_activity = None
                self._last_authenticated_wall = None
                self._session = {}
                self._actions = tuple(hello["actions"])
                self._app_catalog = {}
                self._catalog_connection_id = None
                self.version = hello["version"]
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
            protocol_error = self._protocol().ProtocolError
            reason = str(exc) if isinstance(exc, protocol_error) else type(exc).__name__
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
        protocol = self._protocol()
        expected = {
            "type",
            "connection_id",
            "issued_at",
            "sig",
            "seq",
            "session",
            "version",
        }
        seq = frame.get("seq")
        session = frame.get("session")
        if (
            set(frame) != expected
            or frame.get("version") != self.version
            or type(seq) is not int
            or seq <= sequence
            or type(session) is not dict
            or set(session)
            - {
                "state",
                "gui_launch",
                "interactive_session",
                "session_id",
                "observed_at",
            }
            or session.get("state") not in {"ACTIVE", "LOCKED", "NONE", "MULTIPLE"}
            or type(session.get("gui_launch")) is not bool
            or type(session.get("interactive_session")) is not bool
        ):
            code = (
                "replayed_message"
                if type(seq) is int and seq <= sequence
                else "malformed_message"
            )
            raise protocol.ProtocolError(code)
        with self._state_lock:
            if self._socket is not websocket:
                raise protocol.ProtocolError("superseded_connection")
            self._session = dict(session)
            self.last_authenticated_activity = self._monotonic()
            self._last_authenticated_wall = self._wall_clock()
            self.reason = "agent_connected"
        return seq

    def _handle_response(
        self, frame: dict[str, object], websocket: Any, connection_id: str
    ) -> None:
        protocol = self._protocol()
        kind = frame.get("type")
        request_id = frame.get("request_id")
        if kind == "error" and request_id is None:
            if set(frame) != {"type", "connection_id", "issued_at", "sig", "error"}:
                raise protocol.ProtocolError("malformed_message")
            return  # S-7: an unidentifiable error owns no pending request.
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
            pending.acknowledged.set()
            return
        if kind == "result":
            value = frame.get("result")
            if pending.result.done():
                raise protocol.ProtocolError("replayed_message")
            if (
                set(frame) != base | {"result", "duplicate"}
                or type(frame.get("duplicate")) is not bool
                or not pending.acknowledged.is_set()
                or type(value) is not dict
                or type(value.get("success")) is not bool
                or value.get("action") != pending.action
            ):
                raise protocol.ProtocolError("malformed_result")
            pending.result.set_result(dict(value))
            return
        if kind == "error":
            error = frame.get("error")
            if (
                set(frame) != base | {"error"}
                or not isinstance(error, str)
                or not error
                or len(error) > 64
                or pending.result.done()
            ):
                raise protocol.ProtocolError("malformed_result")
            pending.acknowledged.set()
            pending.result.set_result(self._failure(pending.action, error))
            return
        raise protocol.ProtocolError("malformed_message")

    def _remember_timeout(
        self, connection_id: str, request_id: str, action: str
    ) -> None:
        self._prune_timeouts()
        key = (connection_id, request_id)
        self._timed_out[key] = _TimedOutRequest(
            action, self._monotonic() + self._LATE_RESULT_TTL_SECONDS
        )
        self._timed_out.move_to_end(key)
        while len(self._timed_out) > self._LATE_RESULT_CAPACITY:
            self._timed_out.popitem(last=False)

    def _ignore_late_terminal(
        self,
        frame: dict[str, object],
        websocket: Any,
        connection_id: str,
        request_id: str,
    ) -> bool:
        """Ignore one valid terminal reply to a known timed-out current request."""

        if not self._is_current(websocket, connection_id):
            return False
        self._prune_timeouts()
        key = (connection_id, request_id)
        timed_out = self._timed_out.get(key)
        if timed_out is None:
            return False
        kind = frame.get("type")
        base = {"type", "connection_id", "issued_at", "sig", "request_id"}
        if kind == "result":
            value = frame.get("result")
            valid = (
                set(frame) == base | {"result", "duplicate"}
                and type(frame.get("duplicate")) is bool
                and type(value) is dict
                and type(value.get("success")) is bool
                and value.get("action") == timed_out.action
            )
        elif kind == "error":
            error = frame.get("error")
            valid = (
                set(frame) == base | {"error"}
                and isinstance(error, str)
                and bool(error)
                and len(error) <= 64
            )
        else:
            return False
        if not valid:
            raise self._protocol().ProtocolError("malformed_result")
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

    def _authenticate_hello(self, hello: dict[str, object], schemas: object) -> None:
        token = hello.get("token")
        actions = hello.get("actions")
        credentials = self._credentials
        assert credentials is not None
        valid = (
            set(hello)
            == {"type", "protocol", "schema", "agent_id", "version", "token", "actions"}
            and hello.get("type") == "hello"
            and type(hello.get("protocol")) is int
            and hello.get("protocol") == credentials.protocol_version
            and type(hello.get("schema")) is int
            and hello.get("schema") == 1
            and hello.get("agent_id") == credentials.agent_id
            and isinstance(token, str)
            and len(token) == 64
            and hmac.compare_digest(
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                credentials.token_sha256,
            )
            and isinstance(actions, list)
            and all(isinstance(action, str) and action in schemas for action in actions)
            and len(actions) == len(set(actions))
            and isinstance(hello.get("version"), str)
            and 0 < len(hello["version"]) <= 32
        )
        if not valid:
            raise self._protocol().ProtocolError("unauthorized")

    def _fail_pending(self, connection_id: str, code: str) -> None:
        for pending in tuple(self._pending.values()):
            if pending.connection_id == connection_id and not pending.result.done():
                pending.acknowledged.set()
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
            self._session = {}
            self._actions = ()
            self._app_catalog = {}
            self._catalog_connection_id = None
            self.version = None
            self.reason = reason
        self._fail_pending(connection_id, "agent_disconnected")
        self._forget_timeouts(connection_id)

    def _is_current(self, websocket: Any, connection_id: str) -> bool:
        with self._state_lock:
            return self._socket is websocket and self.connection_id == connection_id

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

    @classmethod
    def _project_app(cls, value: object) -> dict[str, object] | None:
        protocol = cls._protocol()
        if type(value) is not dict:
            return None
        name = value.get("app")
        if not isinstance(name, str) or not protocol.NAME.fullmatch(name):
            return None
        installed = value.get("installed")
        running = value.get("running")
        visible = value.get("visible_window")
        reason = value.get("reason")
        if type(installed) is not bool or type(running) is not bool:
            return None
        if visible is not None and type(visible) is not bool:
            return None
        if reason is not None and reason not in cls._PUBLIC_REASONS:
            return None
        state = (
            "unavailable" if not installed else "running" if running else "not_running"
        )
        projected: dict[str, object] = {
            "app": name,
            "status": state,
            "available": installed,
        }
        if visible is not None:
            projected["visible_window"] = visible
        if reason is not None:
            projected["reason"] = reason
        return projected

    @staticmethod
    def _transport_metadata(result: dict[str, object]) -> dict[str, object]:
        return {
            key: result[key]
            for key in ("request_id", "idempotency_key", "transport")
            if key in result
        } | ({"transport_state": "completed"} if result.get("success") else {})

    @classmethod
    def _public_failure(
        cls, action: str, result: dict[str, object]
    ) -> dict[str, object]:
        error = result.get("error")
        code = (
            error
            if isinstance(error, str) and error in cls._PUBLIC_ERRORS
            else "agent_action_failed"
        )
        return {
            **cls._transport_metadata(result),
            "action": action,
            "success": False,
            "error": code,
        }

    def _validate_app_name(self, action: str, name: str) -> dict[str, object] | None:
        try:
            self._protocol().parameters(action, {"app": name})
        except self._protocol().ProtocolError as exc:
            return self._failure(action, str(exc))
        return None

    @staticmethod
    def _failure(action: str, code: str) -> dict[str, object]:
        return {"action": action, "success": False, "error": code}

    @staticmethod
    def _protocol() -> Any:
        from butters_agent import protocol

        return protocol

    @staticmethod
    def _load_credentials(path: Path) -> _MachineCredentials | None:
        try:
            if path.stat().st_mode & 0o022:
                raise ValueError("unsafe_configuration")
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            key_path = Path(data["command_key_file"])
            if not key_path.is_absolute():
                raise ValueError("unsafe_command_key")
            require_private_regular_file(key_path, "command_key")
            key = bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
            token_hash = data["token_sha256"]
            if (
                data.get("schema_version") != 1
                or data.get("protocol_version") != 1
                or data.get("agent_id") != "desktop"
                or not isinstance(token_hash, str)
                or len(token_hash) != 64
                or len(key) != 32
            ):
                raise ValueError("invalid_agent_configuration")
            int(token_hash, 16)
            return _MachineCredentials("desktop", token_hash.lower(), key, 1)
        except (OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
            return None
