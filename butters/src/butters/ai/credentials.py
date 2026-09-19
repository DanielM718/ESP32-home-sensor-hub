"""The one reviewed storage destination for Butters' OpenAI API credential.

This is deliberately not a generic secret store. There is exactly one
credential class (`openai_api_key`), exactly one file, and no Admin request
can name a path, a variable, or another class. Adding a second secret is a
separate reviewed change, not a parameter.

Storage rules:

* the file lives under the daemon's own state directory, which is the only
  path the `butters-web` unit can write (`ReadWritePaths=/var/lib/butters`);
* the directory is 0700 and the file is 0600, created with those modes rather
  than relaxed afterwards;
* writes are atomic (`os.replace` over a same-directory temporary file), so a
  crash mid-write cannot leave a truncated credential behind;
* the secret is returned only to provider construction. Every other surface -
  API responses, metadata, logs, audit records, exceptions, `repr` - carries
  the non-reversible fingerprint and never the value.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CREDENTIAL_CLASS = "openai_api_key"
_KEY_FILENAME = "openai-api-key"
_META_FILENAME = "openai-api-key.meta.json"
_MIN_LENGTH = 20
_MAX_LENGTH = 512
_VALIDATION_TIMEOUT_SECONDS = 15.0
_VALIDATION_RESPONSE_LIMIT = 1024 * 1024


class CredentialError(RuntimeError):
    """Never carries candidate or stored secret material in its message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def fingerprint(secret: str) -> str:
    """Non-reversible short identity for a credential.

    A truncated SHA-256 of a high-entropy key identifies which credential is
    installed across a replacement without disclosing any part of it. No
    prefix or suffix of the key itself is ever retained.
    """

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Separates 'the credential authenticates' from 'this model is available'."""

    authenticated: bool
    code: str
    detail: str
    checked_at: float
    model_available: bool | None = None
    model_checked: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "authenticated": self.authenticated,
            "code": self.code,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "model_available": self.model_available,
            "model_checked": self.model_checked,
        }


@dataclass(frozen=True, slots=True)
class CredentialState:
    """Everything Admin is allowed to know about the credential."""

    configured: bool
    source: str
    fingerprint: str | None = None
    stored_at: float | None = None
    last_validated_at: float | None = None
    last_validation: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "credential_class": CREDENTIAL_CLASS,
            "configured": self.configured,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "stored_at": self.stored_at,
            "last_validated_at": self.last_validated_at,
            "last_validation": self.last_validation,
        }


def normalize_candidate(raw: object) -> str:
    """Reject anything that is not a plausible bearer credential.

    The candidate is never quoted back: an administrator who pastes the wrong
    thing gets the reason, not an echo of what they pasted.
    """

    if not isinstance(raw, str):
        raise CredentialError("invalid_candidate", "the API key must be a string")
    value = raw.strip()
    if not value:
        raise CredentialError("invalid_candidate", "the API key is empty")
    if not _MIN_LENGTH <= len(value) <= _MAX_LENGTH:
        raise CredentialError(
            "invalid_candidate",
            f"the API key must be {_MIN_LENGTH} to {_MAX_LENGTH} characters",
        )
    if any(character.isspace() for character in value):
        raise CredentialError(
            "invalid_candidate", "the API key must not contain whitespace"
        )
    if not all(32 <= ord(character) < 127 for character in value):
        raise CredentialError(
            "invalid_candidate", "the API key must be printable ASCII"
        )
    return value


class OpenAICredentialValidator:
    """Bounded, side-effect-free authentication check.

    `GET /v1/models` is the least expensive authenticated read on the API: it
    creates no file, assistant, fine-tune, or any other upstream object. Model
    availability is asked separately, because 'the key works' and 'this project
    can use that model' are different facts and are reported as such.
    """

    def __init__(
        self,
        base_url: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = _VALIDATION_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._opener = opener
        self._clock = clock
        self._timeout = timeout_seconds

    def validate(self, secret: str, *, model: str | None = None) -> ValidationOutcome:
        authenticated, code, detail = self._probe("/v1/models", secret)
        if not authenticated:
            return ValidationOutcome(False, code, detail, self._clock())
        if model is None:
            return ValidationOutcome(True, "valid", "credential authenticates", self._clock())
        available, _code, _detail = self._probe(f"/v1/models/{model}", secret)
        return ValidationOutcome(
            True,
            "valid",
            "credential authenticates",
            self._clock(),
            model_available=available,
            model_checked=model,
        )

    def _probe(self, path: str, secret: str) -> tuple[bool, str, str]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={
                "Authorization": f"Bearer {secret}",
                "Accept": "application/json",
                "User-Agent": "Butters-Admin/1.0",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                response.read(_VALIDATION_RESPONSE_LIMIT)
        except urllib.error.HTTPError as exc:
            # The upstream body can quote the submitted key back; it is read
            # and discarded here rather than surfaced or logged.
            if exc.code in {401, 403}:
                return False, "unauthorized", "OpenAI rejected the credential"
            if exc.code == 404:
                return False, "not_available", "OpenAI does not expose this model"
            if exc.code == 429:
                return False, "rate_limited", "OpenAI rate limited the check"
            return False, "upstream_status", f"OpenAI returned HTTP {exc.code}"
        except TimeoutError:
            return False, "timeout", "the validation request timed out"
        except (urllib.error.URLError, OSError):
            return False, "unavailable", "OpenAI could not be reached"
        return True, "valid", "OpenAI accepted the credential"


class OpenAICredentialStore:
    """One file, one class, atomic replacement, restrictive permissions."""

    def __init__(
        self,
        state_dir: Path,
        *,
        environment: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(state_dir) / "credentials"
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.key_path = self.directory / _KEY_FILENAME
        self.meta_path = self.directory / _META_FILENAME
        self._environment = os.environ if environment is None else environment
        self._clock = clock

    # ----- reads ----------------------------------------------------------

    def secret(self) -> str | None:
        """The effective credential, stored file first, then the unit environment.

        The deployed unit still loads `OPENAI_API_KEY` from
        `/etc/butters/butters.env`. That path is left exactly as it is: this
        store never reads, copies, or rewrites it, and only takes precedence
        when an administrator has deliberately stored a credential here.
        """

        stored = self._read_stored()
        if stored is not None:
            return stored
        environment_value = self._environment.get("OPENAI_API_KEY", "").strip()
        return environment_value or None

    def source(self) -> str:
        if self._read_stored() is not None:
            return "butters_store"
        if self._environment.get("OPENAI_API_KEY", "").strip():
            return "unit_environment"
        return "none"

    def state(self) -> CredentialState:
        source = self.source()
        if source == "none":
            return CredentialState(False, "none")
        secret = self.secret()
        assert secret is not None
        metadata = self._read_metadata() if source == "butters_store" else {}
        return CredentialState(
            True,
            source,
            fingerprint(secret),
            _optional_float(metadata.get("stored_at")),
            _optional_float(metadata.get("last_validated_at")),
            metadata.get("last_validation")
            if isinstance(metadata.get("last_validation"), dict)
            else None,
        )

    # ----- writes ---------------------------------------------------------

    def store(self, secret: str, *, validation: ValidationOutcome | None) -> None:
        self._atomic_write(self.key_path, secret.encode("utf-8"))
        self._write_metadata(
            {
                "credential_class": CREDENTIAL_CLASS,
                "fingerprint": fingerprint(secret),
                "stored_at": self._clock(),
                "last_validated_at": None if validation is None else validation.checked_at,
                "last_validation": None if validation is None else validation.as_dict(),
            }
        )

    def record_validation(self, validation: ValidationOutcome) -> None:
        metadata = self._read_metadata()
        metadata["last_validated_at"] = validation.checked_at
        metadata["last_validation"] = validation.as_dict()
        self._write_metadata(metadata)

    def remove(self) -> bool:
        """Delete the locally stored credential only.

        This has no effect at OpenAI. The caller is responsible for telling the
        administrator that the key may still exist in their OpenAI account.
        """

        removed = self.key_path.exists()
        self.key_path.unlink(missing_ok=True)
        self.meta_path.unlink(missing_ok=True)
        return removed

    def snapshot(self) -> bytes | None:
        """Exact stored bytes, for rollback after a failed activation."""

        try:
            return self.key_path.read_bytes()
        except FileNotFoundError:
            return None

    def restore(self, snapshot: bytes | None, metadata: dict[str, object] | None) -> None:
        if snapshot is None:
            self.key_path.unlink(missing_ok=True)
            self.meta_path.unlink(missing_ok=True)
            return
        self._atomic_write(self.key_path, snapshot)
        self._write_metadata(metadata or {})

    def metadata_snapshot(self) -> dict[str, object]:
        return self._read_metadata()

    # ----- internals ------------------------------------------------------

    def _read_stored(self) -> str | None:
        try:
            value = self.key_path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, NotADirectoryError):
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise CredentialError(
                "credential_unreadable", "the stored credential could not be read"
            ) from exc
        return value or None

    def _read_metadata(self) -> dict[str, object]:
        try:
            raw = self.meta_path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return {}
        except OSError:
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
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.chmod(path, 0o600)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"OpenAICredentialStore(configured={self.key_path.exists()})"


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
