"""SQLite persistence for monitoring sessions and their shared export jobs."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.workflows import (
    ExportRequest,
    MonitoringRequest,
    WorkflowConflictError,
    WorkflowNotFoundError,
    iso_utc,
    json_sources,
)


class MonitoringExportStore:
    """Small transactional repository with one SQLite connection per operation."""

    def __init__(self, database_path: Path, output_dir: Path) -> None:
        self.database_path = Path(database_path)
        self.output_dir = Path(output_dir)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.output_dir, 0o700)
        except OSError:
            pass
        lock_path = self.database_path.with_name(
            self.database_path.name + ".schema.lock"
        )
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            with self._connect() as connection:
                schema_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS monitoring_sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'stopped')),
                    start_time_utc TEXT NOT NULL,
                    scheduled_end_time_utc TEXT NOT NULL,
                    actual_end_time_utc TEXT,
                    selected_sources_json TEXT NOT NULL,
                    selected_fields_json TEXT NOT NULL,
                    csv_format TEXT NOT NULL CHECK (csv_format IN ('long', 'wide')),
                    resolution TEXT NOT NULL CHECK (resolution IN ('raw', '1m', '5m', '15m', '1h')),
                    automatic_export_job_id TEXT UNIQUE,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS export_jobs (
                    id TEXT PRIMARY KEY,
                    monitoring_session_id TEXT UNIQUE,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'completed', 'failed',
                                   'cancel_requested', 'cancelled')
                    ),
                    start_time_utc TEXT NOT NULL,
                    end_time_utc TEXT NOT NULL,
                    selected_sources_json TEXT NOT NULL,
                    selected_fields_json TEXT NOT NULL,
                    resolution TEXT NOT NULL CHECK (resolution IN ('raw', '1m', '5m', '15m', '1h')),
                    csv_format TEXT NOT NULL CHECK (csv_format IN ('long', 'wide')),
                    output_path TEXT NOT NULL,
                    output_size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (output_size_bytes >= 0),
                    rows_written INTEGER NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
                    work_units_total INTEGER NOT NULL DEFAULT 0 CHECK (work_units_total >= 0),
                    work_units_completed INTEGER NOT NULL DEFAULT 0 CHECK (work_units_completed >= 0),
                    current_phase TEXT NOT NULL DEFAULT 'queued',
                    warning_json TEXT NOT NULL DEFAULT '[]',
                    source_results_json TEXT NOT NULL DEFAULT '[]',
                    error_message TEXT,
                    worker_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    created_at_utc TEXT NOT NULL,
                    started_at_utc TEXT,
                    completed_at_utc TEXT,
                    heartbeat_at_utc TEXT,
                    updated_at_utc TEXT NOT NULL,
                    FOREIGN KEY (monitoring_session_id)
                        REFERENCES monitoring_sessions(id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_monitoring_status_end
                    ON monitoring_sessions(status, scheduled_end_time_utc);
                CREATE INDEX IF NOT EXISTS idx_monitoring_created
                    ON monitoring_sessions(created_at_utc DESC);
                CREATE INDEX IF NOT EXISTS idx_export_claim
                    ON export_jobs(status, created_at_utc);
                CREATE INDEX IF NOT EXISTS idx_export_heartbeat
                    ON export_jobs(status, heartbeat_at_utc);
                """
                )
                monitoring_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(monitoring_sessions)"
                    ).fetchall()
                }
                if schema_version == 1 and "trigger_source" not in monitoring_columns:
                    self._migrate_resolution_constraints(connection)
                self._ensure_monitoring_metadata_columns(connection)
                connection.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS idx_monitoring_printer_session
                       ON monitoring_sessions(printer_session_id)
                       WHERE printer_session_id IS NOT NULL"""
                )
                connection.execute("PRAGMA user_version = 3")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_resolution_constraints(connection: sqlite3.Connection) -> None:
        """Expand v1 resolution checks without losing sessions or export jobs."""

        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE monitoring_sessions RENAME TO monitoring_sessions_v1;
                ALTER TABLE export_jobs RENAME TO export_jobs_v1;

                CREATE TABLE monitoring_sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'stopped')),
                    start_time_utc TEXT NOT NULL,
                    scheduled_end_time_utc TEXT NOT NULL,
                    actual_end_time_utc TEXT,
                    selected_sources_json TEXT NOT NULL,
                    selected_fields_json TEXT NOT NULL,
                    csv_format TEXT NOT NULL CHECK (csv_format IN ('long', 'wide')),
                    resolution TEXT NOT NULL CHECK (resolution IN ('raw', '1m', '5m', '15m', '1h')),
                    automatic_export_job_id TEXT UNIQUE,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE export_jobs (
                    id TEXT PRIMARY KEY,
                    monitoring_session_id TEXT UNIQUE,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'completed', 'failed',
                                   'cancel_requested', 'cancelled')
                    ),
                    start_time_utc TEXT NOT NULL,
                    end_time_utc TEXT NOT NULL,
                    selected_sources_json TEXT NOT NULL,
                    selected_fields_json TEXT NOT NULL,
                    resolution TEXT NOT NULL CHECK (resolution IN ('raw', '1m', '5m', '15m', '1h')),
                    csv_format TEXT NOT NULL CHECK (csv_format IN ('long', 'wide')),
                    output_path TEXT NOT NULL,
                    output_size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (output_size_bytes >= 0),
                    rows_written INTEGER NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
                    work_units_total INTEGER NOT NULL DEFAULT 0 CHECK (work_units_total >= 0),
                    work_units_completed INTEGER NOT NULL DEFAULT 0 CHECK (work_units_completed >= 0),
                    current_phase TEXT NOT NULL DEFAULT 'queued',
                    warning_json TEXT NOT NULL DEFAULT '[]',
                    source_results_json TEXT NOT NULL DEFAULT '[]',
                    error_message TEXT,
                    worker_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    created_at_utc TEXT NOT NULL,
                    started_at_utc TEXT,
                    completed_at_utc TEXT,
                    heartbeat_at_utc TEXT,
                    updated_at_utc TEXT NOT NULL,
                    FOREIGN KEY (monitoring_session_id)
                        REFERENCES monitoring_sessions(id) ON DELETE RESTRICT
                );

                INSERT INTO monitoring_sessions SELECT * FROM monitoring_sessions_v1;
                INSERT INTO export_jobs SELECT * FROM export_jobs_v1;
                DROP TABLE export_jobs_v1;
                DROP TABLE monitoring_sessions_v1;

                CREATE INDEX idx_monitoring_status_end
                    ON monitoring_sessions(status, scheduled_end_time_utc);
                CREATE INDEX idx_monitoring_created
                    ON monitoring_sessions(created_at_utc DESC);
                CREATE INDEX idx_export_claim
                    ON export_jobs(status, created_at_utc);
                CREATE INDEX idx_export_heartbeat
                    ON export_jobs(status, heartbeat_at_utc);
                COMMIT;
                """
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _ensure_monitoring_metadata_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(monitoring_sessions)"
            ).fetchall()
        }
        additions = {
            "trigger_source": "TEXT NOT NULL DEFAULT 'manual'",
            "printer_session_id": "TEXT",
            "printer_ended_at_utc": "TEXT",
            "recovery_end_time_utc": "TEXT",
            "auto_close_on_schedule": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE monitoring_sessions ADD COLUMN {name} {declaration}"
                )

    def create_session(
        self,
        request: MonitoringRequest,
        *,
        start_time: datetime,
        scheduled_end_time: datetime,
    ) -> dict[str, Any]:
        session_id = str(uuid4())
        now_text = iso_utc(start_time)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO monitoring_sessions (
                    id, name, notes, status, start_time_utc,
                    scheduled_end_time_utc, actual_end_time_utc,
                    selected_sources_json, selected_fields_json,
                    csv_format, resolution, automatic_export_job_id,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, 'running', ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    session_id,
                    request.name,
                    request.notes,
                    now_text,
                    iso_utc(scheduled_end_time),
                    _json(json_sources(request.sources)),
                    _json(list(request.fields)),
                    request.csv_format,
                    request.resolution,
                    now_text,
                    now_text,
                ),
            )
        return self.get_session(session_id)

    def list_sessions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM monitoring_sessions
                ORDER BY created_at_utc DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [_session_dict(row) for row in rows]

    def ensure_printer_session(
        self,
        *,
        printer_session_id: str,
        name: str,
        start_time: datetime,
        provisional_end_time: datetime,
        sources: Sequence[Mapping[str, Any]],
        fields: Sequence[str],
    ) -> dict[str, Any]:
        """Idempotently associate one monitoring interval with one print."""

        now_text = iso_utc(start_time)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM monitoring_sessions WHERE printer_session_id=?",
                (printer_session_id,),
            ).fetchone()
            if existing is None:
                session_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO monitoring_sessions (
                        id, name, notes, status, start_time_utc,
                        scheduled_end_time_utc, actual_end_time_utc,
                        selected_sources_json, selected_fields_json,
                        csv_format, resolution, automatic_export_job_id,
                        created_at_utc, updated_at_utc, trigger_source,
                        printer_session_id, printer_ended_at_utc,
                        recovery_end_time_utc, auto_close_on_schedule
                    ) VALUES (?, ?, ?, 'running', ?, ?, NULL, ?, ?, 'wide',
                              'raw', NULL, ?, ?, 'printer', ?, NULL, NULL, 0)
                    """,
                    (
                        session_id,
                        name[:120],
                        "Automatically associated with read-only printer observation.",
                        now_text,
                        iso_utc(provisional_end_time),
                        _json(list(sources)),
                        _json(list(fields)),
                        now_text,
                        now_text,
                        printer_session_id,
                    ),
                )
            else:
                session_id = str(existing["id"])
        return self.get_session(session_id)

    def finish_printer_session(
        self,
        printer_session_id: str,
        *,
        print_ended_at: datetime,
        recovery_end_time: datetime,
    ) -> dict[str, Any] | None:
        """Schedule post-print recovery; this never communicates with a printer."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM monitoring_sessions WHERE printer_session_id=?",
                (printer_session_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "running":
                connection.execute(
                    """
                    UPDATE monitoring_sessions
                    SET printer_ended_at_utc=?, recovery_end_time_utc=?,
                        scheduled_end_time_utc=?, auto_close_on_schedule=1,
                        updated_at_utc=?
                    WHERE id=? AND status='running'
                    """,
                    (
                        iso_utc(print_ended_at),
                        iso_utc(recovery_end_time),
                        iso_utc(recovery_end_time),
                        iso_utc(print_ended_at),
                        row["id"],
                    ),
                )
                session_id = str(row["id"])
            else:
                session_id = str(row["id"])
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monitoring_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFoundError("unknown monitoring session")
        return _session_dict(row)

    def reconcile_due_sessions(self, now: datetime) -> int:
        now_text = iso_utc(now)
        reconciled = 0
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM monitoring_sessions
                WHERE status = 'running' AND auto_close_on_schedule = 1
                  AND scheduled_end_time_utc <= ?
                ORDER BY scheduled_end_time_utc, id
                """,
                (now_text,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE monitoring_sessions
                    SET status = 'completed', actual_end_time_utc = scheduled_end_time_utc,
                        updated_at_utc = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now_text, row["id"]),
                )
                current = connection.execute(
                    "SELECT * FROM monitoring_sessions WHERE id = ?", (row["id"],)
                ).fetchone()
                if current is not None:
                    self._ensure_automatic_export(connection, current, now_text)
                    reconciled += 1
        return reconciled

    def stop_session(self, session_id: str, now: datetime) -> dict[str, Any]:
        now_text = iso_utc(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM monitoring_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("unknown monitoring session")
            if row["trigger_source"] == "printer" and row["status"] == "running":
                raise WorkflowConflictError(
                    "automatic printer monitoring closes after its recovery interval"
                )
            if row["status"] == "running":
                if row["scheduled_end_time_utc"] <= now_text:
                    status = "completed"
                    actual_end = row["scheduled_end_time_utc"]
                else:
                    status = "stopped"
                    actual_end = now_text
                connection.execute(
                    """
                    UPDATE monitoring_sessions
                    SET status = ?, actual_end_time_utc = ?, updated_at_utc = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (status, actual_end, now_text, session_id),
                )
                row = connection.execute(
                    "SELECT * FROM monitoring_sessions WHERE id = ?", (session_id,)
                ).fetchone()
            self._ensure_automatic_export(connection, row, now_text)
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> str | None:
        job_id: str | None = None
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM monitoring_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise WorkflowNotFoundError("unknown monitoring session")
            if session["status"] == "running":
                raise WorkflowConflictError(
                    "a running monitoring session cannot be deleted"
                )
            job = connection.execute(
                "SELECT * FROM export_jobs WHERE monitoring_session_id = ?",
                (session_id,),
            ).fetchone()
            if job is not None:
                if job["status"] in {"running", "cancel_requested"}:
                    raise WorkflowConflictError(
                        "the associated export is active; cancel it and wait for cancellation first"
                    )
                job_id = str(job["id"])
                connection.execute("DELETE FROM export_jobs WHERE id = ?", (job_id,))
            connection.execute(
                "DELETE FROM monitoring_sessions WHERE id = ?", (session_id,)
            )
        return job_id

    def create_export(self, request: ExportRequest, *, now: datetime) -> dict[str, Any]:
        with self._connect() as connection:
            job_id = self._insert_export(
                connection,
                monitoring_session_id=None,
                name=request.name,
                start_time_utc=iso_utc(request.start_time),
                end_time_utc=iso_utc(request.end_time),
                sources=json_sources(request.sources),
                fields=list(request.fields),
                resolution=request.resolution,
                csv_format=request.csv_format,
                warnings=list(request.warnings),
                now_text=iso_utc(now),
            )
        return self.get_export(job_id)

    def list_exports(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM export_jobs
                ORDER BY created_at_utc DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [_export_dict(row) for row in rows]

    def get_export(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFoundError("unknown export job")
        return _export_dict(row)

    def get_export_for_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE monitoring_session_id = ?",
                (session_id,),
            ).fetchone()
        return _export_dict(row) if row is not None else None

    def claim_oldest_export(
        self, *, worker_id: str, now: datetime
    ) -> dict[str, Any] | None:
        now_text = iso_utc(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT id FROM export_jobs
                WHERE status = 'queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM export_jobs
                      WHERE status IN ('running', 'cancel_requested')
                  )
                ORDER BY created_at_utc, rowid
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE export_jobs
                SET status = 'running', current_phase = 'preparing', worker_id = ?,
                    attempt_count = attempt_count + 1,
                    started_at_utc = COALESCE(started_at_utc, ?),
                    heartbeat_at_utc = ?, updated_at_utc = ?,
                    completed_at_utc = NULL, error_message = NULL,
                    output_size_bytes = 0, rows_written = 0,
                    work_units_completed = 0, source_results_json = '[]'
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, now_text, now_text, now_text, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM export_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
        return _export_dict(claimed)

    def update_job_progress(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        current_phase: str | None = None,
        rows_written: int | None = None,
        output_size_bytes: int | None = None,
        work_units_total: int | None = None,
        work_units_completed: int | None = None,
        warnings: Sequence[str] | None = None,
        source_results: Sequence[Mapping[str, Any]] | None = None,
    ) -> bool:
        assignments = ["heartbeat_at_utc = ?", "updated_at_utc = ?"]
        now_text = iso_utc(now)
        values: list[Any] = [now_text, now_text]
        optional = (
            ("current_phase", current_phase),
            ("rows_written", rows_written),
            ("output_size_bytes", output_size_bytes),
            ("work_units_total", work_units_total),
            ("work_units_completed", work_units_completed),
        )
        for column, value in optional:
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if warnings is not None:
            assignments.append("warning_json = ?")
            values.append(_json(list(warnings)))
        if source_results is not None:
            assignments.append("source_results_json = ?")
            values.append(_json(list(source_results)))
        values.extend((job_id, worker_id))
        sql = (
            "UPDATE export_jobs SET "
            + ", ".join(assignments)
            + " WHERE id = ? AND worker_id = ? AND status IN ('running', 'cancel_requested')"
        )
        with self._connect() as connection:
            result = connection.execute(sql, tuple(values))
        return result.rowcount == 1

    def heartbeat(self, job_id: str, *, worker_id: str, now: datetime) -> bool:
        return self.update_job_progress(job_id, worker_id=worker_id, now=now)

    def cancellation_requested(self, job_id: str, *, worker_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, worker_id FROM export_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(
            row is not None
            and row["worker_id"] == worker_id
            and row["status"] == "cancel_requested"
        )

    def request_cancel(self, job_id: str, *, now: datetime) -> dict[str, Any]:
        now_text = iso_utc(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("unknown export job")
            if row["status"] == "queued":
                connection.execute(
                    """
                    UPDATE export_jobs
                    SET status = 'cancelled', current_phase = 'cancelled',
                        completed_at_utc = ?, heartbeat_at_utc = NULL,
                        updated_at_utc = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now_text, now_text, job_id),
                )
            elif row["status"] == "running":
                connection.execute(
                    """
                    UPDATE export_jobs
                    SET status = 'cancel_requested', current_phase = 'cancel_requested',
                        updated_at_utc = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now_text, job_id),
                )
            elif row["status"] in {"cancel_requested", "cancelled"}:
                pass
            else:
                raise WorkflowConflictError(
                    f"an export in status {row['status']} cannot be cancelled"
                )
        return self.get_export(job_id)

    def finish_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        rows_written: int,
        output_size_bytes: int,
        work_units_total: int,
        work_units_completed: int,
        warnings: Sequence[str],
        source_results: Sequence[Mapping[str, Any]],
    ) -> bool:
        now_text = iso_utc(now)
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE export_jobs
                SET status = 'completed', current_phase = 'completed',
                    rows_written = ?, output_size_bytes = ?,
                    work_units_total = ?, work_units_completed = ?,
                    warning_json = ?, source_results_json = ?,
                    completed_at_utc = ?, heartbeat_at_utc = NULL,
                    updated_at_utc = ?
                WHERE id = ? AND worker_id = ? AND status = 'running'
                """,
                (
                    rows_written,
                    output_size_bytes,
                    work_units_total,
                    work_units_completed,
                    _json(list(warnings)),
                    _json(list(source_results)),
                    now_text,
                    now_text,
                    job_id,
                    worker_id,
                ),
            )
        return result.rowcount == 1

    def mark_job_cancelled(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        warnings: Sequence[str],
        source_results: Sequence[Mapping[str, Any]],
    ) -> bool:
        now_text = iso_utc(now)
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE export_jobs
                SET status = 'cancelled', current_phase = 'cancelled',
                    warning_json = ?, source_results_json = ?,
                    output_size_bytes = 0, completed_at_utc = ?,
                    heartbeat_at_utc = NULL, updated_at_utc = ?
                WHERE id = ? AND worker_id = ?
                  AND status IN ('running', 'cancel_requested')
                """,
                (
                    _json(list(warnings)),
                    _json(list(source_results)),
                    now_text,
                    now_text,
                    job_id,
                    worker_id,
                ),
            )
        return result.rowcount == 1

    def mark_job_failed(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        error_message: str,
    ) -> bool:
        now_text = iso_utc(now)
        message = error_message.strip()[:1000] or "export failed"
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE export_jobs
                SET status = 'failed', current_phase = 'failed', error_message = ?,
                    output_size_bytes = 0, completed_at_utc = ?,
                    heartbeat_at_utc = NULL, updated_at_utc = ?
                WHERE id = ? AND worker_id = ?
                  AND status IN ('running', 'cancel_requested')
                """,
                (message, now_text, now_text, job_id, worker_id),
            )
        return result.rowcount == 1

    def release_owned_job(
        self, job_id: str, *, worker_id: str, now: datetime
    ) -> str | None:
        """Return an interrupted owned job to a restart-safe state on graceful stop."""

        now_text = iso_utc(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM export_jobs WHERE id = ? AND worker_id = ?",
                (job_id, worker_id),
            ).fetchone()
            if row is None or row["status"] not in {"running", "cancel_requested"}:
                return None
            if row["status"] == "cancel_requested":
                status = "cancelled"
                phase = "cancelled"
                completed_at = now_text
            else:
                status = "queued"
                phase = "queued"
                completed_at = None
            connection.execute(
                """
                UPDATE export_jobs
                SET status = ?, current_phase = ?, worker_id = NULL,
                    heartbeat_at_utc = NULL, completed_at_utc = ?,
                    rows_written = 0, output_size_bytes = 0,
                    work_units_completed = 0, source_results_json = '[]',
                    updated_at_utc = ?
                WHERE id = ? AND worker_id = ? AND status = ?
                """,
                (
                    status,
                    phase,
                    completed_at,
                    now_text,
                    job_id,
                    worker_id,
                    row["status"],
                ),
            )
        return status

    def recover_stale_jobs(
        self, *, cutoff: datetime, now: datetime
    ) -> list[dict[str, str]]:
        cutoff_text = iso_utc(cutoff)
        now_text = iso_utc(now)
        recovered: list[dict[str, str]] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, status FROM export_jobs
                WHERE status IN ('running', 'cancel_requested')
                  AND (heartbeat_at_utc IS NULL OR heartbeat_at_utc < ?)
                ORDER BY created_at_utc, id
                """,
                (cutoff_text,),
            ).fetchall()
            for row in rows:
                if row["status"] == "cancel_requested":
                    new_status = "cancelled"
                    phase = "cancelled"
                    completed = now_text
                else:
                    new_status = "queued"
                    phase = "queued"
                    completed = None
                connection.execute(
                    """
                    UPDATE export_jobs
                    SET status = ?, current_phase = ?, worker_id = NULL,
                        heartbeat_at_utc = NULL, completed_at_utc = ?,
                        rows_written = 0, output_size_bytes = 0,
                        work_units_completed = 0, source_results_json = '[]',
                        error_message = NULL, updated_at_utc = ?
                    WHERE id = ? AND status = ?
                    """,
                    (new_status, phase, completed, now_text, row["id"], row["status"]),
                )
                recovered.append({"id": str(row["id"]), "status": new_status})
        return recovered

    def delete_export(self, job_id: str) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM export_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise WorkflowNotFoundError("unknown export job")
            if row["monitoring_session_id"] is not None:
                raise WorkflowConflictError(
                    "automatic monitoring exports are deleted with their monitoring session"
                )
            if row["status"] not in {"completed", "failed", "cancelled"}:
                raise WorkflowConflictError(
                    "only completed, failed, or cancelled exports can be deleted"
                )
            connection.execute("DELETE FROM export_jobs WHERE id = ?", (job_id,))

    def _ensure_automatic_export(
        self, connection: sqlite3.Connection, session: sqlite3.Row, now_text: str
    ) -> str:
        existing = connection.execute(
            "SELECT id FROM export_jobs WHERE monitoring_session_id = ?",
            (session["id"],),
        ).fetchone()
        if existing is None:
            effective_end = (
                session["actual_end_time_utc"] or session["scheduled_end_time_utc"]
            )
            job_id = self._insert_export(
                connection,
                monitoring_session_id=str(session["id"]),
                name=f"{session['name']} monitoring export",
                start_time_utc=str(session["start_time_utc"]),
                end_time_utc=str(effective_end),
                sources=json.loads(session["selected_sources_json"]),
                fields=json.loads(session["selected_fields_json"]),
                resolution=str(session["resolution"]),
                csv_format=str(session["csv_format"]),
                warnings=[],
                now_text=now_text,
            )
        else:
            job_id = str(existing["id"])
        connection.execute(
            """
            UPDATE monitoring_sessions
            SET automatic_export_job_id = COALESCE(automatic_export_job_id, ?),
                updated_at_utc = ?
            WHERE id = ?
            """,
            (job_id, now_text, session["id"]),
        )
        return job_id

    def _insert_export(
        self,
        connection: sqlite3.Connection,
        *,
        monitoring_session_id: str | None,
        name: str,
        start_time_utc: str,
        end_time_utc: str,
        sources: Sequence[Mapping[str, Any]],
        fields: Sequence[str],
        resolution: str,
        csv_format: str,
        warnings: Sequence[str],
        now_text: str,
    ) -> str:
        job_id = str(uuid4())
        output_path = str(self.output_dir / f"{job_id}.csv")
        connection.execute(
            """
            INSERT INTO export_jobs (
                id, monitoring_session_id, name, status,
                start_time_utc, end_time_utc,
                selected_sources_json, selected_fields_json,
                resolution, csv_format, output_path,
                output_size_bytes, rows_written,
                work_units_total, work_units_completed,
                current_phase, warning_json, source_results_json,
                error_message, worker_id, attempt_count,
                created_at_utc, started_at_utc, completed_at_utc,
                heartbeat_at_utc, updated_at_utc
            ) VALUES (
                ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?,
                0, 0, 0, 0, 'queued', ?, '[]', NULL, NULL, 0,
                ?, NULL, NULL, NULL, ?
            )
            """,
            (
                job_id,
                monitoring_session_id,
                name,
                start_time_utc,
                end_time_utc,
                _json(list(sources)),
                _json(list(fields)),
                resolution,
                csv_format,
                output_path,
                _json(list(warnings)),
                now_text,
                now_text,
            ),
        )
        return job_id

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()


def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["selected_sources"] = json.loads(result.pop("selected_sources_json"))
    result["selected_fields"] = json.loads(result.pop("selected_fields_json"))
    return result


def _export_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["selected_sources"] = json.loads(result.pop("selected_sources_json"))
    result["selected_fields"] = json.loads(result.pop("selected_fields_json"))
    result["warnings"] = json.loads(result.pop("warning_json"))
    result["source_results"] = json.loads(result.pop("source_results_json"))
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
