"""Durable cloud-history, usage, and local maintenance intelligence."""

from __future__ import annotations

import hashlib
import json
import logging
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
from app.printer_maintenance import (
    DEFAULT_ROLLING_WINDOW_DAYS,
    EVENT_HEAVY_USE_ENTERED,
    EVENT_HEAVY_USE_EXITED,
    EVENT_RETURNED_TO_OK,
    LOCAL_POLICY_SOURCE,
    MANUFACTURER_SOURCE,
    MANUFACTURER_SOURCE_RETRIEVED,
    MANUFACTURER_SOURCE_REVISION,
    MANUFACTURER_SOURCE_URL,
    MINIMUM_MODE_HISTORY_DAYS,
    MODE_HEAVY_USE,
    PROBLEM_STATES,
    STATE_OK,
    TASK_STATE_EVENTS,
    TRIGGER_KINDS,
    TRIGGER_THRESHOLD,
    X2D_MAINTENANCE_TASKS,
    ManufacturerMaintenanceTask,
    maintenance_mode,
    maintenance_summary,
    task_status,
)

LOGGER = logging.getLogger("home_sensor.printer.intelligence")

CLOUD_TASKS_URL = "https://api.bambulab.com/v1/user-service/my/tasks"
CLOUD_SOURCE = "bambu_cloud_history"
CLOUD_RECONCILED_SOURCE = "bambu_cloud_history_reconciled"
LOCAL_SOURCE = "locally_observed"
LOCAL_SOURCES = frozenset({"locally_observed", "home_assistant"})
COUNTED_USAGE_RESULTS = frozenset({"completed", "failed", "cancelled"})
FAILED_OR_CANCELLED_RESULTS = frozenset(
    {"failed", "cancelled", "aborted_or_failed", "aborted"}
)
MAX_CLOUD_RESPONSE_BYTES = 16 * 1024 * 1024
TASK_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

# Tracked print time can never be proven complete: Bambu Cloud history has an
# unknown account/API retention boundary and local observation only begins when
# the observer is deployed.
TRACKED_COMPLETENESS_REASONS = (
    "bambu_cloud_history_retention_boundary_unknown",
    "local_observation_starts_at_observer_deployment",
    "no_authoritative_printer_lifetime_counter_available",
)

FILAMENT_COMPLETENESS_REASONS = (
    "bambu_cloud_history_retention_boundary_unknown",
    "filament_amount_is_a_slicer_estimate_not_a_measurement",
    "locally_observed_only_jobs_carry_no_filament_amount",
)



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
    def __init__(
        self,
        database_path: Path,
        *,
        rolling_window_days: int = DEFAULT_ROLLING_WINDOW_DAYS,
        minimum_mode_history_days: int = MINIMUM_MODE_HISTORY_DAYS,
    ) -> None:
        self.database_path = Path(database_path)
        self.rolling_window_days = max(1, int(rolling_window_days))
        self.minimum_mode_history_days = max(0, int(minimum_mode_history_days))

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

                -- Durable, append-only maintenance lifecycle events. Delivery
                -- is recorded here so a restart never resends an old state.
                CREATE TABLE IF NOT EXISTS maintenance_notification_events (
                    event_id TEXT PRIMARY KEY,
                    printer_id TEXT,
                    subject_key TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    previous_state TEXT,
                    new_state TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    delivery_status TEXT NOT NULL DEFAULT 'pending',
                    delivered_at_utc TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_maintenance_notification_events_created
                    ON maintenance_notification_events(created_at_utc DESC, event_id DESC);
                CREATE INDEX IF NOT EXISTS idx_maintenance_notification_events_pending
                    ON maintenance_notification_events(delivery_status, created_at_utc);

                -- Edge-trigger memory: one row per notifiable subject.
                CREATE TABLE IF NOT EXISTS maintenance_notification_state (
                    subject_key TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    last_state TEXT NOT NULL,
                    last_event_id TEXT,
                    updated_at_utc TEXT NOT NULL
                );
                """
            )
            self._migrate_maintenance_task_columns(connection)
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_maintenance_task_columns(connection: sqlite3.Connection) -> None:
        """Add trigger/provenance columns to an existing maintenance table.

        Idempotent and additive: existing rows keep their generic threshold
        behaviour because every new column has a compatible default.
        """

        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(maintenance_tasks)")
        }
        additions = (
            ("trigger_kind", f"TEXT NOT NULL DEFAULT '{TRIGGER_THRESHOLD}'"),
            ("interval_months", "INTEGER"),
            ("interval_months_low_use", "INTEGER"),
            ("interval_months_normal_use", "INTEGER"),
            ("interval_months_heavy_use", "INTEGER"),
            ("prerequisite_task_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("cadence", "TEXT NOT NULL DEFAULT ''"),
            ("source_url", "TEXT NOT NULL DEFAULT ''"),
            ("source_revision", "TEXT NOT NULL DEFAULT ''"),
            ("warning_source", f"TEXT NOT NULL DEFAULT '{LOCAL_POLICY_SOURCE}'"),
        )
        for column, definition in additions:
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE maintenance_tasks ADD COLUMN {column} {definition}"
                )

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

    def tracked_runtime(
        self, printer_id: str | None = None, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Canonical cumulative print time from known, deduplicated intervals."""

        with self._connect() as connection:
            return self._tracked_runtime(connection, printer_id, now=now)

    def _tracked_runtime(
        self,
        connection: sqlite3.Connection,
        printer_id: str | None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        local_rows = connection.execute(
            f"""SELECT session_id, started_at_utc, ended_at_utc, result
                FROM print_sessions
                WHERE source IN ({",".join("?" * len(LOCAL_SOURCES))})
                  AND (? IS NULL OR printer_id = ?)""",
            (*sorted(LOCAL_SOURCES), printer_id, printer_id),
        ).fetchall()
        cloud_rows = connection.execute(
            """SELECT history_id, started_at_utc, ended_at_utc, result,
                      reconciled_session_id
               FROM cloud_print_history WHERE (? IS NULL OR printer_id = ?)""",
            (printer_id, printer_id),
        ).fetchall()

        cloud_by_session = {
            str(row["reconciled_session_id"]): row
            for row in cloud_rows
            if row["reconciled_session_id"]
        }
        intervals: list[dict[str, Any]] = []
        unknown_interval_jobs = 0
        # A reconciled cloud task and its local session are the same physical
        # print. It contributes exactly one interval: the local one when it is
        # known, otherwise the cloud fallback.
        for row in local_rows:
            interval = _valid_interval(row["started_at_utc"], row["ended_at_utc"])
            source = LOCAL_SOURCE
            result = row["result"]
            cloud = cloud_by_session.get(str(row["session_id"]))
            if interval is None and cloud is not None:
                interval = _valid_interval(
                    cloud["started_at_utc"], cloud["ended_at_utc"]
                )
                source = CLOUD_RECONCILED_SOURCE
                result = result or cloud["result"]
            if interval is None:
                unknown_interval_jobs += 1
                continue
            intervals.append({**interval, "source": source, "result": result})
        for row in cloud_rows:
            if row["reconciled_session_id"]:
                continue
            interval = _valid_interval(row["started_at_utc"], row["ended_at_utc"])
            if interval is None:
                unknown_interval_jobs += 1
                continue
            intervals.append(
                {**interval, "source": CLOUD_SOURCE, "result": row["result"]}
            )

        total_seconds = sum(int(item["seconds"]) for item in intervals)
        completed = sum(1 for item in intervals if item["result"] == "completed")
        failed_or_cancelled = sum(
            1
            for item in intervals
            if str(item["result"] or "") in FAILED_OR_CANCELLED_RESULTS
        )
        unknown_result = len(intervals) - completed - failed_or_cancelled
        first_start = min((item["start"] for item in intervals), default=None)
        last_end = max((item["end"] for item in intervals), default=None)
        sources = sorted({str(item["source"]) for item in intervals})

        window_start = now - timedelta(days=self.rolling_window_days)
        rolling_seconds = sum(
            _overlap_seconds(item["start"], item["end"], window_start, now)
            for item in intervals
        )
        history_days: float | None = None
        hours_per_day: float | None = None
        if first_start is not None:
            observed_days = max(0.0, (now - first_start).total_seconds() / 86400)
            history_days = min(float(self.rolling_window_days), observed_days)
            if history_days > 0:
                hours_per_day = (rolling_seconds / 3600) / history_days
        mode, mode_reason = maintenance_mode(
            hours_per_day,
            history_days=history_days,
            minimum_history_days=self.minimum_mode_history_days,
        )
        return {
            "tracked_print_seconds": total_seconds,
            "tracked_print_hours": round(total_seconds / 3600, 4),
            "tracked_job_count": len(intervals),
            "tracked_completed_count": completed,
            "tracked_failed_or_cancelled_count": failed_or_cancelled,
            "tracked_unknown_result_count": unknown_result,
            "tracked_unknown_interval_job_count": unknown_interval_jobs,
            "tracked_first_print_at": _iso(first_start),
            "tracked_last_print_at": _iso(last_end),
            "tracked_history_complete": False,
            "tracked_history_completeness_reasons": list(TRACKED_COMPLETENESS_REASONS),
            "tracked_history_provenance": sources,
            "tracked_semantics": (
                "sum of known actual print-history intervals (ended_at minus "
                "started_at) across locally observed sessions and imported Bambu "
                "Cloud tasks, deduplicated by canonical reconciliation; slicer "
                "estimates and remaining-time predictions are never used"
            ),
            "rolling_window_days": self.rolling_window_days,
            "rolling_window_source": LOCAL_POLICY_SOURCE,
            "rolling_tracked_print_hours": round(rolling_seconds / 3600, 4),
            "rolling_tracked_history_days": (
                None if history_days is None else round(history_days, 4)
            ),
            "rolling_tracked_print_hours_per_day": (
                None if hours_per_day is None else round(hours_per_day, 4)
            ),
            "maintenance_mode": mode,
            "maintenance_mode_reason": mode_reason,
            "maintenance_mode_source": MANUFACTURER_SOURCE,
            "maintenance_mode_thresholds": {
                "heavy_use_hours_per_day_at_least": 5.0,
                "low_use_hours_per_day_below": 1.0,
                "minimum_history_days": self.minimum_mode_history_days,
                "minimum_history_days_source": LOCAL_POLICY_SOURCE,
            },
        }

    def _tracked_filament(
        self, connection: sqlite3.Connection, printer_id: str | None
    ) -> dict[str, Any]:
        """Aggregate filament over the same canonical jobs as the runtime total.

        Two source facts shape every decision here, both established from the
        deployed history rather than from field names:

        * ``weight_grams`` is the slicer's plan for the job, not measured
          consumption. A job aborted after two seconds still reports 154.5 g.
          Counting an aborted job's weight as filament used would therefore
          overstate the total badly, so only completed jobs contribute and the
          rest are counted as jobs whose real consumption is unknown.
        * ``ams_mapping`` carries a per-slot ``filamentType`` and ``weight``
          whose sum equals ``weight_grams`` exactly across every stored record,
          so a multi-material job needs no guessing and no even division. A job
          that has a weight but no usable mapping is tracked as unallocated
          rather than being spread across the materials it listed.

        Mass exists only on the cloud rows: the local observer records material
        and AMS slot but no weight, so a locally-only job is a known print with
        unknown filament. Deduplication is inherited from the same
        ``reconciled_session_id`` join the runtime total uses, so a print seen
        both locally and in the cloud is counted exactly once.
        """

        local_rows = connection.execute(
            f"""SELECT session_id, result
                FROM print_sessions
                WHERE source IN ({",".join("?" * len(LOCAL_SOURCES))})
                  AND (? IS NULL OR printer_id = ?)""",
            (*sorted(LOCAL_SOURCES), printer_id, printer_id),
        ).fetchall()
        cloud_rows = connection.execute(
            """SELECT history_id, result, started_at_utc, weight_grams,
                      length_meters, materials_json, ams_mapping_json,
                      reconciled_session_id
               FROM cloud_print_history WHERE (? IS NULL OR printer_id = ?)""",
            (printer_id, printer_id),
        ).fetchall()

        cloud_by_session = {
            str(row["reconciled_session_id"]): row
            for row in cloud_rows
            if row["reconciled_session_id"]
        }

        jobs: list[dict[str, Any]] = []
        for row in local_rows:
            cloud = cloud_by_session.get(str(row["session_id"]))
            jobs.append(
                {
                    "result": row["result"] or (cloud["result"] if cloud else None),
                    "cloud": cloud,
                }
            )
        for row in cloud_rows:
            if row["reconciled_session_id"]:
                continue
            jobs.append({"result": row["result"], "cloud": row})

        total_grams = 0.0
        total_metres = 0.0
        counted = 0
        unallocated_grams = 0.0
        by_material: dict[str, dict[str, Any]] = {}
        unknown_amount_jobs = 0
        incomplete_jobs = 0
        first_at: str | None = None
        for job in jobs:
            completed = str(job["result"] or "") == "completed"
            cloud = job["cloud"]
            grams = None if cloud is None else cloud["weight_grams"]
            if not completed:
                # Real filament was used, but the stored number is the plan for
                # the whole job, so the amount actually consumed is unknown.
                incomplete_jobs += 1
                continue
            if grams is None or float(grams) <= 0:
                unknown_amount_jobs += 1
                continue
            grams = float(grams)
            total_grams += grams
            counted += 1
            if cloud["length_meters"] is not None:
                total_metres += float(cloud["length_meters"])
            started = cloud["started_at_utc"]
            if started and (first_at is None or str(started) < first_at):
                first_at = str(started)
            allocated = 0.0
            contributed: set[str] = set()
            for entry in _json_list(cloud["ams_mapping_json"]):
                if not isinstance(entry, Mapping):
                    continue
                weight = entry.get("weight")
                try:
                    weight = float(weight)
                except (TypeError, ValueError):
                    continue
                if weight <= 0:
                    # A slot that was mapped but contributed nothing.
                    continue
                material = _normalize_material(entry.get("filamentType"))
                bucket = by_material.setdefault(
                    material["material"],
                    {
                        "material": material["material"],
                        "family": material["family"],
                        "variant": material["variant"],
                        "raw_names": [],
                        "grams": 0.0,
                        "job_count": 0,
                    },
                )
                bucket["grams"] += weight
                if material["raw"] and material["raw"] not in bucket["raw_names"]:
                    bucket["raw_names"].append(material["raw"])
                allocated += weight
                contributed.add(material["material"])
            if allocated <= 0:
                # A weight with no usable per-slot breakdown. Dividing it across
                # the materials the job listed would invent figures the source
                # never gave, so it stays known-but-unallocated.
                unallocated_grams += grams
            for name in contributed:
                # Once per job per material, not once per tray: a job can map
                # the same filament into several slots.
                by_material[name]["job_count"] += 1

        materials = sorted(
            (
                {
                    **bucket,
                    "grams": round(bucket["grams"], 3),
                    "kilograms": round(bucket["grams"] / 1000, 4),
                }
                for bucket in by_material.values()
            ),
            key=lambda item: (-item["grams"], item["material"]),
        )
        return {
            "tracked_filament_estimate_g": round(total_grams, 3),
            "tracked_filament_estimate_kg": round(total_grams / 1000, 4),
            "tracked_filament_length_m": round(total_metres, 3),
            "tracked_filament_job_count": counted,
            "tracked_filament_unknown_amount_job_count": unknown_amount_jobs,
            "tracked_filament_incomplete_job_count": incomplete_jobs,
            "tracked_filament_unallocated_g": round(unallocated_grams, 3),
            "tracked_filament_first_job_at": first_at,
            "tracked_filament_history_complete": False,
            "tracked_filament_history_completeness_reasons": list(
                FILAMENT_COMPLETENESS_REASONS
            ),
            "tracked_filament_by_material": materials,
            "tracked_filament_measured": False,
            "tracked_filament_semantics": (
                "sum of Bambu's per-job slicer filament estimate over completed "
                "prints only, deduplicated by the same canonical reconciliation "
                "as tracked print time. It is an estimate of planned filament, "
                "not weighed consumption. Prints that did not complete are "
                "counted separately because their stored weight is the plan for "
                "the whole job rather than the part that printed."
            ),
        }

    def usage_summary(
        self, printer_id: str | None = None, *, now: datetime | None = None
    ) -> dict[str, Any]:
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
            tracked = self._tracked_runtime(connection, printer_id, now=now)
            filament = self._tracked_filament(connection, printer_id)
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
            **tracked,
            **filament,
            "printer_id": printer_id,
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
                "tracked_print_hours": (
                    "canonical deduplicated print time across local and cloud "
                    "history; counts any known interval regardless of outcome and "
                    "is not the printer's lifetime counter"
                ),
            },
        }

    def sync_maintenance_tasks(
        self,
        tasks: Sequence[MaintenanceTaskSettings],
        *,
        now: datetime,
        manufacturer_tasks: Sequence[ManufacturerMaintenanceTask] | None = None,
    ) -> None:
        """Seed the manufacturer catalog, then apply local configuration.

        Configured tasks are written last so an operator can override or
        disable a manufacturer entry by reusing its task id.
        """

        now_text = _iso(now)
        catalog = (
            X2D_MAINTENANCE_TASKS if manufacturer_tasks is None else manufacturer_tasks
        )
        with self._transaction() as connection:
            # Configuration is authoritative for whether a task remains active.
            # Audit events are retained even when a task is removed or disabled.
            connection.execute(
                "UPDATE maintenance_tasks SET enabled=0, updated_at_utc=?",
                (now_text,),
            )
            for manufacturer_task in catalog:
                self._upsert_task(
                    connection,
                    _manufacturer_task_values(manufacturer_task),
                    now_text=now_text,
                )
            for task in tasks:
                self._upsert_task(
                    connection, _configured_task_values(task), now_text=now_text
                )

    @staticmethod
    def _upsert_task(
        connection: sqlite3.Connection,
        values: Mapping[str, Any],
        *,
        now_text: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO maintenance_tasks (
                task_id, name, description, interval_hours, warning_hours,
                interval_prints, warning_prints, interval_days, warning_days,
                due_when, notes, source, enabled, created_at_utc, updated_at_utc,
                trigger_kind, interval_months, interval_months_low_use,
                interval_months_normal_use, interval_months_heavy_use,
                prerequisite_task_ids, cadence, source_url, source_revision,
                warning_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
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
                updated_at_utc=excluded.updated_at_utc,
                trigger_kind=excluded.trigger_kind,
                interval_months=excluded.interval_months,
                interval_months_low_use=excluded.interval_months_low_use,
                interval_months_normal_use=excluded.interval_months_normal_use,
                interval_months_heavy_use=excluded.interval_months_heavy_use,
                prerequisite_task_ids=excluded.prerequisite_task_ids,
                cadence=excluded.cadence, source_url=excluded.source_url,
                source_revision=excluded.source_revision,
                warning_source=excluded.warning_source
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
                values["trigger_kind"],
                values["interval_months"],
                values["interval_months_low_use"],
                values["interval_months_normal_use"],
                values["interval_months_heavy_use"],
                values["prerequisite_task_ids"],
                values["cadence"],
                values["source_url"],
                values["source_revision"],
                values["warning_source"],
            ),
        )

    def maintenance(
        self, printer_id: str | None = None, *, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        usage = self.usage_summary(printer_id, now=now)
        with self._connect() as connection:
            statuses, events = self._task_statuses(connection, usage, now=now)
            alerts = connection.execute(
                """SELECT * FROM maintenance_notification_events
                   ORDER BY created_at_utc DESC, event_id DESC LIMIT 50"""
            ).fetchall()
        return {
            "available": True,
            "usage": usage,
            "summary": maintenance_summary(statuses, usage=usage),
            "tasks": statuses,
            "completion_history": [_maintenance_event_dict(event) for event in events],
            "recent_notifications": [
                _notification_event_dict(event) for event in alerts
            ],
            "manufacturer_source": {
                "source": MANUFACTURER_SOURCE,
                "url": MANUFACTURER_SOURCE_URL,
                "revision": MANUFACTURER_SOURCE_REVISION,
                "retrieved_at": MANUFACTURER_SOURCE_RETRIEVED,
            },
            "local_record_only": True,
            "printer_control": False,
        }

    def _task_statuses(
        self,
        connection: sqlite3.Connection,
        usage: Mapping[str, Any],
        *,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], list[sqlite3.Row]]:
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
        last_completion_by_task = {
            task_id: _datetime(rows[0]["completed_at_utc"])
            for task_id, rows in events_by_task.items()
            if _datetime(rows[0]["completed_at_utc"]) is not None
        }
        statuses = [
            task_status(
                task,
                events_by_task.get(str(task["task_id"]), []),
                usage,
                now=now,
                mode=str(usage.get("maintenance_mode") or "normal"),
                last_completion_by_task=last_completion_by_task,
            )
            for task in tasks
        ]
        return statuses, events

    def complete_maintenance(
        self,
        task_id: str,
        *,
        notes: str,
        completed_at: datetime,
        printer_id: str | None = None,
        recorded_by: str = "dashboard_user",
    ) -> dict[str, Any]:
        if not TASK_ID_RE.fullmatch(task_id):
            raise PrinterIntelligenceError("invalid maintenance task id")
        result = self._complete(
            [task_id],
            notes=notes,
            completed_at=completed_at,
            printer_id=printer_id,
            recorded_by=recorded_by,
        )
        completion = result["completions"][0]
        return {
            "event_id": completion["event_id"],
            "task_id": task_id,
            "completed_at": completion["completed_at"],
            "notification_events": result["notification_events"],
            "local_record_only": True,
            "printer_control": False,
        }

    def complete_all_maintenance(
        self,
        *,
        notes: str,
        completed_at: datetime,
        printer_id: str | None = None,
        recorded_by: str = "dashboard_user",
    ) -> dict[str, Any]:
        """Record one local completion per enabled task (baseline helper)."""

        with self._connect() as connection:
            task_ids = [
                str(row["task_id"])
                for row in connection.execute(
                    "SELECT task_id FROM maintenance_tasks WHERE enabled=1 ORDER BY task_id"
                )
            ]
        if not task_ids:
            raise PrinterIntelligenceError("no enabled maintenance tasks exist")
        result = self._complete(
            task_ids,
            notes=notes,
            completed_at=completed_at,
            printer_id=printer_id,
            recorded_by=recorded_by,
        )
        return {
            "completed_task_count": len(result["completions"]),
            "completions": result["completions"],
            "notification_events": result["notification_events"],
            "local_record_only": True,
            "printer_control": False,
        }

    def _complete(
        self,
        task_ids: Sequence[str],
        *,
        notes: str,
        completed_at: datetime,
        printer_id: str | None,
        recorded_by: str,
    ) -> dict[str, Any]:
        if len(notes) > 2000:
            raise PrinterIntelligenceError("maintenance notes exceed 2000 characters")
        safe_recorded_by = str(recorded_by).strip()[:64] or "dashboard_user"
        usage = self.usage_summary(printer_id, now=completed_at)
        completions: list[dict[str, Any]] = []
        with self._transaction() as connection:
            for task_id in task_ids:
                task = connection.execute(
                    "SELECT * FROM maintenance_tasks WHERE task_id=? AND enabled=1",
                    (task_id,),
                ).fetchone()
                if task is None:
                    raise PrinterIntelligenceError(
                        "unknown or disabled maintenance task"
                    )
                event_id = str(uuid4())
                connection.execute(
                    """INSERT INTO maintenance_completion_events (
                           event_id, task_id, completed_at_utc,
                           effective_usage_hours, completed_print_count, notes,
                           recorded_by
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        task_id,
                        _iso(completed_at),
                        usage["maintenance_effective_lifetime_hours"],
                        usage["locally_observed_completed_print_count"],
                        notes.strip(),
                        safe_recorded_by,
                    ),
                )
                completions.append(
                    {
                        "event_id": event_id,
                        "task_id": task_id,
                        "completed_at": _iso(completed_at),
                    }
                )
        # Completion resets the lifecycle, so re-evaluating here keeps the
        # durable event log and the dashboard consistent immediately.
        events = self.evaluate_maintenance_events(printer_id, now=completed_at)
        return {"completions": completions, "notification_events": events}

    def evaluate_maintenance_events(
        self, printer_id: str | None = None, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Append one durable event per genuine state transition.

        Edge-triggered: repeated evaluation of an unchanged state, including
        across a service restart, never appends a second event.
        """

        now = now or datetime.now(timezone.utc)
        usage = self.usage_summary(printer_id, now=now)
        created: list[dict[str, Any]] = []
        with self._transaction() as connection:
            statuses, _ = self._task_statuses(connection, usage, now=now)
            observations = [
                (
                    f"maintenance_task:{status['maintenance_task_id']}",
                    "maintenance_task",
                    str(status["maintenance_task_id"]),
                    str(status["state"]),
                    {
                        "name": status["name"],
                        "cadence": status["cadence"],
                        "next_due_at": status["next_due_at"],
                        "remaining_days": status["remaining_days"],
                        "trigger_kind": status["trigger_kind"],
                        "manufacturer_source": status["manufacturer_source"],
                    },
                )
                for status in statuses
                if status["enabled"]
            ]
            resolved_printer_id = str(
                usage.get("printer_id") or printer_id or "printer"
            )
            observations.append(
                (
                    f"maintenance_mode:{resolved_printer_id}",
                    "printer",
                    resolved_printer_id,
                    str(usage.get("maintenance_mode") or "normal"),
                    {
                        "maintenance_mode_reason": usage.get("maintenance_mode_reason"),
                        "rolling_tracked_print_hours_per_day": usage.get(
                            "rolling_tracked_print_hours_per_day"
                        ),
                        "rolling_window_days": usage.get("rolling_window_days"),
                    },
                )
            )
            for subject_key, subject_type, subject_id, state, payload in observations:
                row = connection.execute(
                    "SELECT last_state FROM maintenance_notification_state WHERE subject_key=?",
                    (subject_key,),
                ).fetchone()
                previous = None if row is None else str(row["last_state"])
                if previous == state:
                    continue
                event_type = (
                    _mode_event_type(previous, state)
                    if subject_type == "printer"
                    else _task_event_type(previous, state)
                )
                event_id = str(uuid4()) if event_type else None
                if event_type and event_id:
                    connection.execute(
                        """INSERT INTO maintenance_notification_events (
                               event_id, printer_id, subject_key, subject_type,
                               subject_id, event_type, previous_state, new_state,
                               created_at_utc, payload_json
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event_id,
                            resolved_printer_id,
                            subject_key,
                            subject_type,
                            subject_id,
                            event_type,
                            previous,
                            state,
                            _iso(now),
                            json.dumps(payload, sort_keys=True, default=str),
                        ),
                    )
                    created.append(
                        {
                            "event_id": event_id,
                            "printer_id": resolved_printer_id,
                            "subject_type": subject_type,
                            "subject_id": subject_id,
                            "event_type": event_type,
                            "previous_state": previous,
                            "new_state": state,
                            "created_at": _iso(now),
                            "payload": payload,
                            "delivery_status": "pending",
                        }
                    )
                connection.execute(
                    """INSERT INTO maintenance_notification_state (
                           subject_key, subject_type, subject_id, last_state,
                           last_event_id, updated_at_utc
                       ) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(subject_key) DO UPDATE SET
                           last_state=excluded.last_state,
                           last_event_id=COALESCE(excluded.last_event_id, maintenance_notification_state.last_event_id),
                           updated_at_utc=excluded.updated_at_utc""",
                    (
                        subject_key,
                        subject_type,
                        subject_id,
                        state,
                        event_id,
                        _iso(now),
                    ),
                )
        return created

    def notification_events(
        self,
        *,
        limit: int = 100,
        pending_only: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM maintenance_notification_events"
        if pending_only:
            query += " WHERE delivery_status = 'pending'"
        query += " ORDER BY created_at_utc DESC, event_id DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, (max(1, min(limit, 500)),)).fetchall()
        return [_notification_event_dict(row) for row in rows]

    def mark_notification_delivered(
        self, event_id: str, *, delivered_at: datetime, status: str = "delivered"
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE maintenance_notification_events
                   SET delivery_status=?, delivered_at_utc=? WHERE event_id=?""",
                (str(status)[:32], _iso(delivered_at), event_id),
            )

    def dispatch_notifications(
        self,
        notifier: Any,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> int:
        """Hand pending events to a notifier without losing undelivered state."""

        now = now or datetime.now(timezone.utc)
        delivered = 0
        for event in reversed(self.notification_events(limit=limit, pending_only=True)):
            try:
                status = str(notifier.deliver(event) or "delivered")
            except Exception:
                # A broken transport must not abort the loop or lose the
                # event; it stays pending and is retried next cycle.
                LOGGER.exception(
                    "maintenance notification delivery failed: %s", event["event_id"]
                )
                continue
            self.mark_notification_delivered(
                str(event["event_id"]), delivered_at=now, status=status
            )
            delivered += 1
        return delivered

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


def _manufacturer_task_values(
    task: ManufacturerMaintenanceTask,
) -> dict[str, Any]:
    if task.trigger_kind not in TRIGGER_KINDS:
        raise PrinterIntelligenceError("unknown maintenance trigger kind")
    intervals = dict(task.interval_months_by_mode)
    return {
        "task_id": task.task_id,
        "name": task.name,
        "description": task.description,
        "interval_hours": None,
        "warning_hours": 0.0,
        "interval_prints": None,
        "warning_prints": 0,
        "interval_days": None,
        "warning_days": task.warning_days,
        "due_when": "any",
        "notes": task.notes,
        "source": task.source,
        "enabled": True,
        "trigger_kind": task.trigger_kind,
        "interval_months": task.interval_months,
        "interval_months_low_use": intervals.get("low_use"),
        "interval_months_normal_use": intervals.get("normal"),
        "interval_months_heavy_use": intervals.get("heavy_use"),
        "prerequisite_task_ids": json.dumps(list(task.prerequisite_task_ids)),
        "cadence": task.cadence,
        "source_url": task.source_url,
        "source_revision": task.source_revision,
        # Warning lead time is a dashboard courtesy, never a Bambu Lab value.
        "warning_source": LOCAL_POLICY_SOURCE,
    }


def _configured_task_values(task: MaintenanceTaskSettings) -> dict[str, Any]:
    values = asdict(task)
    return {
        **values,
        "trigger_kind": TRIGGER_THRESHOLD,
        "interval_months": None,
        "interval_months_low_use": None,
        "interval_months_normal_use": None,
        "interval_months_heavy_use": None,
        "prerequisite_task_ids": "[]",
        "cadence": _configured_cadence(task),
        "source_url": "",
        "source_revision": "",
        "warning_source": LOCAL_POLICY_SOURCE,
    }


def _configured_cadence(task: MaintenanceTaskSettings) -> str:
    parts = [
        f"{value:g} {unit}"
        for value, unit in (
            (task.interval_hours, "operating hours"),
            (task.interval_prints, "completed prints"),
            (task.interval_days, "days"),
        )
        if value is not None
    ]
    joiner = " and " if task.due_when == "all" else " or "
    return f"Every {joiner.join(parts)}" if parts else ""


def _valid_interval(start: Any, end: Any) -> dict[str, Any] | None:
    """Return a known machine-operating interval, or None when unknowable."""

    start_time = _datetime(start)
    end_time = _datetime(end)
    if start_time is None or end_time is None or end_time < start_time:
        return None
    return {
        "start": start_time,
        "end": end_time,
        "seconds": max(0, round((end_time - start_time).total_seconds())),
    }


def _overlap_seconds(
    start: datetime, end: datetime, window_start: datetime, window_end: datetime
) -> int:
    first = max(start, window_start)
    last = min(end, window_end)
    if last <= first:
        return 0
    return max(0, round((last - first).total_seconds()))


def _task_event_type(previous: str | None, state: str) -> str | None:
    if state in TASK_STATE_EVENTS:
        return TASK_STATE_EVENTS[state]
    if state == STATE_OK and previous in PROBLEM_STATES:
        return EVENT_RETURNED_TO_OK
    return None


def _mode_event_type(previous: str | None, state: str) -> str | None:
    if state == MODE_HEAVY_USE:
        return EVENT_HEAVY_USE_ENTERED
    if previous == MODE_HEAVY_USE:
        return EVENT_HEAVY_USE_EXITED
    return None


def _notification_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return {
        "event_id": row["event_id"],
        "printer_id": row["printer_id"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "event_type": row["event_type"],
        "previous_state": row["previous_state"],
        "new_state": row["new_state"],
        "created_at": row["created_at_utc"],
        "payload": payload if isinstance(payload, dict) else {},
        "delivery_status": row["delivery_status"],
        "delivered_at": row["delivered_at_utc"],
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



def _json_list(raw: Any) -> list[Any]:
    """Decode a stored JSON array, treating anything unusable as empty."""

    if isinstance(raw, (list, tuple)):
        return list(raw)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _normalize_material(raw: Any) -> dict[str, str]:
    """Split a filament name into family and variant without losing the name.

    Bambu sends a free-form string, so nothing here may assume a fixed set.
    PETG and PETG-ESD have to stay distinguishable, as do PLA and PLA-CF, so
    the material key keeps the full designation and the family is offered
    alongside it for grouping rather than instead of it.
    """

    text = str(raw or "").strip()
    if not text:
        return {"material": "unknown", "family": "unknown", "variant": "", "raw": ""}
    designation = text.upper().replace("_", "-")
    family, separator, variant = designation.partition("-")
    return {
        "material": designation,
        "family": family if separator else designation,
        "variant": variant,
        "raw": text,
    }

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
