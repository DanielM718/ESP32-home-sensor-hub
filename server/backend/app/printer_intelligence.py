"""Durable cloud-history, usage, and local maintenance intelligence."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.printer_config import MaintenanceTaskSettings

CLOUD_TASKS_URL = "https://api.bambulab.com/v1/user-service/my/tasks"
CLOUD_SOURCE = "bambu_cloud_history"
LOCAL_SOURCES = frozenset({"locally_observed", "home_assistant"})
COUNTED_USAGE_RESULTS = frozenset({"completed", "failed", "cancelled"})
MAX_CLOUD_RESPONSE_BYTES = 16 * 1024 * 1024
TASK_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


class PrinterIntelligenceError(RuntimeError):
    pass


class BambuCloudHistoryAdapter:
    """Bounded GET-only access to Bambu Cloud task metadata."""

    def __init__(
        self,
        token: str,
        device_id: str,
        *,
        timeout_seconds: float = 15,
        max_records: int = 1000,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not token.strip() or not device_id.strip():
            raise PrinterIntelligenceError(
                "cloud history credentials are not configured"
            )
        self._token = token.strip()
        self._device_id = device_id.strip()
        self.timeout_seconds = timeout_seconds
        self.max_records = max_records
        self._opener = opener

    def fetch(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        after: object | None = None
        while len(records) < self.max_records:
            parameters: dict[str, object] = {
                "deviceId": self._device_id,
                "limit": min(100, self.max_records - len(records)),
            }
            if after is not None:
                parameters["after"] = after
            payload = self._get(parameters)
            if total is None and isinstance(payload.get("total"), int):
                total = int(payload["total"])
            hits = payload.get("hits")
            if not isinstance(hits, list):
                break
            new_records = []
            for item in hits:
                if not isinstance(item, dict):
                    continue
                identity = _cloud_identity(item)
                if identity in seen:
                    continue
                seen.add(identity)
                new_records.append(_sanitize_cloud_record(item))
            if not new_records:
                break
            records.extend(new_records)
            after = hits[-1].get("id") if isinstance(hits[-1], dict) else None
            if after is None:
                break
            if total is not None and len(records) >= total:
                break
            if total is None and len(hits) < int(parameters["limit"]):
                break
        return {
            "records": records,
            "api_total": total,
            "records_retrieved": len(records),
            "truncated": (
                (total is not None and len(records) < total)
                or (total is None and len(records) >= self.max_records)
            ),
        }

    def _get(self, parameters: Mapping[str, object]) -> dict[str, Any]:
        request = Request(
            f"{CLOUD_TASKS_URL}?{urlencode(parameters)}",
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "bambu_network_agent/01.09.05.01",
                "X-BBL-Client-Name": "OrcaSlicer",
                "X-BBL-Client-Type": "slicer",
                "X-BBL-Client-Version": "01.09.05.51",
                "X-BBL-Language": "en-US",
                "X-BBL-OS-Type": "linux",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_CLOUD_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise PrinterIntelligenceError(
                    "Bambu Cloud authentication was denied"
                ) from None
            raise PrinterIntelligenceError(
                "Bambu Cloud returned a non-success status"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise PrinterIntelligenceError(
                "Bambu Cloud history is unavailable"
            ) from None
        if len(raw) > MAX_CLOUD_RESPONSE_BYTES:
            raise PrinterIntelligenceError(
                "Bambu Cloud response exceeded the size limit"
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PrinterIntelligenceError(
                "Bambu Cloud returned malformed JSON"
            ) from None
        if not isinstance(payload, dict):
            raise PrinterIntelligenceError("Bambu Cloud response has an invalid shape")
        return payload


class PrinterIntelligenceStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cloud_print_history (
                    history_id TEXT PRIMARY KEY,
                    printer_id TEXT NOT NULL,
                    cloud_id TEXT NOT NULL,
                    title TEXT,
                    design_title TEXT,
                    status_raw TEXT,
                    result TEXT,
                    started_at_utc TEXT,
                    ended_at_utc TEXT,
                    duration_seconds INTEGER,
                    slicer_estimated_seconds INTEGER,
                    weight_grams REAL,
                    length_meters REAL,
                    plate_index INTEGER,
                    plate_name TEXT,
                    bed_type TEXT,
                    print_source TEXT,
                    device_model TEXT,
                    cover_available INTEGER NOT NULL DEFAULT 0,
                    materials_json TEXT NOT NULL DEFAULT '[]',
                    ams_mapping_json TEXT NOT NULL DEFAULT '[]',
                    nozzle_ids_json TEXT NOT NULL DEFAULT '[]',
                    raw_source_json TEXT NOT NULL DEFAULT '{}',
                    imported_at_utc TEXT NOT NULL,
                    reconciled_session_id TEXT,
                    UNIQUE (printer_id, cloud_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cloud_history_started
                    ON cloud_print_history(printer_id, started_at_utc DESC);

                CREATE TABLE IF NOT EXISTS printer_usage_baselines (
                    printer_id TEXT PRIMARY KEY,
                    local_seconds_at_baseline INTEGER NOT NULL,
                    ha_estimate_baseline_hours REAL,
                    ha_estimate_latest_hours REAL,
                    printer_reported_baseline_hours REAL,
                    printer_reported_latest_hours REAL,
                    first_observed_at_utc TEXT NOT NULL,
                    latest_observed_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS maintenance_tasks (
                    task_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    interval_hours REAL,
                    warning_hours REAL NOT NULL DEFAULT 0,
                    interval_prints INTEGER,
                    warning_prints INTEGER NOT NULL DEFAULT 0,
                    interval_days INTEGER,
                    warning_days INTEGER NOT NULL DEFAULT 0,
                    due_when TEXT NOT NULL CHECK (due_when IN ('any', 'all')),
                    notes TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS maintenance_completion_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    completed_at_utc TEXT NOT NULL,
                    effective_usage_hours REAL NOT NULL,
                    completed_print_count INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    recorded_by TEXT NOT NULL DEFAULT 'dashboard_user',
                    FOREIGN KEY(task_id) REFERENCES maintenance_tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_maintenance_events
                    ON maintenance_completion_events(task_id, completed_at_utc DESC);

                CREATE TABLE IF NOT EXISTS printer_history_import_state (
                    printer_id TEXT PRIMARY KEY,
                    last_attempt_at_utc TEXT,
                    last_success_at_utc TEXT,
                    api_total INTEGER,
                    imported_records INTEGER NOT NULL DEFAULT 0,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                );
                """
            )
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    def import_cloud_records(
        self,
        printer_id: str,
        records: Sequence[Mapping[str, Any]],
        *,
        imported_at: datetime,
        api_total: int | None = None,
        truncated: bool = False,
    ) -> dict[str, int]:
        now_text = _iso(imported_at)
        inserted = 0
        updated = 0
        with self._transaction() as connection:
            for source in records:
                record = _sanitize_cloud_record(source)
                cloud_id = _cloud_identity(record)
                existing = connection.execute(
                    "SELECT history_id FROM cloud_print_history WHERE printer_id=? AND cloud_id=?",
                    (printer_id, cloud_id),
                ).fetchone()
                values = _cloud_values(printer_id, cloud_id, record, now_text)
                connection.execute(
                    """
                    INSERT INTO cloud_print_history (
                        history_id, printer_id, cloud_id, title, design_title,
                        status_raw, result, started_at_utc, ended_at_utc,
                        duration_seconds, slicer_estimated_seconds, weight_grams,
                        length_meters, plate_index, plate_name, bed_type,
                        print_source, device_model, cover_available,
                        materials_json, ams_mapping_json, nozzle_ids_json,
                        raw_source_json, imported_at_utc, reconciled_session_id
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, NULL
                    )
                    ON CONFLICT(printer_id, cloud_id) DO UPDATE SET
                        title=excluded.title, design_title=excluded.design_title,
                        status_raw=excluded.status_raw, result=excluded.result,
                        started_at_utc=excluded.started_at_utc,
                        ended_at_utc=excluded.ended_at_utc,
                        duration_seconds=excluded.duration_seconds,
                        slicer_estimated_seconds=excluded.slicer_estimated_seconds,
                        weight_grams=excluded.weight_grams,
                        length_meters=excluded.length_meters,
                        plate_index=excluded.plate_index,
                        plate_name=excluded.plate_name, bed_type=excluded.bed_type,
                        print_source=excluded.print_source,
                        device_model=excluded.device_model,
                        cover_available=excluded.cover_available,
                        materials_json=excluded.materials_json,
                        ams_mapping_json=excluded.ams_mapping_json,
                        nozzle_ids_json=excluded.nozzle_ids_json,
                        raw_source_json=excluded.raw_source_json,
                        imported_at_utc=excluded.imported_at_utc
                    """,
                    values,
                )
                inserted += existing is None
                updated += existing is not None
            reconciled = self._reconcile(connection, printer_id)
            connection.execute(
                """
                INSERT INTO printer_history_import_state (
                    printer_id, last_attempt_at_utc, last_success_at_utc,
                    api_total, imported_records, truncated, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(printer_id) DO UPDATE SET
                    last_attempt_at_utc=excluded.last_attempt_at_utc,
                    last_success_at_utc=excluded.last_success_at_utc,
                    api_total=excluded.api_total,
                    imported_records=excluded.imported_records,
                    truncated=excluded.truncated,
                    last_error=NULL
                """,
                (
                    printer_id,
                    now_text,
                    now_text,
                    api_total,
                    len(records),
                    int(truncated),
                ),
            )
        return {"inserted": inserted, "updated": updated, "reconciled": reconciled}

    def record_import_error(
        self, printer_id: str, error: str, *, attempted_at: datetime
    ) -> None:
        safe_error = str(error)[:256]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO printer_history_import_state (
                    printer_id, last_attempt_at_utc, imported_records, truncated,
                    last_error
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(printer_id) DO UPDATE SET
                    last_attempt_at_utc=excluded.last_attempt_at_utc,
                    last_error=excluded.last_error
                """,
                (printer_id, _iso(attempted_at), safe_error),
            )

    def history(
        self, printer_id: str | None = None, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses = []
        values: list[object] = []
        if printer_id is not None:
            clauses.append("printer_id = ?")
            values.append(printer_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            cloud_rows = connection.execute(
                f"SELECT * FROM cloud_print_history {where} ORDER BY started_at_utc DESC, history_id DESC",
                tuple(values),
            ).fetchall()
            local_query = "SELECT * FROM print_sessions"
            local_values: tuple[object, ...] = ()
            if printer_id is not None:
                local_query += " WHERE printer_id = ?"
                local_values = (printer_id,)
            local_rows = connection.execute(local_query, local_values).fetchall()
        cloud_by_session = {
            str(row["reconciled_session_id"]): row
            for row in cloud_rows
            if row["reconciled_session_id"]
        }
        items = [
            _local_history_dict(row, cloud_by_session.get(str(row["session_id"])))
            for row in local_rows
        ]
        items.extend(
            _cloud_history_dict(row)
            for row in cloud_rows
            if not row["reconciled_session_id"]
        )
        items.sort(
            key=lambda item: str(item.get("started_at") or item.get("ended_at") or ""),
            reverse=True,
        )
        return items[: max(1, min(limit, 500))]

    def history_item(self, history_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            local = connection.execute(
                "SELECT * FROM print_sessions WHERE session_id = ?", (history_id,)
            ).fetchone()
            if local is not None:
                cloud = connection.execute(
                    "SELECT * FROM cloud_print_history WHERE reconciled_session_id = ?",
                    (history_id,),
                ).fetchone()
                return _local_history_dict(local, cloud)
            cloud = connection.execute(
                "SELECT * FROM cloud_print_history WHERE history_id = ?", (history_id,)
            ).fetchone()
        return None if cloud is None else _cloud_history_dict(cloud)

    def history_status(self, printer_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            if printer_id is None:
                row = connection.execute(
                    "SELECT * FROM printer_history_import_state ORDER BY last_attempt_at_utc DESC LIMIT 1"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM printer_history_import_state WHERE printer_id=?",
                    (printer_id,),
                ).fetchone()
        return {} if row is None else dict(row)

    def observe_usage(
        self,
        printer_id: str,
        *,
        observed_at: datetime,
        ha_estimate_hours: float | None,
        printer_reported_hours: float | None,
    ) -> None:
        local_seconds = self._local_usage_seconds(printer_id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO printer_usage_baselines (
                    printer_id, local_seconds_at_baseline,
                    ha_estimate_baseline_hours, ha_estimate_latest_hours,
                    printer_reported_baseline_hours, printer_reported_latest_hours,
                    first_observed_at_utc, latest_observed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(printer_id) DO UPDATE SET
                    ha_estimate_latest_hours=COALESCE(excluded.ha_estimate_latest_hours, ha_estimate_latest_hours),
                    printer_reported_latest_hours=COALESCE(excluded.printer_reported_latest_hours, printer_reported_latest_hours),
                    latest_observed_at_utc=excluded.latest_observed_at_utc
                """,
                (
                    printer_id,
                    local_seconds,
                    ha_estimate_hours,
                    ha_estimate_hours,
                    printer_reported_hours,
                    printer_reported_hours,
                    _iso(observed_at),
                    _iso(observed_at),
                ),
            )

    def usage_summary(self, printer_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            if printer_id is None:
                baseline = connection.execute(
                    "SELECT * FROM printer_usage_baselines ORDER BY latest_observed_at_utc DESC LIMIT 1"
                ).fetchone()
                printer_id = str(baseline["printer_id"]) if baseline else None
            else:
                baseline = connection.execute(
                    "SELECT * FROM printer_usage_baselines WHERE printer_id=?",
                    (printer_id,),
                ).fetchone()
            stats = self._local_stats(connection, printer_id)
            cloud = connection.execute(
                """SELECT COUNT(*) AS jobs, COALESCE(SUM(duration_seconds), 0) AS seconds
                   FROM cloud_print_history WHERE (? IS NULL OR printer_id=?)""",
                (printer_id, printer_id),
            ).fetchone()
        local_hours = stats["usage_seconds"] / 3600
        effective = local_hours
        effective_provenance = "locally_observed"
        ha_latest = None
        printer_latest = None
        if baseline is not None:
            delta_hours = max(
                0.0,
                (stats["usage_seconds"] - int(baseline["local_seconds_at_baseline"]))
                / 3600,
            )
            ha_latest = baseline["ha_estimate_latest_hours"]
            printer_latest = baseline["printer_reported_latest_hours"]
            if printer_latest is not None:
                effective = max(
                    float(printer_latest),
                    float(baseline["printer_reported_baseline_hours"]) + delta_hours,
                )
                effective_provenance = "printer_reported_high_water_plus_local_delta"
            elif ha_latest is not None:
                effective = max(
                    float(ha_latest),
                    float(baseline["ha_estimate_baseline_hours"]) + delta_hours,
                )
                effective_provenance = (
                    "ha_bambulab_estimate_high_water_plus_local_delta"
                )
        return {
            "printer_reported_lifetime_hours": printer_latest,
            "printer_reported_lifetime_hours_available": printer_latest is not None,
            "ha_bambulab_estimated_usage_hours": ha_latest,
            "locally_observed_print_hours": round(local_hours, 4),
            "maintenance_effective_lifetime_hours": round(effective, 4),
            "maintenance_effective_provenance": effective_provenance,
            "locally_observed_completed_print_count": stats["completed_count"],
            "locally_recorded_terminal_job_count": stats["terminal_count"],
            "cloud_history_known_interval_hours": round(
                float(cloud["seconds"]) / 3600, 4
            ),
            "cloud_history_job_count": int(cloud["jobs"]),
            "semantics": {
                "local_hours": "completed, failed, and cancelled locally observed session intervals; unknown outcomes excluded",
                "completed_print_count": "locally observed sessions whose confirmed result is completed",
                "cloud_hours": "sum of known cloud start/end intervals; not a lifetime counter and not added to local hours",
            },
        }

    def sync_maintenance_tasks(
        self, tasks: Sequence[MaintenanceTaskSettings], *, now: datetime
    ) -> None:
        now_text = _iso(now)
        with self._transaction() as connection:
            # Configuration is authoritative for whether a task remains active.
            # Audit events are retained even when a task is removed or disabled.
            connection.execute(
                "UPDATE maintenance_tasks SET enabled=0, updated_at_utc=?",
                (now_text,),
            )
            for task in tasks:
                values = asdict(task)
                connection.execute(
                    """
                    INSERT INTO maintenance_tasks (
                        task_id, name, description, interval_hours, warning_hours,
                        interval_prints, warning_prints, interval_days, warning_days,
                        due_when, notes, source, enabled, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        name=excluded.name, description=excluded.description,
                        interval_hours=excluded.interval_hours,
                        warning_hours=excluded.warning_hours,
                        interval_prints=excluded.interval_prints,
                        warning_prints=excluded.warning_prints,
                        interval_days=excluded.interval_days,
                        warning_days=excluded.warning_days,
                        due_when=excluded.due_when, notes=excluded.notes,
                        source=excluded.source, enabled=excluded.enabled,
                        updated_at_utc=excluded.updated_at_utc
                    """,
                    (
                        values["task_id"],
                        values["name"],
                        values["description"],
                        values["interval_hours"],
                        values["warning_hours"],
                        values["interval_prints"],
                        values["warning_prints"],
                        values["interval_days"],
                        values["warning_days"],
                        values["due_when"],
                        values["notes"],
                        values["source"],
                        int(values["enabled"]),
                        now_text,
                        now_text,
                    ),
                )

    def maintenance(
        self, printer_id: str | None = None, *, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        usage = self.usage_summary(printer_id)
        with self._connect() as connection:
            tasks = connection.execute(
                "SELECT * FROM maintenance_tasks ORDER BY enabled DESC, name"
            ).fetchall()
            events = connection.execute(
                """SELECT * FROM maintenance_completion_events
                   ORDER BY completed_at_utc DESC, event_id DESC LIMIT 200"""
            ).fetchall()
        events_by_task: dict[str, list[sqlite3.Row]] = {}
        for event in events:
            events_by_task.setdefault(str(event["task_id"]), []).append(event)
        return {
            "available": True,
            "usage": usage,
            "tasks": [
                _maintenance_task_status(
                    task, events_by_task.get(str(task["task_id"]), []), usage, now
                )
                for task in tasks
            ],
            "completion_history": [_maintenance_event_dict(event) for event in events],
            "local_record_only": True,
            "printer_control": False,
        }

    def complete_maintenance(
        self,
        task_id: str,
        *,
        notes: str,
        completed_at: datetime,
        printer_id: str | None = None,
    ) -> dict[str, Any]:
        if not TASK_ID_RE.fullmatch(task_id):
            raise PrinterIntelligenceError("invalid maintenance task id")
        if len(notes) > 2000:
            raise PrinterIntelligenceError("maintenance notes exceed 2000 characters")
        usage = self.usage_summary(printer_id)
        event_id = str(uuid4())
        with self._transaction() as connection:
            task = connection.execute(
                "SELECT * FROM maintenance_tasks WHERE task_id=? AND enabled=1",
                (task_id,),
            ).fetchone()
            if task is None:
                raise PrinterIntelligenceError("unknown or disabled maintenance task")
            connection.execute(
                """INSERT INTO maintenance_completion_events (
                       event_id, task_id, completed_at_utc,
                       effective_usage_hours, completed_print_count, notes
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    task_id,
                    _iso(completed_at),
                    usage["maintenance_effective_lifetime_hours"],
                    usage["locally_observed_completed_print_count"],
                    notes.strip(),
                ),
            )
        return {
            "event_id": event_id,
            "task_id": task_id,
            "completed_at": _iso(completed_at),
            "local_record_only": True,
            "printer_control": False,
        }

    def _reconcile(self, connection: sqlite3.Connection, printer_id: str) -> int:
        cloud_rows = connection.execute(
            "SELECT * FROM cloud_print_history WHERE printer_id=?",
            (printer_id,),
        ).fetchall()
        local_rows = connection.execute(
            "SELECT * FROM print_sessions WHERE printer_id=?",
            (printer_id,),
        ).fetchall()
        reconciled = 0
        claimed: set[str] = set()
        for cloud in cloud_rows:
            match = _match_local_session(cloud, local_rows, claimed)
            session_id = None if match is None else str(match["session_id"])
            if session_id:
                claimed.add(session_id)
                reconciled += 1
            connection.execute(
                "UPDATE cloud_print_history SET reconciled_session_id=? WHERE history_id=?",
                (session_id, cloud["history_id"]),
            )
        return reconciled

    def _local_usage_seconds(self, printer_id: str) -> int:
        with self._connect() as connection:
            return int(self._local_stats(connection, printer_id)["usage_seconds"])

    @staticmethod
    def _local_stats(
        connection: sqlite3.Connection, printer_id: str | None
    ) -> dict[str, int]:
        values: list[object] = []
        where = [
            "ended_at_utc IS NOT NULL",
            "source IN ('locally_observed','home_assistant')",
        ]
        if printer_id is not None:
            where.append("printer_id=?")
            values.append(printer_id)
        rows = connection.execute(
            f"SELECT started_at_utc, ended_at_utc, result FROM print_sessions WHERE {' AND '.join(where)}",
            tuple(values),
        ).fetchall()
        usage_seconds = 0
        completed = 0
        terminal = 0
        for row in rows:
            result = str(row["result"] or "")
            if result in COUNTED_USAGE_RESULTS:
                start = _datetime(row["started_at_utc"])
                end = _datetime(row["ended_at_utc"])
                if start and end:
                    usage_seconds += max(0, round((end - start).total_seconds()))
                terminal += 1
            if result == "completed":
                completed += 1
        return {
            "usage_seconds": usage_seconds,
            "completed_count": completed,
            "terminal_count": terminal,
        }

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


def _sanitize_cloud_record(source: Mapping[str, Any]) -> dict[str, Any]:
    safe_keys = (
        "id",
        "cloud_id",
        "title",
        "designTitle",
        "status",
        "failedType",
        "startTime",
        "endTime",
        "costTime",
        "weight",
        "length",
        "plateIndex",
        "plateName",
        "mode",
        "deviceModel",
        "bedType",
        "jobType",
        "useAms",
        "cover_available",
    )
    record = {
        key: _safe_scalar(source.get(key))
        for key in safe_keys
        if source.get(key) is not None
    }
    mappings = source.get("amsDetailMapping", source.get("ams_detail_mapping", []))
    safe_mappings = []
    if isinstance(mappings, list):
        for mapping in mappings:
            if not isinstance(mapping, Mapping):
                continue
            safe_mappings.append(
                {
                    key: _safe_scalar(mapping.get(key))
                    for key in (
                        "ams",
                        "amsId",
                        "slotId",
                        "nozzleId",
                        "filamentType",
                        "targetFilamentType",
                        "sourceColor",
                        "targetColor",
                        "weight",
                    )
                    if mapping.get(key) is not None
                }
            )
    record["ams_detail_mapping"] = safe_mappings
    record["cover_available"] = bool(
        source.get("cover_available") or source.get("cover")
    )
    return record


def _cloud_values(
    printer_id: str, cloud_id: str, record: Mapping[str, Any], imported_at: str
) -> tuple[Any, ...]:
    start = _datetime(record.get("startTime"))
    end = _datetime(record.get("endTime"))
    duration = (
        max(0, round((end - start).total_seconds()))
        if start and end and end >= start
        else None
    )
    mappings = record.get("ams_detail_mapping", [])
    materials = sorted(
        {
            str(item.get("filamentType"))
            for item in mappings
            if isinstance(item, Mapping) and item.get("filamentType")
        }
    )
    nozzles = sorted(
        {
            int(item["nozzleId"])
            for item in mappings
            if isinstance(item, Mapping) and isinstance(item.get("nozzleId"), int)
        }
    )
    status = record.get("status")
    result = (
        "completed"
        if status == 2
        else "aborted_or_failed"
        if status == 3
        else "unknown"
    )
    history_id = str(
        uuid5(NAMESPACE_URL, f"home-sensor:{printer_id}:bambu-cloud:{cloud_id}")
    )
    raw = {key: value for key, value in record.items() if key not in {"cloud_id"}}
    return (
        history_id,
        printer_id,
        cloud_id,
        _limited(record.get("title")),
        _limited(record.get("designTitle")),
        str(status) if status is not None else None,
        result,
        _iso(start),
        _iso(end),
        duration,
        _integer(record.get("costTime")),
        _number(record.get("weight")),
        _length_meters(record.get("length")),
        _integer(record.get("plateIndex")),
        _limited(record.get("plateName")),
        _limited(record.get("bedType")),
        _limited(record.get("mode")),
        _limited(record.get("deviceModel")),
        int(bool(record.get("cover_available"))),
        json.dumps(materials),
        json.dumps(mappings, sort_keys=True),
        json.dumps(nozzles),
        json.dumps(raw, sort_keys=True),
        imported_at,
    )


def _cloud_identity(record: Mapping[str, Any]) -> str:
    value = record.get("cloud_id", record.get("id"))
    if value is not None and str(value):
        return str(value)[:128]
    digest_source = json.dumps(
        {
            key: record.get(key)
            for key in ("title", "designTitle", "startTime", "endTime", "status")
        },
        sort_keys=True,
        default=str,
    )
    return "missing-id-" + hashlib.sha256(digest_source.encode()).hexdigest()[:32]


def _match_local_session(
    cloud: sqlite3.Row, local_rows: Sequence[sqlite3.Row], claimed: set[str]
) -> sqlite3.Row | None:
    for local in local_rows:
        if str(local["session_id"]) in claimed:
            continue
        if local["job_id"] and str(local["job_id"]) == str(cloud["cloud_id"]):
            return local
    cloud_start = _datetime(cloud["started_at_utc"])
    cloud_end = _datetime(cloud["ended_at_utc"])
    cloud_names = {
        _normalized_name(cloud["title"]),
        _normalized_name(cloud["design_title"]),
    } - {""}
    candidates = []
    for local in local_rows:
        if str(local["session_id"]) in claimed:
            continue
        local_start = _datetime(local["started_at_utc"])
        local_end = _datetime(local["ended_at_utc"])
        if not cloud_start or not local_start:
            continue
        name_matches = _normalized_name(local["job_name"]) in cloud_names
        close_start = abs((local_start - cloud_start).total_seconds()) <= 15 * 60
        overlaps = bool(
            cloud_end
            and local_end
            and local_start <= cloud_end
            and cloud_start <= local_end
        )
        if close_start and (name_matches or overlaps):
            candidates.append((abs((local_start - cloud_start).total_seconds()), local))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _local_history_dict(
    local: sqlite3.Row, cloud: sqlite3.Row | None
) -> dict[str, Any]:
    item = {
        "history_id": str(local["session_id"]),
        "printer_id": local["printer_id"],
        "job_id": local["job_id"],
        "job_name": local["job_name"],
        "started_at": local["started_at_utc"],
        "ended_at": local["ended_at_utc"],
        "duration_seconds": _interval_seconds(
            local["started_at_utc"], local["ended_at_utc"]
        ),
        "result": local["result"],
        "material": local["material"],
        "active_tool": local["active_tool"],
        "ams_slot": local["ams_slot"],
        "source": "locally_observed",
        "provenance": ["locally_observed"],
        "environment_summary_available": local["ended_at_utc"] is not None,
    }
    if cloud is not None:
        item.update(_cloud_metadata(cloud))
        item["provenance"] = ["locally_observed", CLOUD_SOURCE]
        item["cloud_result"] = cloud["result"]
    return item


def _cloud_history_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "history_id": row["history_id"],
        "printer_id": row["printer_id"],
        "job_id": row["cloud_id"],
        "job_name": row["title"] or row["design_title"],
        "started_at": row["started_at_utc"],
        "ended_at": row["ended_at_utc"],
        "duration_seconds": row["duration_seconds"],
        "result": row["result"],
        "material": ", ".join(json.loads(row["materials_json"])) or None,
        "active_tool": None,
        "ams_slot": None,
        "source": CLOUD_SOURCE,
        "provenance": [CLOUD_SOURCE],
        "environment_summary_available": bool(
            row["started_at_utc"] and row["ended_at_utc"]
        ),
        **_cloud_metadata(row),
    }


def _cloud_metadata(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cloud_id": row["cloud_id"],
        "design_title": row["design_title"],
        "slicer_estimated_seconds": row["slicer_estimated_seconds"],
        "weight_grams": row["weight_grams"],
        "length_meters": row["length_meters"],
        "plate_index": row["plate_index"],
        "plate_name": row["plate_name"],
        "bed_type": row["bed_type"],
        "print_source": row["print_source"],
        "device_model": row["device_model"],
        "cover_available": bool(row["cover_available"]),
        "materials": json.loads(row["materials_json"]),
        "ams_mapping": json.loads(row["ams_mapping_json"]),
        "nozzle_ids": json.loads(row["nozzle_ids_json"]),
        "imported_at": row["imported_at_utc"],
    }


def _maintenance_task_status(
    task: sqlite3.Row,
    events: Sequence[sqlite3.Row],
    usage: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    last = events[0] if events else None
    base_hours = float(last["effective_usage_hours"]) if last else 0.0
    base_prints = int(last["completed_print_count"]) if last else 0
    base_date = _datetime(last["completed_at_utc"] if last else task["created_at_utc"])
    current_hours = max(
        0.0, float(usage["maintenance_effective_lifetime_hours"]) - base_hours
    )
    current_prints = max(
        0, int(usage["locally_observed_completed_print_count"]) - base_prints
    )
    current_days = (
        max(0, int((now - base_date).total_seconds() // 86400)) if base_date else 0
    )
    metrics = []
    for kind, current, interval, warning in (
        (
            "operating_hours",
            current_hours,
            task["interval_hours"],
            task["warning_hours"],
        ),
        (
            "completed_prints",
            current_prints,
            task["interval_prints"],
            task["warning_prints"],
        ),
        ("calendar_days", current_days, task["interval_days"], task["warning_days"]),
    ):
        if interval is None:
            continue
        remaining = float(interval) - float(current)
        next_due_at = None
        if kind == "calendar_days" and base_date is not None:
            next_due_at = _iso(base_date + timedelta(days=float(interval)))
        metrics.append(
            {
                "trigger_type": kind,
                "interval": interval,
                "warning_threshold": warning,
                "current_accumulated_value": round(current, 4),
                "remaining": round(remaining, 4),
                "next_due_value": round(
                    (
                        base_hours
                        if kind == "operating_hours"
                        else base_prints
                        if kind == "completed_prints"
                        else 0
                    )
                    + float(interval),
                    4,
                ),
                "due": remaining <= 0,
                "overdue": remaining < 0,
                "warning": remaining > 0 and remaining <= float(warning),
                "next_due_at": next_due_at,
            }
        )
    due_flags = [bool(metric["due"]) for metric in metrics]
    due = all(due_flags) if task["due_when"] == "all" else any(due_flags)
    overdue_flags = [bool(metric["overdue"]) for metric in metrics]
    overdue = all(overdue_flags) if task["due_when"] == "all" else any(overdue_flags)
    warning = not due and any(
        bool(metric["warning"] or metric["due"]) for metric in metrics
    )
    return {
        "maintenance_task_id": task["task_id"],
        "name": task["name"],
        "description": task["description"],
        "enabled": bool(task["enabled"]),
        "due_when": task["due_when"],
        "state": "overdue"
        if overdue
        else "due"
        if due
        else "warning"
        if warning
        else "ok",
        "due": due,
        "overdue": overdue,
        "warning": warning,
        "triggers": metrics,
        "last_completed_at": last["completed_at_utc"] if last else None,
        "usage_hours_at_last_completion": last["effective_usage_hours"]
        if last
        else None,
        "print_count_at_last_completion": last["completed_print_count"]
        if last
        else None,
        "notes": task["notes"],
        "provenance": task["source"],
        "completion_count": len(events),
    }


def _maintenance_event_dict(event: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "maintenance_task_id": event["task_id"],
        "completed_at": event["completed_at_utc"],
        "usage_hours_at_completion": event["effective_usage_hours"],
        "print_count_at_completion": event["completed_print_count"],
        "notes": event["notes"],
        "recorded_by": event["recorded_by"],
    }


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _limited(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:1024]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _length_meters(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else number / 1000


def _normalized_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _interval_seconds(start: Any, end: Any) -> int | None:
    start_time = _datetime(start)
    end_time = _datetime(end)
    if not start_time or not end_time or end_time < start_time:
        return None
    return max(0, round((end_time - start_time).total_seconds()))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)
