"""Restart-safe, idempotent SQLite persistence for printer sessions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.printer_model import (
    NormalizedPrinterState,
    PrinterState,
    PrintSession,
    ValueProvenance,
)

TERMINAL_RESULTS = {
    NormalizedPrinterState.COMPLETED: "completed",
    NormalizedPrinterState.FAILED: "failed",
    NormalizedPrinterState.CANCELLED: "cancelled",
    NormalizedPrinterState.IDLE: "unknown",
}


class PrinterStore:
    def __init__(self, database_path: Path, *, terminal_confirmations: int = 2) -> None:
        self.database_path = Path(database_path)
        self.terminal_confirmations = terminal_confirmations

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS printer_current_state (
                    printer_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    permanent_sample_at_utc TEXT
                );

                CREATE TABLE IF NOT EXISTS print_sessions (
                    session_id TEXT PRIMARY KEY,
                    printer_id TEXT NOT NULL,
                    job_id TEXT,
                    job_name TEXT,
                    started_at_utc TEXT NOT NULL,
                    start_provenance TEXT NOT NULL,
                    ended_at_utc TEXT,
                    end_provenance TEXT NOT NULL,
                    result TEXT,
                    material TEXT,
                    material_provenance TEXT NOT NULL,
                    active_tool TEXT,
                    ams_slot TEXT,
                    source TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_print_per_printer
                    ON print_sessions(printer_id) WHERE ended_at_utc IS NULL;
                CREATE INDEX IF NOT EXISTS idx_print_sessions_started
                    ON print_sessions(printer_id, started_at_utc DESC);

                CREATE TABLE IF NOT EXISTS printer_tracker_state (
                    printer_id TEXT PRIMARY KEY,
                    terminal_candidate TEXT,
                    terminal_candidate_at_utc TEXT,
                    terminal_candidate_count INTEGER NOT NULL DEFAULT 0
                );
                """
            )
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    def process(self, state: PrinterState) -> tuple[PrintSession, ...]:
        """Persist current state and apply one idempotent session transition."""

        changed_sessions: list[PrintSession] = []
        with self._transaction() as connection:
            now_text = _iso(state.observed_at)
            connection.execute(
                """
                INSERT INTO printer_current_state (printer_id, state_json, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(printer_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (
                    state.printer_id,
                    json.dumps(state.to_dict(), sort_keys=True),
                    now_text,
                ),
            )
            active_row = connection.execute(
                """SELECT * FROM print_sessions
                   WHERE printer_id = ? AND ended_at_utc IS NULL""",
                (state.printer_id,),
            ).fetchone()

            if state.session_active:
                if active_row is not None and _different_stable_job(active_row, state):
                    changed_sessions.append(
                        self._close_session(
                            connection,
                            active_row,
                            ended_at=state.observed_at,
                            result="unknown",
                            end_provenance=ValueProvenance.INFERRED_TIMESTAMP,
                        )
                    )
                    active_row = None
                if active_row is None:
                    active_row = self._create_session(connection, state)
                    changed = True
                else:
                    changed = self._update_active_session(connection, active_row, state)
                self._clear_candidate(connection, state.printer_id)
                current_session = self._row_to_session(
                    connection.execute(
                        "SELECT * FROM print_sessions WHERE session_id = ?",
                        (active_row["session_id"],),
                    ).fetchone()
                )
                if changed:
                    changed_sessions.append(current_session)
            elif state.normalized_state in TERMINAL_RESULTS and active_row is not None:
                candidate = state.normalized_state.value
                tracker = connection.execute(
                    "SELECT * FROM printer_tracker_state WHERE printer_id = ?",
                    (state.printer_id,),
                ).fetchone()
                # Preserve an observed terminal outcome while HA settles back
                # to idle. Otherwise FINISH -> IDLE would become "unknown".
                if (
                    state.normalized_state is NormalizedPrinterState.IDLE
                    and tracker is not None
                    and tracker["terminal_candidate"]
                    in {"completed", "failed", "cancelled"}
                ):
                    candidate = str(tracker["terminal_candidate"])
                count = (
                    int(tracker["terminal_candidate_count"]) + 1
                    if tracker is not None
                    and tracker["terminal_candidate"] == candidate
                    else 1
                )
                connection.execute(
                    """
                    INSERT INTO printer_tracker_state (
                        printer_id, terminal_candidate, terminal_candidate_at_utc,
                        terminal_candidate_count
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(printer_id) DO UPDATE SET
                        terminal_candidate=excluded.terminal_candidate,
                        terminal_candidate_at_utc=excluded.terminal_candidate_at_utc,
                        terminal_candidate_count=excluded.terminal_candidate_count
                    """,
                    (state.printer_id, candidate, now_text, count),
                )
                if count >= self.terminal_confirmations:
                    observed_end = state.print_finished_at
                    end = (
                        observed_end
                        if observed_end is not None
                        and observed_end >= _datetime(active_row["started_at_utc"])
                        and observed_end <= state.observed_at
                        else state.observed_at
                    )
                    end_provenance = (
                        ValueProvenance.OBSERVED
                        if end is observed_end
                        else ValueProvenance.INFERRED_TIMESTAMP
                    )
                    changed_sessions.append(
                        self._close_session(
                            connection,
                            active_row,
                            ended_at=end,
                            result=TERMINAL_RESULTS[NormalizedPrinterState(candidate)],
                            end_provenance=end_provenance,
                        )
                    )
                    self._clear_candidate(connection, state.printer_id)
            # Offline and unknown observations deliberately leave an active
            # session open. Reconnect can resume it without minting a duplicate.
        return tuple(changed_sessions)

    def current_state(self, printer_id: str | None = None) -> PrinterState | None:
        with self._connect() as connection:
            if printer_id is None:
                row = connection.execute(
                    "SELECT * FROM printer_current_state ORDER BY updated_at_utc DESC LIMIT 1"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM printer_current_state WHERE printer_id = ?",
                    (printer_id,),
                ).fetchone()
        if row is None:
            return None
        try:
            return PrinterState.from_dict(json.loads(row["state_json"]))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def latest_session(self, printer_id: str | None = None) -> PrintSession | None:
        query = "SELECT * FROM print_sessions"
        values: tuple[str, ...] = ()
        if printer_id is not None:
            query += " WHERE printer_id = ?"
            values = (printer_id,)
        query += " ORDER BY started_at_utc DESC, session_id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return None if row is None else self._row_to_session(row)

    def latest_finished_session(
        self, printer_id: str | None = None
    ) -> PrintSession | None:
        query = "SELECT * FROM print_sessions WHERE ended_at_utc IS NOT NULL"
        values: tuple[str, ...] = ()
        if printer_id is not None:
            query += " AND printer_id = ?"
            values = (printer_id,)
        query += " ORDER BY started_at_utc DESC, session_id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return None if row is None else self._row_to_session(row)

    def list_sessions(
        self, printer_id: str | None = None, *, limit: int = 20
    ) -> list[PrintSession]:
        query = "SELECT * FROM print_sessions"
        values: list[object] = []
        if printer_id is not None:
            query += " WHERE printer_id = ?"
            values.append(printer_id)
        query += " ORDER BY started_at_utc DESC, session_id DESC LIMIT ?"
        values.append(max(1, min(limit, 100)))
        with self._connect() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return [self._row_to_session(row) for row in rows]

    def permanent_sample_due(
        self, printer_id: str, observed_at: datetime, interval_seconds: int
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT permanent_sample_at_utc FROM printer_current_state WHERE printer_id = ?",
                (printer_id,),
            ).fetchone()
        if row is None or row["permanent_sample_at_utc"] is None:
            return True
        return (
            observed_at - _datetime(row["permanent_sample_at_utc"])
        ).total_seconds() >= interval_seconds

    def mark_permanent_sample(self, printer_id: str, observed_at: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE printer_current_state SET permanent_sample_at_utc = ?
                   WHERE printer_id = ?""",
                (_iso(observed_at), printer_id),
            )

    def _create_session(
        self, connection: sqlite3.Connection, state: PrinterState
    ) -> sqlite3.Row:
        observed_start = state.print_started_at
        start = (
            observed_start
            if observed_start is not None and observed_start <= state.observed_at
            else state.observed_at
        )
        start_provenance = (
            ValueProvenance.OBSERVED
            if start is observed_start
            else ValueProvenance.INFERRED_TIMESTAMP
        )
        session_id = str(uuid4())
        material_provenance = state.provenance.get(
            "active_material", ValueProvenance.UNKNOWN
        )
        connection.execute(
            """
            INSERT INTO print_sessions (
                session_id, printer_id, job_id, job_name, started_at_utc,
                start_provenance, ended_at_utc, end_provenance, result,
                material, material_provenance, active_tool, ams_slot, source,
                updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                state.printer_id,
                state.job_id,
                state.job_name,
                _iso(start),
                start_provenance.value,
                ValueProvenance.UNKNOWN.value,
                state.active_material,
                material_provenance.value,
                state.active_tool,
                state.ams_slot,
                "locally_observed",
                _iso(state.observed_at),
            ),
        )
        return connection.execute(
            "SELECT * FROM print_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()

    def _update_active_session(
        self, connection: sqlite3.Connection, row: sqlite3.Row, state: PrinterState
    ) -> bool:
        material_provenance = state.provenance.get(
            "active_material", ValueProvenance.UNKNOWN
        )
        material = row["material"]
        stored_provenance = row["material_provenance"]
        if state.active_material is not None:
            if material is None:
                material = state.active_material
                stored_provenance = material_provenance.value
            elif material != state.active_material and material != "multiple":
                material = "multiple"
                stored_provenance = ValueProvenance.UNKNOWN.value
        updates = {
            "job_id": state.job_id or row["job_id"],
            "job_name": state.job_name or row["job_name"],
            "material": material,
            "material_provenance": stored_provenance,
            "active_tool": state.active_tool or row["active_tool"],
            "ams_slot": state.ams_slot or row["ams_slot"],
        }
        changed = any(row[key] != value for key, value in updates.items())
        connection.execute(
            """
            UPDATE print_sessions SET job_id=?, job_name=?, material=?,
                material_provenance=?, active_tool=?, ams_slot=?, updated_at_utc=?
            WHERE session_id=?
            """,
            (*updates.values(), _iso(state.observed_at), row["session_id"]),
        )
        return changed

    def _close_session(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        ended_at: datetime,
        result: str,
        end_provenance: ValueProvenance,
    ) -> PrintSession:
        connection.execute(
            """
            UPDATE print_sessions SET ended_at_utc=?, end_provenance=?, result=?,
                updated_at_utc=? WHERE session_id=? AND ended_at_utc IS NULL
            """,
            (
                _iso(ended_at),
                end_provenance.value,
                result,
                _iso(ended_at),
                row["session_id"],
            ),
        )
        updated = connection.execute(
            "SELECT * FROM print_sessions WHERE session_id = ?",
            (row["session_id"],),
        ).fetchone()
        return self._row_to_session(updated)

    @staticmethod
    def _clear_candidate(connection: sqlite3.Connection, printer_id: str) -> None:
        connection.execute(
            "DELETE FROM printer_tracker_state WHERE printer_id = ?", (printer_id,)
        )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> PrintSession:
        return PrintSession(
            session_id=row["session_id"],
            printer_id=row["printer_id"],
            job_id=row["job_id"],
            job_name=row["job_name"],
            started_at=_datetime(row["started_at_utc"]),
            start_provenance=ValueProvenance(row["start_provenance"]),
            ended_at=(
                None if row["ended_at_utc"] is None else _datetime(row["ended_at_utc"])
            ),
            end_provenance=ValueProvenance(row["end_provenance"]),
            result=row["result"],
            material=row["material"],
            material_provenance=ValueProvenance(row["material_provenance"]),
            active_tool=row["active_tool"],
            ams_slot=row["ams_slot"],
            source=row["source"],
            updated_at=_datetime(row["updated_at_utc"]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _different_stable_job(row: sqlite3.Row, state: PrinterState) -> bool:
    return bool(row["job_id"] and state.job_id and row["job_id"] != state.job_id)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)
