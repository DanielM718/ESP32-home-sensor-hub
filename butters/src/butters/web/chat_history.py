"""Durable per-identity Chat transcripts.

Butters Chat used to live entirely in `SessionManager`: a restart of
`butters-web` threw every conversation away, and a reload could only recover
whatever the in-memory session still held. This module is the durable half.

WHAT IS STORED, AND WHAT IS DELIBERATELY NOT
--------------------------------------------
Stored: the canonical message text, its role, its order, when it happened,
its trace id, and the small allow-listed block of routing facts the Chat page
already displays beside an assistant answer.

Not stored, ever: rendered HTML (the canonical Markdown is the source of
truth and the browser re-renders it through the same sanitizer), hidden
chain-of-thought (only the provider-generated summary Butters already shows),
audio, credentials, authorization headers, or any provider response object.

The metadata block is an explicit allow-list rather than a passthrough, so a
field added to `ServiceResponse` later cannot silently start being persisted.

WHERE IT LIVES
--------------
Its own database under the existing state directory. Chat transcripts are not
accounting data, not audit data and not credential data, so they do not share
a file with `usage.sqlite3`, `actions.sqlite3` or `security.sqlite3`. Deleting
chat history therefore cannot reach any of those, which is a property of the
file layout rather than of the delete statement.

OWNERSHIP
---------
`owner_key` is the server-computed peer identity of the browser session
(`AuthPolicy.peer_key`). It is never read from a request body, a query string
or a cookie. Every read and every delete is filtered by it, and a conversation
that belongs to someone else is reported as absent rather than as forbidden,
so an identifier cannot be probed for existence.

RETENTION
---------
Conversations expire a rolling number of days after their last *message*.
Opening one does not refresh it, so reading an old transcript cannot keep it
alive forever. Purging is opportunistic: it runs at construction and at the
top of every history operation, which is enough for a single-home service and
needs no daemon of its own.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from butters.web.sessions import normalize_message_text

# One rolling retention window, in days, unless `web.chat_history_retention_days`
# says otherwise.
DEFAULT_RETENTION_DAYS = 30

# Bounds. Every one of these is a ceiling on something a caller could
# otherwise grow without limit: the number of rows one identity can accumulate,
# the size of one history response, and the size of one stored message.
MAX_CONVERSATIONS_PER_OWNER = 100
MAX_CONVERSATIONS_RETURNED = 50
MAX_MESSAGES_PER_CONVERSATION = 200
# Matches the validated composer limit and `SessionManager.add_message`. This
# is deliberately the same number rather than a larger one: durable storage is
# not a reason to start accepting longer messages than the service validates.
MAX_MESSAGE_CHARS = 4000
MAX_TITLE_CHARS = 60
MAX_METADATA_CHARS = 8000
MAX_OWNER_CHARS = 192

# The routing facts the Chat page already renders beside an assistant answer,
# and nothing else. `reasoning_summary` is the provider-generated summary
# Butters intentionally exposes; there is no field here for hidden reasoning,
# and adding one would have to be a deliberate edit to this tuple.
METADATA_FIELDS: tuple[str, ...] = (
    "cloud_used",
    "routing_mode",
    "routing_tier",
    "routing_reason_codes",
    "estimated_complexity",
    "tier_escalated",
    "model",
    "reasoning_effort",
    "reasoning_summary",
)

# Structural guard, mirroring `butters.ai.store`: this table is replicated into
# API responses, so a future field named like a secret must fail loudly.
_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "secret", "token", "authorization", "credential", "password"}
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

DEFAULT_TITLE = "New chat"


class ChatHistoryError(ValueError):
    """A history request that names nothing this identity may act on."""

    def __init__(self, code: str, message: str, status_code: int = 404) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def derive_title(text: str, *, limit: int = MAX_TITLE_CHARS) -> str:
    """Name a conversation after its first user message, locally.

    No model is consulted. A title is a deterministic function of text the
    person already typed, so creating a conversation costs nothing and cannot
    be influenced by anything the assistant later says.
    """

    candidate = ""
    for line in str(text).splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            candidate = collapsed
            break
    if not candidate:
        return DEFAULT_TITLE
    if len(candidate) > limit:
        return candidate[:limit].rstrip() + "…"
    return candidate


def assistant_metadata(response: object) -> dict[str, object] | None:
    """The displayable routing facts of one assistant answer, allow-listed.

    Reads named attributes off a `ServiceResponse`. Anything absent is left
    out, and a response that carries nothing displayable produces no metadata
    row at all rather than a block of nulls.
    """

    collected: dict[str, object] = {}
    for field in METADATA_FIELDS:
        value = getattr(response, field, None)
        if value is None or value is False or value == () or value == []:
            continue
        if isinstance(value, tuple):
            value = list(value)
        collected[field] = value
    return collected or None


class ChatHistoryStore:
    def __init__(
        self,
        database_path: Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_conversations_per_owner: int = MAX_CONVERSATIONS_PER_OWNER,
        max_messages_per_conversation: int = MAX_MESSAGES_PER_CONVERSATION,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(1, int(retention_days))
        self.max_conversations_per_owner = max(1, int(max_conversations_per_owner))
        self.max_messages_per_conversation = max(2, int(max_messages_per_conversation))
        self.clock = clock
        self._lock = threading.RLock()
        self._initialize()
        # Startup is one of the two opportunistic purge points, so a machine
        # that was switched off for a month does not serve expired transcripts
        # to the first page load after it comes back.
        self.purge_expired()

    # ----- schema ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        # SQLite disables foreign keys per connection by default, so the
        # cascade below only exists if this runs on every connection.
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS chat_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    owner_key       TEXT NOT NULL,
                    title           TEXT NOT NULL,
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL,
                    message_count   INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS chat_conversations_owner
                    ON chat_conversations (owner_key, updated_at DESC);
                CREATE INDEX IF NOT EXISTS chat_conversations_updated
                    ON chat_conversations (updated_at);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL
                        REFERENCES chat_conversations (conversation_id)
                        ON DELETE CASCADE,
                    sequence        INTEGER NOT NULL,
                    role            TEXT NOT NULL,
                    text            TEXT NOT NULL,
                    created_at      REAL NOT NULL,
                    trace_id        TEXT,
                    metadata_json   TEXT,
                    UNIQUE (conversation_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS chat_messages_conversation
                    ON chat_messages (conversation_id, sequence);
                """
            )
        # The transcript is the most sensitive thing this service stores on
        # disk. Owner-only, like every other database in the state directory.
        self.database_path.chmod(0o600)

    # ----- retention ------------------------------------------------------

    @property
    def retention_seconds(self) -> float:
        return self.retention_days * 86400.0

    def purge_expired(self) -> int:
        """Drop conversations whose last message is older than the window.

        `updated_at` is last *message* activity. Opening a conversation never
        writes it, so viewing an old transcript does not extend its life.
        Messages go with the conversation through the foreign-key cascade.
        """

        cutoff = self.clock() - self.retention_seconds
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM chat_conversations WHERE updated_at < ?", (cutoff,)
            )
            return max(0, int(cursor.rowcount))

    # ----- writing --------------------------------------------------------

    def start_conversation(self, owner_key: str, first_user_text: str) -> str:
        """Create a conversation titled from the text that started it."""

        owner = _owner(owner_key)
        now = self.clock()
        conversation_id = secrets.token_urlsafe(18)
        self.purge_expired()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO chat_conversations
                (conversation_id, owner_key, title, created_at, updated_at, message_count)
                VALUES (?,?,?,?,?,0)""",
                (conversation_id, owner, derive_title(first_user_text), now, now),
            )
            self._trim_owner(connection, owner)
        return conversation_id

    def append(
        self,
        conversation_id: str,
        owner_key: str,
        role: str,
        text: str,
        *,
        trace_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> bool:
        """Append one message, in order, to a conversation this owner owns.

        Returns False when the conversation is absent or belongs to somebody
        else. A turn is never allowed to fail because history could not be
        written, so callers treat the result as advisory.
        """

        if role not in {"user", "assistant"}:
            raise ChatHistoryError("invalid_role", "conversation role is invalid", 400)
        owner = _owner(owner_key)
        body = normalize_message_text(text, limit=MAX_MESSAGE_CHARS)
        if not body:
            return False
        encoded = _encode_metadata(metadata)
        now = self.clock()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT message_count FROM chat_conversations"
                " WHERE conversation_id=? AND owner_key=?",
                (_identifier(conversation_id), owner),
            ).fetchone()
            if row is None:
                return False
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM chat_messages"
                    " WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO chat_messages
                (conversation_id, sequence, role, text, created_at, trace_id, metadata_json)
                VALUES (?,?,?,?,?,?,?)""",
                (conversation_id, sequence, role, body, now, trace_id, encoded),
            )
            self._trim_messages(connection, conversation_id)
            connection.execute(
                """UPDATE chat_conversations SET updated_at=?,
                message_count=(SELECT COUNT(*) FROM chat_messages WHERE conversation_id=?)
                WHERE conversation_id=?""",
                (now, conversation_id, conversation_id),
            )
        return True

    # ----- reading --------------------------------------------------------

    def conversations(
        self, owner_key: str, *, limit: int = MAX_CONVERSATIONS_RETURNED
    ) -> tuple[dict[str, object], ...]:
        """This identity's conversations, newest activity first, bounded."""

        self.purge_expired()
        bounded = max(1, min(int(limit), MAX_CONVERSATIONS_RETURNED))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT conversation_id, title, created_at, updated_at, message_count
                FROM chat_conversations WHERE owner_key=?
                ORDER BY updated_at DESC, conversation_id DESC LIMIT ?""",
                (_owner(owner_key), bounded),
            ).fetchall()
        return tuple(
            {
                "conversation_id": row["conversation_id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "message_count": int(row["message_count"]),
            }
            for row in rows
        )

    def conversation(
        self, conversation_id: str, owner_key: str
    ) -> dict[str, object] | None:
        """One conversation and its messages, or None if this owner has none.

        A conversation belonging to another identity is reported exactly as an
        absent one, so an identifier reveals nothing by being guessed.
        """

        self.purge_expired()
        identifier = _identifier(conversation_id)
        owner = _owner(owner_key)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT conversation_id, title, created_at, updated_at, message_count
                FROM chat_conversations WHERE conversation_id=? AND owner_key=?""",
                (identifier, owner),
            ).fetchone()
            if row is None:
                return None
            messages = connection.execute(
                """SELECT role, text, created_at, trace_id, metadata_json
                FROM chat_messages WHERE conversation_id=?
                ORDER BY sequence ASC""",
                (identifier,),
            ).fetchall()
        return {
            "conversation_id": row["conversation_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": int(row["message_count"]),
            "messages": [
                {
                    "role": item["role"],
                    "text": item["text"],
                    "created_at": item["created_at"],
                    "trace_id": item["trace_id"],
                    "metadata": _decode_metadata(item["metadata_json"]),
                }
                for item in messages
            ],
        }

    def owns(self, conversation_id: str | None, owner_key: str) -> bool:
        if not valid_conversation_id(conversation_id):
            return False
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM chat_conversations WHERE conversation_id=? AND owner_key=?",
                (str(conversation_id), _owner(owner_key)),
            ).fetchone()
        return row is not None

    # ----- deleting -------------------------------------------------------

    def delete(self, conversation_id: str, owner_key: str) -> bool:
        """Delete one conversation of this identity, messages included."""

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM chat_conversations WHERE conversation_id=? AND owner_key=?",
                (_identifier(conversation_id), _owner(owner_key)),
            )
            return cursor.rowcount > 0

    def delete_all(self, owner_key: str) -> int:
        """Delete every conversation of this identity, and nobody else's."""

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM chat_conversations WHERE owner_key=?", (_owner(owner_key),)
            )
            return max(0, int(cursor.rowcount))

    # ----- internals ------------------------------------------------------

    def _trim_owner(self, connection: sqlite3.Connection, owner: str) -> None:
        """Keep one identity's conversation count under its ceiling."""

        connection.execute(
            """DELETE FROM chat_conversations WHERE conversation_id IN (
                SELECT conversation_id FROM chat_conversations WHERE owner_key=?
                ORDER BY updated_at DESC, conversation_id DESC LIMIT -1 OFFSET ?
            )""",
            (owner, self.max_conversations_per_owner),
        )

    def _trim_messages(self, connection: sqlite3.Connection, conversation_id: str) -> None:
        """Keep one transcript under its ceiling, dropping the oldest first."""

        connection.execute(
            """DELETE FROM chat_messages WHERE message_id IN (
                SELECT message_id FROM chat_messages WHERE conversation_id=?
                ORDER BY sequence DESC LIMIT -1 OFFSET ?
            )""",
            (conversation_id, self.max_messages_per_conversation),
        )


def valid_conversation_id(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.match(value))


def _identifier(value: object) -> str:
    if not valid_conversation_id(value):
        raise ChatHistoryError("conversation_not_found", "conversation was not found")
    return str(value)


def _owner(value: object) -> str:
    owner = str(value or "")[:MAX_OWNER_CHARS]
    if not owner:
        # An unattributable transcript has no owner to scope reads to, so it is
        # refused rather than written under a shared empty key.
        raise ChatHistoryError("owner_required", "conversation owner is required", 403)
    return owner


def _encode_metadata(metadata: dict[str, object] | None) -> str | None:
    if not metadata:
        return None
    leaked = _SECRET_KEYS.intersection(str(key).casefold() for key in metadata)
    if leaked:
        raise ChatHistoryError(
            "secret_rejected", "chat metadata must not carry credential material", 400
        )
    filtered = {key: metadata[key] for key in METADATA_FIELDS if key in metadata}
    if not filtered:
        return None
    encoded = json.dumps(filtered, separators=(",", ":"), sort_keys=True)
    if len(encoded) > MAX_METADATA_CHARS:
        # Fail closed on size rather than storing a truncated, unparseable
        # block: the answer itself is what matters, the badge line is not.
        return None
    return encoded


def _decode_metadata(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return {key: value[key] for key in METADATA_FIELDS if key in value} or None
