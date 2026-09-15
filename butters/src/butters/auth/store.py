"""Bounded persistent WebAuthn ceremony, credential, and elevation state."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from butters.assistant_config import AuthenticationSettings
from butters.skills.model import AuthenticationContext, AuthenticationLevel


class AuthStateError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    record_id: str
    credential_id: bytes
    public_key: bytes
    user_id: bytes
    identity: str
    label: str
    created_at: float
    last_used_at: float | None
    revoked: bool
    sign_count: int
    device_type: str | None
    backed_up: bool | None

    def safe_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "identity": self.identity,
            "label": self.label,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "revoked": self.revoked,
            "device_type": self.device_type,
            "backed_up": self.backed_up,
        }


# The one non-administrator capability this deployment grants. It is named
# explicitly rather than derived so that no future role can widen into
# administrator by accident: `administrator` is decided by AuthPolicy from the
# tailnet identity alone and is never stored, granted, or implied here.
JELLYFIN_ACCESS = "jellyfin_access"
PORTAL_ROLES = frozenset({JELLYFIN_ACCESS})


@dataclass(frozen=True, slots=True)
class PortalIdentityRecord:
    identity: str
    label: str
    roles: frozenset[str]
    created_at: float
    revoked: bool

    def safe_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "label": self.label,
            "roles": sorted(self.roles),
            "created_at": self.created_at,
            "revoked": self.revoked,
        }


@dataclass(frozen=True, slots=True)
class CeremonyRecord:
    ceremony_id: str
    kind: str
    challenge: bytes
    session_id: str
    identity: str
    expires_at: float
    action_digest: str | None
    metadata: dict[str, object]


class AuthStateStore:
    def __init__(
        self,
        database_path: Path,
        settings: AuthenticationSettings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.clock = clock
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS passkey_credentials (
                    record_id TEXT PRIMARY KEY,
                    credential_id BLOB NOT NULL UNIQUE,
                    public_key BLOB NOT NULL,
                    user_id BLOB NOT NULL,
                    identity TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    sign_count INTEGER NOT NULL DEFAULT 0,
                    device_type TEXT,
                    backed_up INTEGER
                );
                CREATE TABLE IF NOT EXISTS auth_ceremonies (
                    ceremony_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    challenge BLOB NOT NULL,
                    session_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    action_digest TEXT,
                    metadata_json TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS browser_elevations (
                    session_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    method TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bootstrap_authorizations (
                    bootstrap_id TEXT PRIMARY KEY,
                    token_hash BLOB NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS portal_identities (
                    identity TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS portal_invites (
                    invite_id TEXT PRIMARY KEY,
                    token_hash BLOB NOT NULL UNIQUE,
                    identity TEXT NOT NULL,
                    label TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS portal_sessions (
                    session_id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fresh_grants (
                    grant_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    subject TEXT,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                """
            )
        self.database_path.chmod(0o600)

    def credential_count(self, *, active_only: bool = True) -> int:
        query = (
            "SELECT COUNT(*) AS count FROM passkey_credentials WHERE revoked=0"
            if active_only
            else "SELECT COUNT(*) AS count FROM passkey_credentials"
        )
        with self._lock, self._connect() as connection:
            row = connection.execute(query).fetchone()
        return int(row["count"])

    def credentials(self, identity: str | None = None) -> tuple[CredentialRecord, ...]:
        query = "SELECT * FROM passkey_credentials"
        values: tuple[object, ...] = ()
        if identity is not None:
            query += " WHERE identity=?"
            values = (identity,)
        query += " ORDER BY created_at, record_id LIMIT ?"
        values += (self.settings.max_credentials,)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(_credential(row) for row in rows)

    def credential_by_encoded_id(self, encoded_id: str) -> CredentialRecord:
        try:
            if not encoded_id or len(encoded_id) > 2048:
                raise ValueError("invalid credential ID length")
            padding = "=" * (-len(encoded_id) % 4)
            credential_id = base64.b64decode(
                encoded_id + padding,
                altchars=b"-_",
                validate=True,
            )
        except (TypeError, ValueError) as exc:
            raise AuthStateError(
                "malformed_credential", "credential ID is invalid"
            ) from exc
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM passkey_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
        if row is None or bool(row["revoked"]):
            raise AuthStateError("credential_denied", "credential is unavailable")
        return _credential(row)

    def add_credential(
        self,
        *,
        credential_id: bytes,
        public_key: bytes,
        user_id: bytes,
        identity: str,
        label: str,
        sign_count: int,
        device_type: str | None,
        backed_up: bool | None,
    ) -> CredentialRecord:
        if self.credential_count(active_only=False) >= self.settings.max_credentials:
            raise AuthStateError("credential_limit", "credential capacity reached")
        now = self.clock()
        record = CredentialRecord(
            secrets.token_urlsafe(18),
            credential_id,
            public_key,
            user_id,
            identity,
            _label(label),
            now,
            None,
            False,
            max(0, int(sign_count)),
            device_type,
            backed_up,
        )
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """INSERT INTO passkey_credentials
                    (record_id,credential_id,public_key,user_id,identity,label,created_at,
                     sign_count,device_type,backed_up)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record.record_id,
                        record.credential_id,
                        record.public_key,
                        record.user_id,
                        record.identity,
                        record.label,
                        record.created_at,
                        record.sign_count,
                        record.device_type,
                        None if backed_up is None else int(backed_up),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthStateError(
                "credential_exists", "credential is already registered"
            ) from exc
        return record

    def update_credential_use(
        self,
        record_id: str,
        *,
        sign_count: int,
        device_type: str | None,
        backed_up: bool | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE passkey_credentials SET last_used_at=?, sign_count=?,
                device_type=?, backed_up=? WHERE record_id=? AND revoked=0""",
                (
                    self.clock(),
                    max(0, int(sign_count)),
                    device_type,
                    None if backed_up is None else int(backed_up),
                    record_id,
                ),
            )

    def label_credential(self, record_id: str, identity: str, label: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE passkey_credentials SET label=? WHERE record_id=? AND identity=?",
                (_label(label), record_id, identity),
            )
        if cursor.rowcount != 1:
            raise AuthStateError("credential_denied", "credential is unavailable")

    def revoke_credential(self, record_id: str, identity: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE passkey_credentials SET revoked=1 WHERE record_id=? AND identity=? AND revoked=0",
                (record_id, identity),
            )
        if cursor.rowcount != 1:
            raise AuthStateError("credential_denied", "credential is unavailable")

    def create_ceremony(
        self,
        *,
        kind: str,
        session_id: str,
        identity: str,
        action_digest: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> CeremonyRecord:
        self.cleanup()
        if self.active_ceremony_count() >= self.settings.max_challenges:
            raise AuthStateError(
                "ceremony_capacity", "authentication ceremony capacity reached"
            )
        now = self.clock()
        record = CeremonyRecord(
            secrets.token_urlsafe(18),
            kind,
            secrets.token_bytes(32),
            session_id,
            identity,
            now + self.settings.challenge_seconds,
            action_digest,
            metadata or {},
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO auth_ceremonies
                (ceremony_id,kind,challenge,session_id,identity,expires_at,action_digest,metadata_json)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    record.ceremony_id,
                    record.kind,
                    record.challenge,
                    record.session_id,
                    record.identity,
                    record.expires_at,
                    record.action_digest,
                    json.dumps(record.metadata, separators=(",", ":")),
                ),
            )
        return record

    def consume_ceremony(
        self, ceremony_id: str, *, kind: str, session_id: str, identity: str
    ) -> CeremonyRecord:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM auth_ceremonies WHERE ceremony_id=?", (ceremony_id,)
            ).fetchone()
            if row is None or bool(row["used"]):
                raise AuthStateError("ceremony_replayed", "ceremony is unavailable")
            if float(row["expires_at"]) <= self.clock():
                connection.execute(
                    "UPDATE auth_ceremonies SET used=1 WHERE ceremony_id=?",
                    (ceremony_id,),
                )
                raise AuthStateError("ceremony_expired", "ceremony expired")
            if row["kind"] != kind:
                raise AuthStateError(
                    "ceremony_mismatch", "ceremony type does not match"
                )
            if row["session_id"] != session_id:
                raise AuthStateError(
                    "ceremony_session_denied", "ceremony belongs to another session"
                )
            if row["identity"] != identity:
                raise AuthStateError(
                    "ceremony_identity_denied", "ceremony belongs to another identity"
                )
            connection.execute(
                "UPDATE auth_ceremonies SET used=1 WHERE ceremony_id=?",
                (ceremony_id,),
            )
        return CeremonyRecord(
            row["ceremony_id"],
            row["kind"],
            bytes(row["challenge"]),
            row["session_id"],
            row["identity"],
            float(row["expires_at"]),
            row["action_digest"],
            json.loads(row["metadata_json"]),
        )

    def active_ceremony_count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM auth_ceremonies WHERE used=0 AND expires_at>?",
                (self.clock(),),
            ).fetchone()
        return int(row["count"])

    def elevate(self, session_id: str, identity: str) -> AuthenticationContext:
        expires = self.clock() + self.settings.elevation_seconds
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO browser_elevations(session_id,identity,expires_at,method)
                VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                identity=excluded.identity, expires_at=excluded.expires_at,
                method=excluded.method""",
                (session_id, identity, expires, "webauthn"),
            )
        return AuthenticationContext(
            AuthenticationLevel.ELEVATED,
            session_id,
            identity,
            expires,
            "webauthn",
        )

    def elevation(self, session_id: str, identity: str) -> AuthenticationContext | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM browser_elevations WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if (
            row is None
            or row["identity"] != identity
            or float(row["expires_at"]) <= self.clock()
        ):
            self.lock(session_id)
            return None
        return AuthenticationContext(
            AuthenticationLevel.ELEVATED,
            session_id,
            identity,
            float(row["expires_at"]),
            row["method"],
        )

    def lock(self, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM browser_elevations WHERE session_id=?", (session_id,)
            )

    def create_bootstrap(self) -> tuple[str, float]:
        if self.credential_count(active_only=False):
            raise AuthStateError(
                "bootstrap_denied", "first-passkey bootstrap is no longer available"
            )
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM bootstrap_authorizations")
            connection.execute(
                """INSERT INTO bootstrap_authorizations
                (bootstrap_id,token_hash,created_at,expires_at) VALUES (?,?,?,?)""",
                (
                    secrets.token_urlsafe(18),
                    _token_hash(token),
                    now,
                    now + self.settings.bootstrap_seconds,
                ),
            )
        return token, now + self.settings.bootstrap_seconds

    def consume_bootstrap(self, token: str) -> None:
        digest = _token_hash(token)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM bootstrap_authorizations WHERE token_hash=?",
                (digest,),
            ).fetchone()
            if row is None or bool(row["used"]):
                raise AuthStateError(
                    "bootstrap_denied", "bootstrap authorization is invalid"
                )
            if float(row["expires_at"]) <= self.clock():
                connection.execute(
                    "UPDATE bootstrap_authorizations SET used=1 WHERE bootstrap_id=?",
                    (row["bootstrap_id"],),
                )
                raise AuthStateError(
                    "bootstrap_expired", "bootstrap authorization expired"
                )
            connection.execute(
                "UPDATE bootstrap_authorizations SET used=1 WHERE bootstrap_id=?",
                (row["bootstrap_id"],),
            )

    def create_fresh_grant(
        self, session_id: str, identity: str, purpose: str, subject: str | None
    ) -> str:
        grant_id = secrets.token_urlsafe(24)
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO fresh_grants
                (grant_id,session_id,identity,purpose,subject,expires_at)
                VALUES (?,?,?,?,?,?)""",
                (grant_id, session_id, identity, purpose, subject, self.clock() + 60),
            )
        return grant_id

    def consume_fresh_grant(
        self, grant_id: str, *, session_id: str, identity: str, purpose: str
    ) -> str | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM fresh_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if (
                row is None
                or bool(row["used"])
                or float(row["expires_at"]) <= self.clock()
                or row["session_id"] != session_id
                or row["identity"] != identity
                or row["purpose"] != purpose
            ):
                raise AuthStateError(
                    "fresh_grant_denied", "fresh authorization is invalid"
                )
            connection.execute(
                "UPDATE fresh_grants SET used=1 WHERE grant_id=?", (grant_id,)
            )
        return row["subject"]


    # ----- portal role assignment -------------------------------------------
    #
    # Roles live beside credentials but never interact with administrator
    # authorization: `AuthPolicy.admin_identity` reads the tailnet identity and
    # the configured administrator list, and consults nothing in this database.
    # A portal role therefore cannot escalate, and an administrator only gains a
    # portal role by being granted one explicitly.

    def portal_identity(self, identity: str) -> PortalIdentityRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM portal_identities WHERE identity=?", (identity,)
            ).fetchone()
        return None if row is None else _portal_identity(row)

    def portal_identities(self) -> tuple[PortalIdentityRecord, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM portal_identities ORDER BY created_at, identity LIMIT ?",
                (self.settings.max_credentials,),
            ).fetchall()
        return tuple(_portal_identity(row) for row in rows)

    def portal_roles(self, identity: str) -> frozenset[str]:
        record = self.portal_identity(identity)
        if record is None or record.revoked:
            return frozenset()
        return record.roles

    def grant_portal_roles(
        self, identity: str, label: str, roles: frozenset[str], *, maximum: int
    ) -> PortalIdentityRecord:
        unknown = roles - PORTAL_ROLES
        if unknown:
            raise AuthStateError("role_denied", "role is not recognised")
        if not roles:
            raise AuthStateError("role_denied", "at least one role is required")
        identity = _identity(identity)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT COUNT(*) AS count FROM portal_identities WHERE identity<>?",
                (identity,),
            ).fetchone()
            if int(existing["count"]) >= maximum:
                raise AuthStateError("identity_limit", "portal identity capacity reached")
            connection.execute(
                """INSERT INTO portal_identities(identity,label,roles_json,created_at,revoked)
                VALUES (?,?,?,?,0) ON CONFLICT(identity) DO UPDATE SET
                label=excluded.label, roles_json=excluded.roles_json, revoked=0""",
                (
                    identity,
                    _label(label),
                    json.dumps(sorted(roles), separators=(",", ":")),
                    self.clock(),
                ),
            )
        record = self.portal_identity(identity)
        assert record is not None
        return record

    def revoke_portal_identity(self, identity: str) -> None:
        """Revoke the role and every live portal session it authorized.

        Credentials are left alone: an administrator who also holds a passkey
        keeps it, and a partner's credential is revoked separately through the
        normal credential path. What ends here is the authorization.
        """

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE portal_identities SET revoked=1 WHERE identity=? AND revoked=0",
                (identity,),
            )
            connection.execute(
                "DELETE FROM portal_sessions WHERE identity=?", (identity,)
            )
            connection.execute(
                "DELETE FROM portal_invites WHERE identity=? AND used=0", (identity,)
            )
        if cursor.rowcount != 1:
            raise AuthStateError("identity_denied", "portal identity is unavailable")

    # ----- enrollment invites ----------------------------------------------

    def create_portal_invite(
        self, identity: str, label: str, roles: frozenset[str], ttl_seconds: float
    ) -> tuple[str, float]:
        """Mint one single-use, expiring enrollment authorization.

        Passkey registration on the portal is never open to an unauthenticated
        visitor. An administrator creates this invite for one named identity,
        and only the holder of the token, arriving as that identity, may
        register a credential.
        """

        unknown = roles - PORTAL_ROLES
        if unknown:
            raise AuthStateError("role_denied", "role is not recognised")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        expires = now + ttl_seconds
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM portal_invites WHERE identity=? AND used=0", (identity,)
            )
            connection.execute(
                """INSERT INTO portal_invites
                (invite_id,token_hash,identity,label,roles_json,created_at,expires_at)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    secrets.token_urlsafe(18),
                    _token_hash(token),
                    _identity(identity),
                    _label(label),
                    json.dumps(sorted(roles), separators=(",", ":")),
                    now,
                    expires,
                ),
            )
        return token, expires

    def consume_portal_invite(self, token: str, identity: str) -> frozenset[str]:
        digest = _token_hash(token)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM portal_invites WHERE token_hash=?", (digest,)
            ).fetchone()
            if row is None or bool(row["used"]) or row["identity"] != identity:
                raise AuthStateError("invite_denied", "enrollment authorization is invalid")
            if float(row["expires_at"]) <= self.clock():
                connection.execute(
                    "UPDATE portal_invites SET used=1 WHERE invite_id=?",
                    (row["invite_id"],),
                )
                raise AuthStateError("invite_expired", "enrollment authorization expired")
            connection.execute(
                "UPDATE portal_invites SET used=1 WHERE invite_id=?", (row["invite_id"],)
            )
        return frozenset(json.loads(row["roles_json"]))

    def portal_invites(self) -> tuple[dict[str, object], ...]:
        now = self.clock()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM portal_invites WHERE used=0 AND expires_at>? "
                "ORDER BY created_at LIMIT ?",
                (now, self.settings.max_credentials),
            ).fetchall()
        return tuple(
            {
                "identity": row["identity"],
                "label": row["label"],
                "roles": sorted(json.loads(row["roles_json"])),
                "expires_at": float(row["expires_at"]),
            }
            for row in rows
        )

    # ----- portal sessions --------------------------------------------------

    def open_portal_session(
        self, session_id: str, identity: str, ttl_seconds: float
    ) -> frozenset[str]:
        roles = self.portal_roles(identity)
        if not roles:
            raise AuthStateError("role_denied", "identity holds no portal role")
        expires = self.clock() + ttl_seconds
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO portal_sessions(session_id,identity,roles_json,expires_at)
                VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                identity=excluded.identity, roles_json=excluded.roles_json,
                expires_at=excluded.expires_at""",
                (
                    session_id,
                    identity,
                    json.dumps(sorted(roles), separators=(",", ":")),
                    expires,
                ),
            )
        return roles

    def portal_session_roles(self, session_id: str, identity: str) -> frozenset[str]:
        """Roles for a live portal session, re-checked against current grants.

        The session row records what was granted at sign-in; the identity's
        current grant decides what still applies. Revocation therefore takes
        effect on the next request rather than at session expiry.
        """

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM portal_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if (
            row is None
            or row["identity"] != identity
            or float(row["expires_at"]) <= self.clock()
        ):
            self.close_portal_session(session_id)
            return frozenset()
        return frozenset(json.loads(row["roles_json"])) & self.portal_roles(identity)

    def close_portal_session(self, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM portal_sessions WHERE session_id=?", (session_id,)
            )

    def cleanup(self) -> None:
        now = self.clock()
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_ceremonies WHERE used=1 OR expires_at<=?", (now,)
            )
            connection.execute(
                "DELETE FROM browser_elevations WHERE expires_at<=?", (now,)
            )
            connection.execute(
                "DELETE FROM fresh_grants WHERE used=1 OR expires_at<=?", (now,)
            )
            connection.execute(
                "DELETE FROM portal_sessions WHERE expires_at<=?", (now,)
            )
            connection.execute(
                "DELETE FROM portal_invites WHERE used=1 OR expires_at<=?", (now,)
            )

    def local_recovery_revoke_all(self) -> None:
        """Local CLI only: erase public credentials and invalidate auth state.

        Removing the records is intentional: it restores the same bootstrap
        precondition as a new installation. Private passkey material never
        existed on this server.
        """

        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM passkey_credentials")
            connection.execute("DELETE FROM browser_elevations")
            connection.execute("DELETE FROM auth_ceremonies")
            connection.execute("DELETE FROM fresh_grants")
            connection.execute("DELETE FROM bootstrap_authorizations")
            connection.execute("DELETE FROM portal_sessions")
            connection.execute("DELETE FROM portal_invites")
            connection.execute("DELETE FROM portal_identities")


def _credential(row: sqlite3.Row) -> CredentialRecord:
    return CredentialRecord(
        row["record_id"],
        bytes(row["credential_id"]),
        bytes(row["public_key"]),
        bytes(row["user_id"]),
        row["identity"],
        row["label"],
        float(row["created_at"]),
        None if row["last_used_at"] is None else float(row["last_used_at"]),
        bool(row["revoked"]),
        int(row["sign_count"]),
        row["device_type"],
        None if row["backed_up"] is None else bool(row["backed_up"]),
    )


def _portal_identity(row: sqlite3.Row) -> PortalIdentityRecord:
    return PortalIdentityRecord(
        row["identity"],
        row["label"],
        frozenset(json.loads(row["roles_json"])),
        float(row["created_at"]),
        bool(row["revoked"]),
    )


def _identity(value: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 160 or not clean.isprintable():
        raise AuthStateError("invalid_identity", "portal identity is invalid")
    return clean


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _label(value: str) -> str:
    clean = " ".join(value.replace("\x00", "").split())[:80]
    if not clean:
        raise AuthStateError("invalid_label", "credential label is required")
    return clean
