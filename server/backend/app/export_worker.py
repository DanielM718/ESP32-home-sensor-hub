"""Single-heavy-job Raspberry Pi worker for every persistent CSV export."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from itertools import groupby
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
from threading import Event, Thread
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from app.config import AppSettings, configure_logging, load_settings
from app.export_queries import ExportPoint, InfluxExportQueryRepository
from app.persistence import MonitoringExportStore
from app.workflow_services import ExportService, csv_safe_text, utc_now
from app.workflows import (
    SENSOR_TYPE_AIR_QUALITY,
    SENSOR_TYPE_ENVIRONMENT,
    Source,
    aggregate_field,
    fields_for_source,
    parse_stored_time,
    sources_from_json,
)


LOGGER = logging.getLogger("home_sensor.export_worker")

LONG_HEADER = (
    "timestamp_utc",
    "sensor_type",
    "source_id",
    "node_id",
    "location",
    "field",
    "value",
    "unit",
    "data_tier",
)
WIDE_IDENTITY_HEADER = (
    "timestamp_utc",
    "sensor_type",
    "source_id",
    "node_id",
    "location",
)


class ExportCancelled(RuntimeError):
    pass


class WorkerStopping(RuntimeError):
    pass


class ExportWorker:
    def __init__(
        self,
        store: MonitoringExportStore,
        query_repository: Any,
        *,
        raw_chunk_seconds: int = 3600,
        aggregate_chunk_seconds: int = 86400,
        lease_seconds: int = 300,
        heartbeat_seconds: int = 30,
        poll_seconds: int = 2,
        clock: Callable[[], datetime] = utc_now,
        worker_id: str | None = None,
        start_heartbeat_thread: bool = True,
    ) -> None:
        self.store = store
        self.query_repository = query_repository
        self.raw_chunk_seconds = raw_chunk_seconds
        self.aggregate_chunk_seconds = aggregate_chunk_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = min(heartbeat_seconds, max(5, lease_seconds // 3))
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.worker_id = worker_id or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        )
        self.start_heartbeat_thread = start_heartbeat_thread
        self.stop_event = Event()
        self.export_service = ExportService(store, clock=clock)

    def recover(self) -> list[dict[str, str]]:
        now = self.clock()
        recovered = self.store.recover_stale_jobs(
            cutoff=now - timedelta(seconds=self.lease_seconds), now=now
        )
        for item in recovered:
            self.export_service.cleanup_paths(item["id"])
            LOGGER.warning(
                "Recovered stale export job %s into %s",
                item["id"],
                item["status"],
            )
        return recovered

    def run_once(self) -> bool:
        # Recovery is intentionally periodic, not startup-only: a process can
        # restart before the previous lease becomes stale after a sudden crash.
        self.recover()
        self.store.reconcile_due_sessions(self.clock())
        job = self.store.claim_oldest_export(worker_id=self.worker_id, now=self.clock())
        if job is None:
            return False
        self.process_job(job)
        return True

    def run_forever(self) -> None:
        LOGGER.info("Export worker %s started", self.worker_id)
        while not self.stop_event.is_set():
            worked = self.run_once()
            if not worked:
                self.stop_event.wait(self.poll_seconds)
        LOGGER.info("Export worker %s stopped", self.worker_id)

    def stop(self) -> None:
        self.stop_event.set()

    def process_job(self, job: Mapping[str, Any]) -> None:
        job_id = str(job["id"])
        final_path, partial_path = self.export_service.safe_paths(job_id)
        self.export_service.cleanup_paths(job_id)
        sources = sources_from_json(job["selected_sources"])
        fields = tuple(str(field) for field in job["selected_fields"])
        resolution = str(job["resolution"])
        csv_format = str(job["csv_format"])
        start = parse_stored_time(job["start_time_utc"])
        end = parse_stored_time(job["end_time_utc"])
        if start is None or end is None or end < start:
            self.store.mark_job_failed(
                job_id,
                worker_id=self.worker_id,
                now=self.clock(),
                error_message="persistent export interval is invalid",
            )
            return

        warnings = list(job.get("warnings") or [])
        source_state = _initial_source_state(sources, fields, resolution)
        configured_chunk_seconds = (
            self.raw_chunk_seconds
            if resolution == "raw"
            else self.aggregate_chunk_seconds
        )
        chunk_seconds = _bounded_chunk_seconds(
            resolution,
            configured_chunk_seconds,
            sources,
            fields,
        )
        chunk_count = (
            int(math.ceil((end - start).total_seconds() / chunk_seconds))
            if end > start
            else 0
        )
        sensor_types = [
            sensor_type
            for sensor_type in (SENSOR_TYPE_ENVIRONMENT, SENSOR_TYPE_AIR_QUALITY)
            if any(source.sensor_type == sensor_type for source in sources)
        ]
        work_total = chunk_count * len(sensor_types)
        work_completed = 0
        rows_written = 0

        heartbeat = _LeaseHeartbeat(
            self,
            job_id,
            enabled=self.start_heartbeat_thread,
        )
        LOGGER.info(
            "Starting export job %s (%s, %s, %d chunks)",
            job_id,
            resolution,
            csv_format,
            chunk_count,
        )
        try:
            partial_path.parent.mkdir(parents=True, exist_ok=True)
            with (
                heartbeat,
                partial_path.open("w", encoding="utf-8", newline="") as handle,
            ):
                writer = csv.writer(handle, lineterminator="\n")
                wide_fields = _wide_fields(fields, resolution)
                if csv_format == "long":
                    writer.writerow(LONG_HEADER)
                else:
                    writer.writerow(WIDE_IDENTITY_HEADER + wide_fields + ("data_tier",))

                for chunk_index, (chunk_start, chunk_stop) in enumerate(
                    _time_chunks(start, end, chunk_seconds), start=1
                ):
                    self._check_interruption(job_id)
                    points: list[ExportPoint] = []
                    for sensor_type in sensor_types:
                        phase = (
                            "querying_aggregate"
                            if resolution == "15m"
                            else (
                                "querying_environment"
                                if sensor_type == SENSOR_TYPE_ENVIRONMENT
                                else "querying_air_quality"
                            )
                        )
                        self.store.update_job_progress(
                            job_id,
                            worker_id=self.worker_id,
                            now=self.clock(),
                            current_phase=phase,
                            rows_written=rows_written,
                            work_units_total=work_total,
                            work_units_completed=work_completed,
                            warnings=warnings,
                            source_results=_source_results(source_state),
                        )
                        stream = self.query_repository.query_source_type(
                            sensor_type=sensor_type,
                            start=chunk_start,
                            stop=chunk_stop,
                            sources=sources,
                            fields=fields,
                            resolution=resolution,
                        )
                        for point_index, point in enumerate(stream, start=1):
                            if point_index % 500 == 0:
                                self._check_interruption(job_id)
                            points.append(point)
                            source_state[point.source_key]["data_points"] += 1
                        work_completed += 1
                        self._check_interruption(job_id)

                    points.sort(key=lambda point: point.sort_key)
                    self.store.update_job_progress(
                        job_id,
                        worker_id=self.worker_id,
                        now=self.clock(),
                        current_phase="writing_csv",
                        work_units_total=work_total,
                        work_units_completed=work_completed,
                    )
                    if csv_format == "long":
                        written = _write_long(writer, points, source_state)
                    else:
                        written = _write_wide(writer, points, wide_fields, source_state)
                    rows_written += written
                    self._check_interruption(job_id)
                    handle.flush()
                    file_size = partial_path.stat().st_size
                    self.store.update_job_progress(
                        job_id,
                        worker_id=self.worker_id,
                        now=self.clock(),
                        current_phase="writing_csv",
                        rows_written=rows_written,
                        output_size_bytes=file_size,
                        work_units_total=work_total,
                        work_units_completed=work_completed,
                        warnings=warnings,
                        source_results=_source_results(source_state),
                    )
                    LOGGER.debug(
                        "Export %s finished chunk %d/%d with %d rows",
                        job_id,
                        chunk_index,
                        chunk_count,
                        rows_written,
                    )

                self._check_interruption(job_id)
                self.store.update_job_progress(
                    job_id,
                    worker_id=self.worker_id,
                    now=self.clock(),
                    current_phase="finalizing",
                    rows_written=rows_written,
                    work_units_total=work_total,
                    work_units_completed=work_completed,
                )
                handle.flush()
                os.fsync(handle.fileno())

            self._check_interruption(job_id)
            os.replace(partial_path, final_path)
            _fsync_directory(final_path.parent)
            final_size = final_path.stat().st_size
            zero_sources = [
                item["source_id"]
                for item in source_state.values()
                if item["data_points"] == 0
            ]
            if zero_sources:
                warnings.append(
                    f"{len(zero_sources)} selected source(s) returned zero data; see source_results"
                )
            source_results = _source_results(source_state, final=True)
            completed = self.store.finish_job(
                job_id,
                worker_id=self.worker_id,
                now=self.clock(),
                rows_written=rows_written,
                output_size_bytes=final_size,
                work_units_total=work_total,
                work_units_completed=work_completed,
                warnings=warnings,
                source_results=source_results,
            )
            if not completed:
                final_path.unlink(missing_ok=True)
                if self.store.cancellation_requested(job_id, worker_id=self.worker_id):
                    self.store.mark_job_cancelled(
                        job_id,
                        worker_id=self.worker_id,
                        now=self.clock(),
                        warnings=warnings,
                        source_results=source_results,
                    )
                    return
                raise RuntimeError("export lease was lost before completion")
            LOGGER.info(
                "Completed export job %s with %d rows and %d bytes",
                job_id,
                rows_written,
                final_size,
            )
        except ExportCancelled:
            self.export_service.cleanup_paths(job_id)
            self.store.mark_job_cancelled(
                job_id,
                worker_id=self.worker_id,
                now=self.clock(),
                warnings=warnings,
                source_results=_source_results(source_state, final=True),
            )
            LOGGER.info("Cancelled export job %s", job_id)
        except WorkerStopping:
            self.export_service.cleanup_paths(job_id)
            status = self.store.release_owned_job(
                job_id, worker_id=self.worker_id, now=self.clock()
            )
            LOGGER.info(
                "Released export job %s as %s during worker shutdown", job_id, status
            )
        except Exception as exc:
            self.export_service.cleanup_paths(job_id)
            message = _safe_error(exc)
            self.store.mark_job_failed(
                job_id,
                worker_id=self.worker_id,
                now=self.clock(),
                error_message=message,
            )
            LOGGER.exception("Export job %s failed: %s", job_id, message)

    def _check_interruption(self, job_id: str) -> None:
        if self.stop_event.is_set():
            raise WorkerStopping()
        if self.store.cancellation_requested(job_id, worker_id=self.worker_id):
            raise ExportCancelled()


class _LeaseHeartbeat:
    def __init__(self, worker: ExportWorker, job_id: str, *, enabled: bool) -> None:
        self.worker = worker
        self.job_id = job_id
        self.enabled = enabled
        self.event = Event()
        self.thread: Thread | None = None

    def __enter__(self) -> "_LeaseHeartbeat":
        if self.enabled:
            self.thread = Thread(
                target=self._run,
                name=f"export-heartbeat-{self.job_id[:8]}",
                daemon=True,
            )
            self.thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self.event.wait(self.worker.heartbeat_seconds):
            try:
                owned = self.worker.store.heartbeat(
                    self.job_id,
                    worker_id=self.worker.worker_id,
                    now=self.worker.clock(),
                )
                if not owned:
                    return
            except Exception:
                LOGGER.exception("Failed to heartbeat export job %s", self.job_id)


def _time_chunks(
    start: datetime, end: datetime, chunk_seconds: int
) -> Iterable[tuple[datetime, datetime]]:
    current = start
    delta = timedelta(seconds=chunk_seconds)
    while current < end:
        stop = min(end, current + delta)
        yield current, stop
        current = stop


def _bounded_chunk_seconds(
    resolution: str,
    configured_seconds: int,
    sources: Sequence[Source],
    fields: Sequence[str],
    *,
    target_points: int = 50_000,
) -> int:
    """Shrink chunks as source cardinality grows so one sort remains Pi-sized."""

    if resolution == "raw":
        sample_seconds = 5
        relevant = [
            source
            for source in sources
            if source.sensor_type == SENSOR_TYPE_AIR_QUALITY
        ]
        minimum = 300
    else:
        sample_seconds = 15 * 60
        relevant = [
            source
            for source in sources
            if source.sensor_type == SENSOR_TYPE_AIR_QUALITY
        ]
        minimum = 3600
    if not relevant:
        # Environment nodes are sparse (normally 15 minutes), so the configured
        # raw chunk is already bounded even at the source limit.
        return configured_seconds
    selected_cells = sum(
        len(fields_for_source(source, fields, resolution)) for source in relevant
    )
    if selected_cells <= 0:
        return configured_seconds
    estimated = int(target_points * sample_seconds / selected_cells)
    return max(minimum, min(configured_seconds, estimated))


def _initial_source_state(
    sources: Sequence[Source], fields: Sequence[str], resolution: str
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sources:
        supported = fields_for_source(source, fields, resolution)
        result[source.key] = {
            **source.as_dict(),
            "source_id": source.source_id,
            "data_points": 0,
            "rows_written": 0,
            "supported_fields": list(supported),
            "unsupported_fields": [field for field in fields if field not in supported],
            "status": "pending",
        }
    return result


def _source_results(
    state: Mapping[tuple[str, str], Mapping[str, Any]], *, final: bool = False
) -> list[dict[str, Any]]:
    results = []
    for key in sorted(state):
        item = dict(state[key])
        if final:
            item["status"] = "data" if item["data_points"] else "zero_data"
        results.append(item)
    return results


def _write_long(
    writer: Any,
    points: Sequence[ExportPoint],
    source_state: Mapping[tuple[str, str], dict[str, Any]],
) -> int:
    for point in points:
        writer.writerow(
            (
                point.timestamp_utc,
                point.sensor_type,
                csv_safe_text(point.source_id),
                point.node_id if point.node_id is not None else "",
                csv_safe_text(point.location),
                point.field,
                point.value,
                point.unit,
                point.data_tier,
            )
        )
        source_state[point.source_key]["rows_written"] += 1
    return len(points)


def _write_wide(
    writer: Any,
    points: Sequence[ExportPoint],
    wide_fields: Sequence[str],
    source_state: Mapping[tuple[str, str], dict[str, Any]],
) -> int:
    rows = 0
    for _key, grouped in groupby(
        points,
        key=lambda point: (
            point.timestamp_utc,
            point.sensor_type,
            point.source_id,
            point.node_id,
            point.location,
            point.data_tier,
        ),
    ):
        group = list(grouped)
        first = group[0]
        values = {point.field: point.value for point in group}
        writer.writerow(
            (
                first.timestamp_utc,
                first.sensor_type,
                csv_safe_text(first.source_id),
                first.node_id if first.node_id is not None else "",
                csv_safe_text(first.location),
                *(values.get(field, "") for field in wide_fields),
                first.data_tier,
            )
        )
        source_state[first.source_key]["rows_written"] += 1
        rows += 1
    return rows


def _wide_fields(fields: Sequence[str], resolution: str) -> tuple[str, ...]:
    if resolution == "raw":
        return tuple(fields)
    return tuple(aggregate_field(field) for field in fields if field != "battery_mv")


def _safe_error(exc: Exception) -> str:
    text = re.sub(r"(?i)(token|authorization)=?[^\s,;]+", r"\1=[redacted]", str(exc))
    text = " ".join(text.split())[:600]
    return f"{type(exc).__name__}: {text or 'export failed'}"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_worker(settings: AppSettings) -> ExportWorker:
    store = MonitoringExportStore(
        settings.monitoring_exports.database_path,
        settings.monitoring_exports.output_dir,
    )
    store.initialize()
    query_repository = InfluxExportQueryRepository(settings.influx)
    return ExportWorker(
        store,
        query_repository,
        raw_chunk_seconds=settings.monitoring_exports.raw_chunk_seconds,
        aggregate_chunk_seconds=settings.monitoring_exports.aggregate_chunk_seconds,
        lease_seconds=settings.monitoring_exports.lease_seconds,
        heartbeat_seconds=settings.monitoring_exports.heartbeat_seconds,
        poll_seconds=settings.monitoring_exports.worker_poll_seconds,
    )


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    worker = build_worker(settings)

    def request_stop(_signum: int, _frame: Any) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        worker.run_forever()
    finally:
        close = getattr(worker.query_repository, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
