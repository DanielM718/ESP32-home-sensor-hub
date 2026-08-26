"""Persistent immutable pending plans, bounded jobs, and sanitized audit."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from butters.assistant_config import ActionSettings
from butters.skills.model import AuthenticationLevel


class ActionStateError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FrozenStep:
    skill: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class PendingPlan:
    plan_id: str
    steps: tuple[FrozenStep, ...]
    summary: str
    session_id: str
    identity: str
    request_id: str
    source: str
    created_at: float
    expires_at: float
    authentication: AuthenticationLevel
    nonce: str
    digest: str
    state: str

    def safe_dict(self) -> dict[str, object]:
        return {
            "pending_action_id": self.plan_id,
            "summary": self.summary,
            "steps": [
                {"skill": item.skill, "arguments": item.arguments}
                for item in self.steps
            ],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "authentication": self.authentication.value,
            "state": self.state,
        }


class ActionStateStore:
    STATES = frozenset(
        {
            "pending_auth",
            "pending_confirmation",
            "queued",
            "running",
            "waiting",
            "completed",
            "failed",
            "cancelled",
            "expired",
        }
    )

    def __init__(
        self,
        database_path: Path,
        settings: ActionSettings,
        *,
        pending_seconds: float = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.pending_seconds = pending_seconds
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
                CREATE TABLE IF NOT EXISTS pending_action_plans (
                    plan_id TEXT PRIMARY KEY,
                    steps_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    authentication TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_jobs (
                    job_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    skill TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL,
                    result_json TEXT,
                    failure_code TEXT,
                    failure_reason TEXT,
                    completed_at REAL
                );
                CREATE TABLE IF NOT EXISTS action_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    identity_ref TEXT NOT NULL,
                    session_ref TEXT NOT NULL,
                    skill TEXT NOT NULL,
                    authentication TEXT NOT NULL,
                    method TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    job_id TEXT,
                    elapsed_ms INTEGER,
                    reason_code TEXT
                );
                CREATE TABLE IF NOT EXISTS timed_overrides (
                    device TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    job_id TEXT NOT NULL,
                    release_state TEXT NOT NULL
                );
                """
            )
        self.database_path.chmod(0o600)

    def recover_interrupted_jobs(self, *, local_console: bool) -> int:
        """Fail jobs owned by the restarting execution surface.

        Browser and physical-listener processes may share the database, so a
        process must never mark the other surface's live work as interrupted.
        """

        query = (
            "UPDATE action_jobs SET state='failed', stage='process_restart', "
            "failure_code='restart_recovery_required', completed_at=? "
            "WHERE state IN ('queued','running','waiting') "
            "AND identity = 'local-console'"
            if local_console
            else "UPDATE action_jobs SET state='failed', stage='process_restart', "
            "failure_code='restart_recovery_required', completed_at=? "
            "WHERE state IN ('queued','running','waiting') "
            "AND identity != 'local-console'"
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(query, (self.clock(),))
            return max(0, int(cursor.rowcount))

    def freeze(
        self,
        *,
        steps: tuple[FrozenStep, ...],
        summary: str,
        session_id: str,
        identity: str,
        request_id: str,
        source: str,
        authentication: AuthenticationLevel,
        state: str = "pending_auth",
    ) -> PendingPlan:
        self._trim_plans()
        if not 1 <= len(steps) <= 4:
            raise ActionStateError(
                "plan_limit", "an action plan must have one to four steps"
            )
        if state not in {"pending_auth", "pending_confirmation"}:
            raise ActionStateError("invalid_state", "pending plan state is invalid")
        canonical = json.dumps(
            [{"skill": item.skill, "arguments": item.arguments} for item in steps],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if len(canonical.encode()) > 8192:
            raise ActionStateError("plan_too_large", "action plan exceeds its limit")
        now = self.clock()
        nonce = secrets.token_urlsafe(24)
        digest = hashlib.sha256(
            (
                canonical + "\x00" + session_id + "\x00" + identity + "\x00" + nonce
            ).encode()
        ).hexdigest()
        plan = PendingPlan(
            secrets.token_urlsafe(18),
            steps,
            " ".join(summary.split())[:500],
            session_id,
            identity,
            request_id,
            source,
            now,
            now + self.pending_seconds,
            authentication,
            nonce,
            digest,
            state,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO pending_action_plans
                (plan_id,steps_json,summary,session_id,identity,request_id,source,
                 created_at,expires_at,authentication,nonce,digest,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan.plan_id,
                    canonical,
                    plan.summary,
                    plan.session_id,
                    plan.identity,
                    plan.request_id,
                    plan.source,
                    plan.created_at,
                    plan.expires_at,
                    plan.authentication.value,
                    plan.nonce,
                    plan.digest,
                    plan.state,
                ),
            )
        return plan

    def require(
        self,
        plan_id: str,
        *,
        session_id: str,
        identity: str,
        allowed_states: frozenset[str] = frozenset(
            {"pending_auth", "pending_confirmation"}
        ),
    ) -> PendingPlan:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_action_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
        if row is None:
            raise ActionStateError(
                "pending_action_denied", "pending action is unavailable"
            )
        if row["session_id"] != session_id:
            raise ActionStateError(
                "pending_action_session_denied",
                "pending action belongs to another session",
            )
        if row["identity"] != identity:
            raise ActionStateError(
                "pending_action_identity_denied",
                "pending action belongs to another identity",
            )
        if float(row["expires_at"]) <= self.clock():
            self.set_plan_state(plan_id, "expired")
            raise ActionStateError("pending_action_expired", "pending action expired")
        if row["state"] not in allowed_states:
            raise ActionStateError(
                "pending_action_replayed", "pending action is no longer executable"
            )
        return _plan(row)

    def claim(self, plan: PendingPlan) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE pending_action_plans SET state='queued'
                WHERE plan_id=? AND state=? AND expires_at>?""",
                (plan.plan_id, plan.state, self.clock()),
            )
        if cursor.rowcount != 1:
            raise ActionStateError(
                "pending_action_replayed", "pending action was already used"
            )

    def cancel_plan(self, plan_id: str, *, session_id: str, identity: str) -> None:
        plan = self.require(plan_id, session_id=session_id, identity=identity)
        self.set_plan_state(plan.plan_id, "cancelled")

    def set_plan_state(self, plan_id: str, state: str) -> None:
        if state not in self.STATES:
            raise ValueError("invalid plan state")
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE pending_action_plans SET state=? WHERE plan_id=?",
                (state, plan_id),
            )

    def create_job(self, plan: PendingPlan, step: FrozenStep) -> str:
        self._trim_jobs()
        job_id = secrets.token_urlsafe(18)
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO action_jobs
                (job_id,plan_id,skill,summary,session_id,identity,created_at,state,stage)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    plan.plan_id,
                    step.skill,
                    plan.summary,
                    plan.session_id,
                    plan.identity,
                    self.clock(),
                    "queued",
                    "queued",
                ),
            )
        return job_id

    def update_job(
        self,
        job_id: str,
        *,
        state: str,
        stage: str,
        progress: float | None = None,
        result: dict[str, object] | None = None,
        failure_code: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if state not in self.STATES:
            raise ValueError("invalid job state")
        terminal = state in {"completed", "failed", "cancelled", "expired"}
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE action_jobs SET state=?,stage=?,progress=?,result_json=?,
                failure_code=?,failure_reason=?,completed_at=? WHERE job_id=?""",
                (
                    state,
                    stage[:80],
                    progress,
                    None
                    if result is None
                    else json.dumps(result, separators=(",", ":"), ensure_ascii=True)[
                        :16384
                    ],
                    failure_code,
                    None if failure_reason is None else failure_reason[:300],
                    self.clock() if terminal else None,
                    job_id,
                ),
            )

    def job(self, job_id: str, *, session_id: str, identity: str) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if (
            row is None
            or row["session_id"] != session_id
            or row["identity"] != identity
        ):
            raise ActionStateError("job_denied", "action job is unavailable")
        return _job(row)

    def jobs(self, *, identity: str, limit: int = 50) -> tuple[dict[str, object], ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM action_jobs WHERE identity=?
                ORDER BY created_at DESC LIMIT ?""",
                (identity, max(1, min(limit, 100))),
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def audit(
        self,
        *,
        identity: str,
        session_id: str,
        skill: str,
        authentication: AuthenticationLevel,
        method: str,
        arguments: dict[str, object],
        outcome: str,
        job_id: str | None,
        elapsed_ms: int | None = None,
        reason_code: str | None = None,
    ) -> None:
        safe_args = json.dumps(arguments, sort_keys=True, separators=(",", ":"))[:2048]
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO action_audit
                (timestamp,identity_ref,session_ref,skill,authentication,method,
                 args_json,outcome,job_id,elapsed_ms,reason_code)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.clock(),
                    _reference(identity),
                    _reference(session_id),
                    skill,
                    authentication.value,
                    method,
                    safe_args,
                    outcome,
                    job_id,
                    elapsed_ms,
                    reason_code,
                ),
            )
            connection.execute(
                """DELETE FROM action_audit WHERE audit_id NOT IN
                (SELECT audit_id FROM action_audit ORDER BY audit_id DESC LIMIT ?)""",
                (self.settings.audit_capacity,),
            )

    def audit_entries(self, limit: int = 100) -> tuple[dict[str, object], ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM action_audit ORDER BY audit_id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return tuple(
            {
                "timestamp": row["timestamp"],
                "identity_ref": row["identity_ref"],
                "session_ref": row["session_ref"],
                "skill": row["skill"],
                "authentication": row["authentication"],
                "method": row["method"],
                "arguments": json.loads(row["args_json"]),
                "outcome": row["outcome"],
                "job_id": row["job_id"],
                "elapsed_ms": row["elapsed_ms"],
                "reason_code": row["reason_code"],
            }
            for row in rows
        )

    def set_override(
        self, device: str, state: str, expires_at: float, job_id: str
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO timed_overrides
                (device,state,started_at,expires_at,job_id,release_state)
                VALUES (?,?,?,?,?,'off') ON CONFLICT(device) DO UPDATE SET
                state=excluded.state,started_at=excluded.started_at,
                expires_at=excluded.expires_at,job_id=excluded.job_id""",
                (device, state, self.clock(), expires_at, job_id),
            )

    def overrides(self) -> tuple[dict[str, object], ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM timed_overrides ORDER BY device"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def clear_override(self, device: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM timed_overrides WHERE device=?", (device,))

    def _trim_jobs(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """DELETE FROM action_jobs
                WHERE state IN ('completed','failed','cancelled','expired')
                AND job_id NOT IN
                (SELECT job_id FROM action_jobs ORDER BY created_at DESC LIMIT ?)""",
                (self.settings.job_capacity,),
            )
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM action_jobs"
            ).fetchone()
        if int(row["count"]) >= self.settings.job_capacity:
            raise ActionStateError("job_capacity", "action job capacity reached")

    def _trim_plans(self) -> None:
        now = self.clock()
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE pending_action_plans SET state='expired'
                WHERE state IN ('pending_auth','pending_confirmation') AND expires_at<=?""",
                (now,),
            )
            connection.execute(
                """DELETE FROM pending_action_plans
                WHERE state IN ('completed','failed','cancelled','expired')
                AND plan_id NOT IN
                (SELECT plan_id FROM pending_action_plans
                 ORDER BY created_at DESC LIMIT ?)""",
                (self.settings.job_capacity,),
            )
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM pending_action_plans
                WHERE state IN ('pending_auth','pending_confirmation','queued','running','waiting')"""
            ).fetchone()
        if int(row["count"]) >= self.settings.job_capacity:
            raise ActionStateError(
                "pending_action_capacity", "pending action capacity reached"
            )


def _plan(row: sqlite3.Row) -> PendingPlan:
    return PendingPlan(
        row["plan_id"],
        tuple(
            FrozenStep(str(item["skill"]), dict(item["arguments"]))
            for item in json.loads(row["steps_json"])
        ),
        row["summary"],
        row["session_id"],
        row["identity"],
        row["request_id"],
        row["source"],
        float(row["created_at"]),
        float(row["expires_at"]),
        AuthenticationLevel(row["authentication"]),
        row["nonce"],
        row["digest"],
        row["state"],
    )


def _job(row: sqlite3.Row) -> dict[str, object]:
    return {
        "job_id": row["job_id"],
        "pending_action_id": row["plan_id"],
        "skill": row["skill"],
        "summary": row["summary"],
        "created_at": row["created_at"],
        "state": row["state"],
        "stage": row["stage"],
        "progress": row["progress"],
        "result": None
        if row["result_json"] is None
        else json.loads(row["result_json"]),
        "failure_code": row["failure_code"],
        "failure_reason": row["failure_reason"],
        "completed_at": row["completed_at"],
    }


def _reference(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]
