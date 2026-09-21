"""The OpenAI Admin credential, stored apart from the inference credential.

`butters.ai.credentials` says plainly that it holds exactly one credential
class and that "adding a second secret is a separate reviewed change, not a
parameter". This module is that separate change. It deliberately repeats the
storage mechanics rather than parameterising the inference store, because the
guarantee worth having is structural: no call site can ask one store for the
other's secret, and no refactor can accidentally widen one into the other.

This credential is *more* dangerous than the inference key. An OpenAI Admin
API key authenticates the organization Admin surface, which includes endpoints
that create keys, change projects and move spend limits. OpenAI does not offer
a usage-only Admin scope, so the key itself cannot be narrowed. Butters
therefore narrows its *own* use of it: the only client that ever receives this
secret is `provider_accounting.OpenAIAccountingClient`, which can issue GET
requests to three reviewed paths on one host and nothing else.

Differences from the inference credential, all deliberate:

* **No environment fallback.** The inference store falls back to
  `OPENAI_API_KEY` from the unit environment. This one is store-only, so an
  Admin key can never arrive by inheriting a variable.
* **No fingerprint in any projection.** The inference control plane shows a
  fingerprint because an administrator manages several keys there. There is
  one Admin key and nothing to disambiguate, so the fingerprint is computed
  for storage integrity and never leaves this module.
* **Never reaches a provider bundle.** Chat, TTS and STT construct their
  providers from `butters.ai.credentials`; this module is not imported there.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CREDENTIAL_CLASS = "openai_usage_admin_key"
_KEY_FILENAME = "openai-usage-admin-key"
_META_FILENAME = "openai-usage-admin-key.meta.json"
_MIN_LENGTH = 20
_MAX_LENGTH = 512


class UsageAdminCredentialError(RuntimeError):
    """Never carries candidate or stored secret material in its message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fingerprint(secret: str) -> str:
    """A non-reversible identifier, used only inside this module's metadata."""

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def normalize_candidate(raw: object) -> str:
    """Reject anything that is not a plausible bearer credential.

    The candidate is never echoed, not even in a rejection: a length or shape
    complaint that quoted the value would put it in a log the moment an
    administrator pasted the wrong thing.
    """

    if not isinstance(raw, str):
        raise UsageAdminCredentialError(
            "invalid_credential", "the Admin API key must be a string"
        )
    candidate = raw.strip()
    if not candidate:
        raise UsageAdminCredentialError(
            "invalid_credential", "the Admin API key must not be empty"
        )
    if not _MIN_LENGTH <= len(candidate) <= _MAX_LENGTH:
        raise UsageAdminCredentialError(
            "invalid_credential",
            f"the Admin API key must be {_MIN_LENGTH} to {_MAX_LENGTH} characters",
        )
    if any(character.isspace() for character in candidate):
        raise UsageAdminCredentialError(
            "invalid_credential", "the Admin API key must not contain whitespace"
        )
    if not candidate.isascii() or not candidate.isprintable():
        raise UsageAdminCredentialError(
            "invalid_credential", "the Admin API key must be printable ASCII"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class UsageAdminCredentialState:
    """Everything Admin is allowed to know about this credential.

    Note what is absent: the value, and the fingerprint. There is one Admin
    key, so a fingerprint would identify rather than disambiguate.
    """

    configured: bool
    stored_at: float | None = None
    last_validated_at: float | None = None
    last_validation: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "credential_class": CREDENTIAL_CLASS,
            "configured": self.configured,
            # Only ever the Butters store: this credential has no other source.
            "source": "butters_store" if self.configured else "none",
            "stored_at": self.stored_at,
            "last_validated_at": self.last_validated_at,
            "last_validation": self.last_validation,
        }


class UsageAdminCredentialStore:
    """One file, one class, atomic replacement, restrictive permissions."""

    def __init__(
        self,
        state_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(state_dir) / "credentials"
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.key_path = self.directory / _KEY_FILENAME
        self.meta_path = self.directory / _META_FILENAME
        self._clock = clock

    # ----- reads ----------------------------------------------------------

    def secret(self) -> str | None:
        """The stored credential, for the accounting client and nothing else.

        There is no environment fallback by design. An Admin key must be put
        here deliberately; it may not arrive by inheriting a variable the unit
        happens to carry.
        """

        return self._read_stored()

    def configured(self) -> bool:
        return self._read_stored() is not None

    def state(self) -> UsageAdminCredentialState:
        if self._read_stored() is None:
            return UsageAdminCredentialState(False)
        metadata = self._read_metadata()
        return UsageAdminCredentialState(
            True,
            _optional_float(metadata.get("stored_at")),
            _optional_float(metadata.get("last_validated_at")),
            metadata.get("last_validation")
            if isinstance(metadata.get("last_validation"), dict)
            else None,
        )

    # ----- writes ---------------------------------------------------------

    def store(self, secret: str, *, validation: dict[str, object] | None = None) -> None:
        self._atomic_write(self.key_path, secret.encode("utf-8"))
        self._write_metadata(
            {
                "credential_class": CREDENTIAL_CLASS,
                # Held for storage integrity only; never projected outward.
                "fingerprint": fingerprint(secret),
                "stored_at": self._clock(),
                "last_validated_at": None
                if validation is None
                else validation.get("checked_at"),
                "last_validation": validation,
            }
        )

    def record_validation(self, validation: dict[str, object]) -> None:
        metadata = self._read_metadata()
        metadata["last_validated_at"] = validation.get("checked_at")
        metadata["last_validation"] = validation
        self._write_metadata(metadata)

    def remove(self) -> bool:
        """Delete the local copy only; this has no effect at OpenAI."""

        removed = self.key_path.exists()
        self.key_path.unlink(missing_ok=True)
        self.meta_path.unlink(missing_ok=True)
        return removed

    # ----- internals ------------------------------------------------------

    def _read_stored(self) -> str | None:
        try:
            value = self.key_path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageAdminCredentialError(
                "credential_unreadable", "the stored Admin key could not be read"
            ) from exc
        return value or None

    def _read_metadata(self) -> dict[str, object]:
        try:
            raw = self.meta_path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, OSError):
            return {}
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_metadata(self, metadata: dict[str, object]) -> None:
        self._atomic_write(
            self.meta_path,
            json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        )

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"<UsageAdminCredentialStore configured={self.key_path.exists()}>"


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
