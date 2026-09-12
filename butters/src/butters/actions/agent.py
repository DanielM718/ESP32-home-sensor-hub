"""Authenticated, observer-only Desktop Agent connection hub.

This module deliberately has no command, invoke, request, or send API.  Its
only outbound protocol frame is the signed connection welcome; after that it
accepts signed heartbeats and turns them into independent state facets.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import threading
import time
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


class AgentHub:
    """Own one authenticated machine connection and expose passive state only."""

    def __init__(
        self,
        settings: AgentIngressSettings,
        *,
        monotonic: Any = time.monotonic,
        wall_clock: Any = time.time,
    ) -> None:
        self.settings = settings
        self._state_lock = threading.Lock()
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
                "desktop.agent",
                agent_state,
                agent_confidence,
                observed_at,
                age,
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
            f.name for f in facets if f.confidence in {"unknown", "stale"}
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

    async def socket(self, websocket: Any) -> None:
        """Authenticate the machine hello, then receive signed heartbeats only."""

        # Browsers always send Origin for a WebSocket.  Reject before accepting
        # so browser/Tailscale/passkey identity can never enter this protocol.
        if not self.configured or websocket.headers.get("origin") is not None:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        current = False
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
                self._socket = websocket
                self.connection_id = secrets.token_hex(32)
                self.connected_at = self._wall_clock()
                self.last_authenticated_activity = None
                self._last_authenticated_wall = None
                self._session = {}
                self.version = hello["version"]
                self.reconnect_count += 1
                self.reason = "awaiting_heartbeat"
                connection_id = self.connection_id
            current = True

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
            while self._is_current(websocket):
                raw = await asyncio.wait_for(
                    websocket.receive_text(), self.settings.socket_idle_seconds
                )
                frame = protocol.verify(
                    protocol.decode(raw),
                    self._credentials.command_key,
                    connection_id,
                )
                expected = {
                    "type",
                    "connection_id",
                    "issued_at",
                    "sig",
                    "seq",
                    "session",
                    "version",
                }
                if set(frame) != expected or frame.get("type") != "heartbeat":
                    raise protocol.ProtocolError("malformed_message")
                if frame.get("version") != self.version:
                    raise protocol.ProtocolError("protocol_mismatch")
                seq = frame.get("seq")
                session = frame.get("session")
                if type(seq) is not int or seq <= sequence or type(session) is not dict:
                    raise protocol.ProtocolError("replayed_message")
                if (
                    set(session)
                    - {
                        "state",
                        "gui_launch",
                        "interactive_session",
                        "session_id",
                        "observed_at",
                    }
                    or session.get("state")
                    not in {"ACTIVE", "LOCKED", "NONE", "MULTIPLE"}
                    or type(session.get("gui_launch")) is not bool
                    or type(session.get("interactive_session")) is not bool
                ):
                    raise protocol.ProtocolError("malformed_message")
                with self._state_lock:
                    if self._socket is not websocket:
                        break
                    self._session = dict(session)
                    self.last_authenticated_activity = self._monotonic()
                    self._last_authenticated_wall = self._wall_clock()
                    self.reason = "agent_connected"
                sequence = seq
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - frames and secrets are never logged.
            protocol_error = self._protocol().ProtocolError
            reason = (
                str(exc) if isinstance(exc, protocol_error) else type(exc).__name__
            )
            with self._state_lock:
                if current and self._socket is websocket:
                    self.reason = reason
        finally:
            if current:
                self._disconnect_if_current(websocket)
            with suppress(Exception):
                await websocket.close(code=1008)

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

    def _disconnect_if_current(self, websocket: Any) -> None:
        with self._state_lock:
            if self._socket is not websocket:
                return
            reason = self.reason
            self._socket = None
            self.connection_id = None
            self.connected_at = None
            self.last_authenticated_activity = None
            self._last_authenticated_wall = None
            self._session = {}
            self.version = None
            self.reason = reason

    def _is_current(self, websocket: Any) -> bool:
        with self._state_lock:
            return self._socket is websocket

    @staticmethod
    def _protocol() -> Any:
        # Default-disabled Butters remains importable before the standalone
        # package is installed.  Enabling the feature makes this dependency
        # mandatory and the installer verifies it.
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
