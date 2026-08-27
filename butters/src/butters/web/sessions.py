"""Bounded in-memory browser conversations; no long-term transcript store."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from butters.routing.model import PendingClarification


ANONYMOUS_PEER = "peer:unknown"


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    role: str
    text: str
    trace_id: str | None
    created_monotonic: float


@dataclass(slots=True)
class BrowserSession:
    session_id: str
    csrf_token: str
    created_monotonic: float
    last_active_monotonic: float
    messages: list[ConversationMessage] = field(default_factory=list)
    peer_key: str = ANONYMOUS_PEER
    administrator: bool = False
    pending_clarification: PendingClarification | None = None
    # Monotonic browser interaction number. Shipped browser clients attach one
    # number to every text turn, voice turn, and Clear request so an older
    # request that waited behind newer work cannot mutate the conversation.
    interaction_generation: int = 0
    turn_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class SessionError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class SessionManager:
    """Bounded session pool with per-peer fairness and an administrator reserve.

    Capacity is never freed by evicting a live conversation. A flood is refused
    at admission instead, so one tailnet caller cannot displace another or lock
    the operator out of the administrative surface.
    """

    def __init__(
        self,
        *,
        max_active: int = 32,
        ttl_seconds: float = 1800.0,
        max_messages: int = 24,
        max_context_chars: int = 12000,
        max_per_peer: int = 4,
        admin_reserve: int = 4,
        clock: Callable[[], float] = time.monotonic,
        on_expire: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self.max_active = max_active
        self.ttl_seconds = ttl_seconds
        self.max_messages = max_messages
        self.max_context_chars = max_context_chars
        self.max_per_peer = max(1, min(max_per_peer, max_active))
        self.admin_reserve = max(0, min(admin_reserve, max(0, max_active - 1)))
        self.clock = clock
        self.on_expire = on_expire
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.RLock()

    def create(
        self,
        *,
        peer_key: str = ANONYMOUS_PEER,
        administrator: bool = False,
    ) -> BrowserSession:
        self.expire()
        with self._lock:
            key = str(peer_key)[:192] or ANONYMOUS_PEER
            owned = sum(1 for item in self._sessions.values() if item.peer_key == key)
            if owned >= self.max_per_peer:
                raise SessionError(
                    "peer_session_limit",
                    "this caller already holds the maximum number of conversations",
                    429,
                )
            # Anonymous callers may only fill the unreserved part of the pool.
            ceiling = self.max_active if administrator else self.max_active - self.admin_reserve
            if len(self._sessions) >= ceiling:
                raise SessionError("session_capacity", "too many active sessions", 503)
            now = self.clock()
            session = BrowserSession(
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(24),
                now,
                now,
                peer_key=key,
                administrator=administrator,
            )
            self._sessions[session.session_id] = session
            return session

    def get(self, session_id: str | None, *, touch: bool = True) -> BrowserSession | None:
        if not self.valid_identifier(session_id):
            return None
        with self._lock:
            session = self._sessions.get(str(session_id))
            if session is None:
                return None
            now = self.clock()
            if now - session.last_active_monotonic >= self.ttl_seconds:
                self._sessions.pop(session.session_id, None)
                self._notify((session.session_id,))
                return None
            if touch:
                session.last_active_monotonic = now
            return session

    def require(self, session_id: str | None) -> BrowserSession:
        session = self.get(session_id)
        if session is None:
            raise SessionError("invalid_session", "browser session is invalid or expired")
        return session

    def add_message(
        self,
        session: BrowserSession,
        role: str,
        text: str,
        trace_id: str | None = None,
    ) -> None:
        if role not in {"user", "assistant"}:
            raise SessionError("invalid_role", "conversation role is invalid", 400)
        clean = " ".join(text.replace("\x00", "").split())[:4000]
        if not clean:
            return
        with self._lock:
            session.messages.append(ConversationMessage(role, clean, trace_id, self.clock()))
            if len(session.messages) > self.max_messages:
                del session.messages[: len(session.messages) - self.max_messages]
            while sum(len(item.text) for item in session.messages) > self.max_context_chars:
                session.messages.pop(0)
            session.last_active_monotonic = self.clock()

    def context(self, session: BrowserSession, *, max_messages: int = 8, max_chars: int = 8000) -> tuple[dict[str, str], ...]:
        selected: list[ConversationMessage] = []
        size = 0
        for item in reversed(session.messages):
            if len(selected) >= max_messages or size + len(item.text) > max_chars:
                break
            selected.append(item)
            size += len(item.text)
        return tuple(
            {"role": item.role, "content": item.text}
            for item in reversed(selected)
        )

    def clear(self, session: BrowserSession, *, rotate_csrf: bool = True) -> None:
        with self._lock:
            session.messages.clear()
            session.pending_clarification = None
            if rotate_csrf:
                session.csrf_token = secrets.token_urlsafe(24)
            session.last_active_monotonic = self.clock()

    def expire(self) -> int:
        now = self.clock()
        with self._lock:
            expired = tuple(
                key
                for key, session in self._sessions.items()
                if now - session.last_active_monotonic >= self.ttl_seconds
            )
            for key in expired:
                self._sessions.pop(key, None)
        self._notify(expired)
        return len(expired)

    def summaries(self) -> tuple[dict[str, object], ...]:
        self.expire()
        now = self.clock()
        with self._lock:
            return tuple(
                {
                    "session_id": item.session_id,
                    "age_seconds": round(now - item.created_monotonic, 1),
                    "idle_seconds": round(now - item.last_active_monotonic, 1),
                    "message_count": len(item.messages),
                    "context_chars": sum(len(message.text) for message in item.messages),
                    "administrator": item.administrator,
                }
                for item in self._sessions.values()
            )

    def capacity(self) -> dict[str, int]:
        with self._lock:
            active = len(self._sessions)
        return {
            "active": active,
            "max_active": self.max_active,
            "admin_reserve": self.admin_reserve,
            "max_per_peer": self.max_per_peer,
        }

    def _notify(self, expired: tuple[str, ...]) -> None:
        if expired and self.on_expire is not None:
            self.on_expire(expired)

    @staticmethod
    def valid_identifier(value: object) -> bool:
        return (
            isinstance(value, str)
            and 32 <= len(value) <= 128
            and all(character.isalnum() or character in "-_" for character in value)
        )
