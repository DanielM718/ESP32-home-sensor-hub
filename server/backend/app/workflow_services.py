"""Application services shared by monitoring and arbitrary export APIs."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.capabilities import validate_source_capabilities
from app.persistence import MonitoringExportStore
from app.workflows import (
    DEFAULT_RAW_RETENTION_SECONDS,
    WorkflowConflictError,
    iso_utc,
    parse_stored_time,
    sources_from_json,
    validate_export_request,
    validate_monitoring_request,
)

Clock = Callable[[], datetime]
CapabilityResolver = Callable[[], Mapping[str, Any]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExportService:
    def __init__(
        self,
        store: MonitoringExportStore,
        *,
        clock: Clock = utc_now,
        raw_retention_seconds: int = DEFAULT_RAW_RETENTION_SECONDS,
        capability_resolver: CapabilityResolver | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.raw_retention_seconds = raw_retention_seconds
        self.capability_resolver = capability_resolver

    def serialize_time(self) -> str:
        return iso_utc(self.clock())

    def create(self, payload: Any) -> dict[str, Any]:
        now = self.clock()
        request = validate_export_request(
            payload,
            now=now,
            raw_retention_seconds=self.raw_retention_seconds,
        )
        if self.capability_resolver is not None:
            validate_source_capabilities(
                request.sources,
                request.fields,
                self.capability_resolver(),
            )
        return self.serialize(self.store.create_export(request, now=now), now=now)

    def list(self) -> list[dict[str, Any]]:
        now = self.clock()
        return [self.serialize(job, now=now) for job in self.store.list_exports()]

    def get(self, job_id: str) -> dict[str, Any]:
        now = self.clock()
        return self.serialize(self.store.get_export(job_id), now=now)

    def cancel(self, job_id: str) -> dict[str, Any]:
        now = self.clock()
        job = self.store.request_cancel(job_id, now=now)
        if job["status"] == "cancelled":
            self.cleanup_paths(job_id)
        return self.serialize(job, now=now)

    def delete(self, job_id: str) -> None:
        job = self.store.get_export(job_id)
        if job["monitoring_session_id"] is not None:
            raise WorkflowConflictError(
                "automatic monitoring exports are deleted with their monitoring session"
            )
        if job["status"] not in {"completed", "failed", "cancelled"}:
            raise WorkflowConflictError(
                "only completed, failed, or cancelled exports can be deleted"
            )
        self.cleanup_paths(job_id)
        self.store.delete_export(job_id)

    def download(self, job_id: str) -> tuple[Path, str]:
        job = self.store.get_export(job_id)
        final_path, _partial_path = self.safe_paths(job_id)
        if job["status"] != "completed" or not final_path.is_file():
            raise WorkflowConflictError("the completed CSV file is not available")
        return final_path, download_filename(job)

    def serialize(
        self, job: Mapping[str, Any], *, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or self.clock()
        final_path, _partial_path = self.safe_paths(str(job["id"]))
        created = parse_stored_time(job.get("created_at_utc"))
        started = parse_stored_time(job.get("started_at_utc"))
        completed = parse_stored_time(job.get("completed_at_utc"))
        status = str(job["status"])
        final_time = (
            completed if status in {"completed", "failed", "cancelled"} else now
        )
        queued_end = started or final_time
        queued_seconds = _seconds_between(created, queued_end)
        active_seconds = _seconds_between(started, final_time) if started else 0
        total_seconds = _seconds_between(created, final_time)
        is_ready = status == "completed" and final_path.is_file()
        return {
            "id": job["id"],
            "monitoring_session_id": job.get("monitoring_session_id"),
            "name": job["name"],
            "status": status,
            "start_time_utc": job["start_time_utc"],
            "end_time_utc": job["end_time_utc"],
            "selected_sources": list(job["selected_sources"]),
            "selected_fields": list(job["selected_fields"]),
            "source_count": len(job["selected_sources"]),
            "field_count": len(job["selected_fields"]),
            "resolution": job["resolution"],
            "csv_format": job["csv_format"],
            "output_size_bytes": int(job["output_size_bytes"]),
            "rows_written": int(job["rows_written"]),
            "work_units_total": int(job["work_units_total"]),
            "work_units_completed": int(job["work_units_completed"]),
            "current_phase": job["current_phase"],
            "warnings": list(job["warnings"]),
            "source_results": list(job["source_results"]),
            "error_message": job.get("error_message"),
            "attempt_count": int(job.get("attempt_count") or 0),
            "created_at_utc": job["created_at_utc"],
            "started_at_utc": job.get("started_at_utc"),
            "completed_at_utc": job.get("completed_at_utc"),
            "heartbeat_at_utc": job.get("heartbeat_at_utc"),
            "updated_at_utc": job["updated_at_utc"],
            "server_time_utc": iso_utc(now),
            "queued_elapsed_seconds": queued_seconds,
            "active_elapsed_seconds": active_seconds,
            "total_elapsed_seconds": total_seconds,
            "is_download_ready": is_ready,
            "download_url": f"/api/exports/{job['id']}/download" if is_ready else None,
            "download_filename": download_filename(job) if is_ready else None,
        }

    def safe_paths(self, job_id: str) -> tuple[Path, Path]:
        base = self.store.output_dir.resolve()
        final_path = (base / f"{job_id}.csv").resolve()
        partial_path = (base / f"{job_id}.csv.part").resolve()
        if final_path.parent != base or partial_path.parent != base:
            raise WorkflowConflictError("invalid export path")
        return final_path, partial_path

    def cleanup_paths(self, job_id: str) -> None:
        final_path, partial_path = self.safe_paths(job_id)
        for path in (partial_path, final_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


class MonitoringService:
    def __init__(
        self,
        store: MonitoringExportStore,
        export_service: ExportService,
        query_repository: Any,
        *,
        clock: Clock = utc_now,
        max_duration_seconds: int = DEFAULT_RAW_RETENTION_SECONDS,
        capability_resolver: CapabilityResolver | None = None,
    ) -> None:
        self.store = store
        self.export_service = export_service
        self.query_repository = query_repository
        self.clock = clock
        self.max_duration_seconds = max_duration_seconds
        self.capability_resolver = capability_resolver

    def server_time(self) -> str:
        return iso_utc(self.clock())

    def create(self, payload: Any) -> dict[str, Any]:
        request = validate_monitoring_request(
            payload, max_duration_seconds=self.max_duration_seconds
        )
        if self.capability_resolver is not None:
            validate_source_capabilities(
                request.sources,
                request.fields,
                self.capability_resolver(),
            )
        now = self.clock()
        session = self.store.create_session(
            request,
            start_time=now,
            scheduled_end_time=now + timedelta(seconds=request.duration_seconds),
        )
        return self.serialize(session, now=now)

    def list(self) -> list[dict[str, Any]]:
        now = self.clock()
        self.store.reconcile_due_sessions(now)
        return [
            self.serialize(session, now=now) for session in self.store.list_sessions()
        ]

    def get(self, session_id: str) -> dict[str, Any]:
        now = self.clock()
        self.store.reconcile_due_sessions(now)
        return self.serialize(self.store.get_session(session_id), now=now)

    def stop(self, session_id: str) -> dict[str, Any]:
        now = self.clock()
        session = self.store.stop_session(session_id, now)
        return self.serialize(session, now=now)

    def delete(self, session_id: str) -> None:
        now = self.clock()
        self.store.reconcile_due_sessions(now)
        job_id = self.store.delete_session(session_id)
        if job_id:
            self.export_service.cleanup_paths(job_id)

    def preview(self, session_id: str) -> dict[str, Any]:
        now = self.clock()
        self.store.reconcile_due_sessions(now)
        session = self.store.get_session(session_id)
        start = parse_stored_time(session["start_time_utc"])
        effective_end = _session_effective_end(session, now)
        preview_method = getattr(self.query_repository, "monitoring_preview", None)
        if not callable(preview_method) or start is None:
            preview = _empty_preview("preview is unavailable for this query repository")
        elif effective_end <= start:
            preview = _empty_preview()
        else:
            preview = preview_method(
                start=start,
                stop=effective_end,
                sources=sources_from_json(session["selected_sources"]),
                fields=tuple(session["selected_fields"]),
                limit=20,
            )
        return self.serialize(session, now=now, preview=preview)

    def serialize(
        self,
        session: Mapping[str, Any],
        *,
        now: datetime | None = None,
        preview: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = now or self.clock()
        start = parse_stored_time(session["start_time_utc"])
        scheduled = parse_stored_time(session["scheduled_end_time_utc"])
        if start is None or scheduled is None:
            raise RuntimeError("monitoring session contains invalid timestamps")
        effective = _session_effective_end(session, now)
        elapsed = max(0, int((effective - start).total_seconds()))
        duration = max(0, int((scheduled - start).total_seconds()))
        remaining = (
            max(0, int((scheduled - now).total_seconds()))
            if session["status"] == "running"
            else 0
        )
        export = self.store.get_export_for_session(str(session["id"]))
        serialized_export = (
            self.export_service.serialize(export, now=now)
            if export is not None
            else None
        )
        return {
            "id": session["id"],
            "name": session["name"],
            "notes": session["notes"],
            "status": session["status"],
            "start_time_utc": session["start_time_utc"],
            "scheduled_end_time_utc": session["scheduled_end_time_utc"],
            "actual_end_time_utc": session.get("actual_end_time_utc"),
            "effective_end_time_utc": iso_utc(effective),
            "duration_seconds": duration,
            "elapsed_seconds": min(elapsed, duration),
            "remaining_seconds": remaining,
            "selected_sources": list(session["selected_sources"]),
            "selected_fields": list(session["selected_fields"]),
            "source_count": len(session["selected_sources"]),
            "field_count": len(session["selected_fields"]),
            "resolution": session["resolution"],
            "csv_format": session["csv_format"],
            "created_at_utc": session["created_at_utc"],
            "updated_at_utc": session["updated_at_utc"],
            "trigger_source": session.get("trigger_source", "manual"),
            "printer_session_id": session.get("printer_session_id"),
            "printer_ended_at_utc": session.get("printer_ended_at_utc"),
            "recovery_end_time_utc": session.get("recovery_end_time_utc"),
            "shares_existing_sensor_storage": session.get("trigger_source")
            == "printer",
            "server_time_utc": iso_utc(now),
            "preview": dict(preview) if preview is not None else None,
            "export": serialized_export,
            "is_download_ready": bool(
                serialized_export and serialized_export["is_download_ready"]
            ),
        }


def download_filename(job: Mapping[str, Any]) -> str:
    normalized = unicodedata.normalize("NFKD", str(job["name"]))
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:64] or "sensor-export"
    start = parse_stored_time(job["start_time_utc"])
    stamp = (start or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H%M%SZ")
    short_id = str(job["id"]).replace("-", "")[:8]
    return f"{slug}_{stamp}_{short_id}.csv"


def csv_safe_text(value: Any) -> str:
    """Protect user-controlled spreadsheet text from formula interpretation."""

    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _session_effective_end(session: Mapping[str, Any], now: datetime) -> datetime:
    scheduled = parse_stored_time(session["scheduled_end_time_utc"])
    actual = parse_stored_time(session.get("actual_end_time_utc"))
    if scheduled is None:
        raise RuntimeError("monitoring session contains invalid scheduled end")
    if session["status"] == "running":
        return min(now.astimezone(timezone.utc), scheduled)
    if session["status"] == "stopped" and actual is not None:
        return min(actual, scheduled)
    return scheduled


def _seconds_between(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds()))


def _empty_preview(warning: str | None = None) -> dict[str, Any]:
    return {
        "row_count": 0,
        "row_count_is_approximate": True,
        "row_count_kind": "selected measurement values",
        "first_sample_timestamp": None,
        "latest_sample_timestamp": None,
        "source_presence": [],
        "recent_samples": [],
        "recent_sample_limit": 20,
        "warnings": [warning] if warning else [],
    }
