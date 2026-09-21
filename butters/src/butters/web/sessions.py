"""Bounded in-memory browser conversations, cached in front of the store.

This layer is still the working set: the bounded, per-session window the model
is shown and the browser renders during one visit. It is no longer the only
copy. `butters.web.chat_history` holds the durable transcript, and a session
carries the identifier of the conversation it is currently writing to.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from butters.routing.model import PendingClarification

ANONYMOUS_PEER = "peer:unknown"

# C0 controls have no place in conversation text. Tab and newline do: an
# assistant answer is Markdown, and Markdown is a whitespace-significant
# format - collapsing its newlines would turn a heading, a list and a table
# back into one run-on paragraph when the page reloads and rebuilds the
# conversation from here.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_RUN = re.compile(r"\n{3,}")


def normalize_message_text(text: str, *, limit: int) -> str:
    """Bound and de-control one conversation message without reflowing it.

    Line structure and leading indentation survive, because both carry
    meaning in Markdown - indentation is how a fenced block's contents and a
    nested list item are written. Trailing spaces, stray carriage returns and
    long runs of blank lines do not survive, because they carry none.
    """

    without_controls = _CONTROL_CHARACTERS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in without_controls.split("\n")]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()[:limit]


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
    # The durable conversation this browser session is currently writing to,
    # or None before its first message and after "New chat". The identifier is
    # assigned by the server and is only ever bound after an ownership check;
    # it is never taken from a request body.
    conversation_id: str | None = None
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
        clean = normalize_message_text(text, limit=4000)
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

    def replace_messages(
        self,
        session: BrowserSession,
        messages: tuple[tuple[str, str, str | None], ...],
    ) -> None:
        """Refill the in-memory context buffer from a durable transcript.

        Reopening an old conversation should let the next turn continue it, so
        the bounded working set the model sees is rebuilt from the stored
        messages under exactly the same caps a live conversation obeys.
        """

        with self._lock:
            session.messages.clear()
            for role, text, trace_id in messages:
                if role not in {"user", "assistant"}:
                    continue
                clean = normalize_message_text(text, limit=4000)
                if clean:
                    session.messages.append(
                        ConversationMessage(role, clean, trace_id, self.clock())
                    )
            if len(session.messages) > self.max_messages:
                del session.messages[: len(session.messages) - self.max_messages]
            while (
                len(session.messages) > 1
                and sum(len(item.text) for item in session.messages)
                > self.max_context_chars
            ):
                session.messages.pop(0)
            session.last_active_monotonic = self.clock()

    def clear(self, session: BrowserSession, *, rotate_csrf: bool = True) -> None:
        with self._lock:
            session.messages.clear()
            session.pending_clarification = None
            # Detach from the durable conversation without deleting it: a new
            # chat starts a new transcript, it does not destroy the old one.
            session.conversation_id = None
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
