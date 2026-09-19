"""Durable Admin AI/TTS settings, kept per provider.

Switching provider must not silently keep an incompatible model or voice, but
it also must not destroy a working configuration. Each provider therefore owns
its own row. Switching away leaves that row untouched and switching back
restores exactly the configuration the administrator last validated.

No secret is stored here. The credential lives in its own file store; this
database holds provider, model, voice, and bounded numeric controls only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from butters.ai.capabilities import CapabilityError, CapabilityRegistry
from butters.ai.model import (
    ChatSettings,
    SpeechSettings,
    clear_unsupported,
    default_chat,
    default_speech,
    validate_chat,
    validate_speech,
)

CHAT = "chat"
SPEECH = "speech"
_SECRET_KEYS = frozenset({"api_key", "apikey", "secret", "token", "authorization"})


class AISettingsStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ai_provider_profiles (
                    kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (kind, provider)
                );
                CREATE TABLE IF NOT EXISTS ai_active_provider (
                    kind TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    # ----- chat -----------------------------------------------------------

    def chat(self, registry: CapabilityRegistry, fallback_model: str) -> ChatSettings:
        provider = self._active(CHAT)
        default = default_chat(registry, fallback_model)
        if provider is None:
            return default
        stored = self._profile(CHAT, provider)
        if stored is None:
            return default
        try:
            return clear_unsupported(registry, validate_chat(registry, stored))
        except CapabilityError:
            # A stored profile whose provider or model no longer exists is not
            # silently repaired into a different model: the reviewed default
            # is returned instead, and Admin shows what is actually effective.
            return default

    def save_chat(self, settings: ChatSettings) -> None:
        self._write(CHAT, settings.provider, settings.as_dict())

    # ----- speech ---------------------------------------------------------

    def speech(self, registry: CapabilityRegistry, fallback_provider: str) -> SpeechSettings:
        provider = self._active(SPEECH) or fallback_provider
        try:
            default = default_speech(registry, provider)
        except CapabilityError:
            default = default_speech(registry, registry.speech_providers()[0].id)
        stored = self._profile(SPEECH, provider)
        if stored is None:
            return default
        try:
            return validate_speech(registry, stored)
        except CapabilityError:
            return default

    def save_speech(self, settings: SpeechSettings) -> None:
        self._write(SPEECH, settings.provider, settings.as_dict())

    # ----- introspection --------------------------------------------------

    def profiles(self, kind: str) -> dict[str, dict[str, object]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT provider, payload FROM ai_provider_profiles WHERE kind=?",
                (kind,),
            ).fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}

    # ----- internals ------------------------------------------------------

    def _active(self, kind: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT provider FROM ai_active_provider WHERE kind=?", (kind,)
            ).fetchone()
        return None if row is None else str(row[0])

    def _profile(self, kind: str, provider: str) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM ai_provider_profiles WHERE kind=? AND provider=?",
                (kind, provider),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _write(self, kind: str, provider: str, payload: dict[str, object]) -> None:
        leaked = _SECRET_KEYS.intersection(key.casefold() for key in payload)
        if leaked:
            # Structural guard: this table is replicated into API responses, so
            # a future field named like a secret must fail loudly here.
            raise CapabilityError(
                "secret_rejected", "AI settings must not carry credential material"
            )
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO ai_provider_profiles (kind, provider, payload)
                VALUES (?,?,?)
                ON CONFLICT(kind, provider) DO UPDATE SET
                payload=excluded.payload, updated_at=CURRENT_TIMESTAMP""",
                (kind, provider, encoded),
            )
            connection.execute(
                """INSERT INTO ai_active_provider (kind, provider) VALUES (?,?)
                ON CONFLICT(kind) DO UPDATE SET
                provider=excluded.provider, updated_at=CURRENT_TIMESTAMP""",
                (kind, provider),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
