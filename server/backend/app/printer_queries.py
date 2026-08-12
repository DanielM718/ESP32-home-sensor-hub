"""Read-only printer API and SEN66 correlation queries."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from app.config import InfluxSettings
from app.printer_model import PrintSession
from app.printer_persistence import PrinterStore

ENVIRONMENT_METRICS = (
    "co2",
    "pm1",
    "pm25",
    "pm4",
    "pm10",
    "voc_index",
    "nox_index",
    "temperature_c",
    "humidity",
)


class PrinterReadRepository:
    def __init__(
        self,
        influx: InfluxSettings,
        *,
        database_path: Path = Path("/var/lib/home-sensor/printer.sqlite3"),
        environment_location: str = "printer_room",
        baseline_minutes: int = 30,
        recovery_minutes: int = 120,
        query_api: Any | None = None,
    ) -> None:
        self.influx = influx
        self.database_path = Path(database_path)
        self.environment_location = environment_location
        self.baseline_minutes = baseline_minutes
        self.recovery_minutes = recovery_minutes
        self._client = None
        self._query_api = query_api

    def _query(self, flux: str) -> Any:
        if self._query_api is None:
            from influxdb_client import InfluxDBClient

            self._client = InfluxDBClient(
                url=self.influx.url,
                token=self.influx.read_token,
                org=self.influx.org,
            )
            self._query_api = self._client.query_api()
        return self._query_api.query(query=flux, org=self.influx.org)

    def current(self) -> dict[str, Any]:
        if not self.database_path.exists():
            return {
                "available": False,
                "status": "not_configured",
                "reason": "printer observer has not produced state",
            }
        state = PrinterStore(self.database_path).current_state()
        if state is None:
            return {
                "available": False,
                "status": "unavailable",
                "reason": "printer state is unavailable",
            }
        result = state.to_dict()
        result["available"] = state.online
        result["status"] = state.normalized_state.value
        return result

    def sessions(self, *, limit: int = 20) -> dict[str, Any]:
        if not self.database_path.exists():
            return {"sessions": [], "available": False}
        sessions = PrinterStore(self.database_path).list_sessions(limit=limit)
        return {"sessions": [item.to_dict() for item in sessions], "available": True}

    def environment_summary(self) -> dict[str, Any]:
        if not self.database_path.exists():
            return _unavailable_summary("no_print_session")
        store = PrinterStore(self.database_path)
        # "Last print" prefers a finished session while an active print is in
        # progress. On a new installation with no history, the active session
        # is still useful as a partial observational window.
        session = store.latest_finished_session() or store.latest_session()
        if session is None:
            return _unavailable_summary("no_print_session")
        end = session.ended_at or datetime.now(timezone.utc)
        query_start = session.started_at - timedelta(minutes=self.baseline_minutes)
        query_stop = end + timedelta(minutes=self.recovery_minutes)
        if datetime.now(timezone.utc) - query_start > timedelta(hours=72):
            return {
                **_unavailable_summary("raw_samples_expired"),
                "session": session.to_dict(),
                "windows": self._windows(session),
            }
        records = self._query(
            printer_environment_flux(
                self.influx.live_bucket,
                self.environment_location,
                query_start,
                query_stop,
            )
        )
        points = _environment_points(records)
        if not points:
            return {
                **_unavailable_summary("no_environment_samples"),
                "session": session.to_dict(),
                "windows": self._windows(session),
            }
        return environment_summary_response(
            session,
            points,
            baseline_minutes=self.baseline_minutes,
            recovery_minutes=self.recovery_minutes,
            location=self.environment_location,
        )

    def _windows(self, session: PrintSession) -> dict[str, str | None]:
        end = session.ended_at
        return {
            "baseline_start": _iso(
                session.started_at - timedelta(minutes=self.baseline_minutes)
            ),
            "print_start": _iso(session.started_at),
            "print_end": _iso(end),
            "recovery_end": _iso(
                None if end is None else end + timedelta(minutes=self.recovery_minutes)
            ),
        }

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def printer_environment_flux(
    bucket: str, location: str, start: datetime, stop: datetime
) -> str:
    fields = json.dumps(list(ENVIRONMENT_METRICS))
    return f"""from(bucket: {json.dumps(bucket)})
  |> range(start: time(v: {json.dumps(_iso(start))}), stop: time(v: {json.dumps(_iso(stop))}))
  |> filter(fn: (r) => r._measurement == "air_quality_reading")
  |> filter(fn: (r) => r.location == {json.dumps(location)})
  |> filter(fn: (r) => contains(value: r._field, set: {fields}))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""


def environment_summary_response(
    session: PrintSession,
    points: list[dict[str, Any]],
    *,
    baseline_minutes: int,
    recovery_minutes: int,
    location: str,
) -> dict[str, Any]:
    end = session.ended_at or max(point["time"] for point in points)
    baseline_start = session.started_at - timedelta(minutes=baseline_minutes)
    recovery_end = end + timedelta(minutes=recovery_minutes)
    baseline = [
        point
        for point in points
        if baseline_start <= point["time"] < session.started_at
    ]
    during = [point for point in points if session.started_at <= point["time"] <= end]
    post = [point for point in points if end < point["time"] <= recovery_end]

    metrics: dict[str, dict[str, float | int | None]] = {}
    for metric in ENVIRONMENT_METRICS:
        baseline_values = _values(baseline, metric)
        during_values = _values(during, metric)
        post_values = _values(post, metric)
        baseline_mean = fmean(baseline_values) if baseline_values else None
        during_mean = fmean(during_values) if during_values else None
        metrics[metric] = {
            "baseline_mean": _rounded(baseline_mean),
            "print_mean": _rounded(during_mean),
            "print_peak": _rounded(max(during_values)) if during_values else None,
            "post_mean": _rounded(fmean(post_values)) if post_values else None,
            "change_from_baseline": _rounded(
                None
                if baseline_mean is None or during_mean is None
                else during_mean - baseline_mean
            ),
        }

    return {
        "available": bool(during),
        "observational": True,
        "location": location,
        "session": session.to_dict(),
        "windows": {
            "baseline_start": _iso(baseline_start),
            "print_start": _iso(session.started_at),
            "print_end": _iso(session.ended_at),
            "recovery_end": _iso(recovery_end),
            "baseline_minutes": baseline_minutes,
            "recovery_minutes": recovery_minutes,
        },
        "sample_counts": {
            "baseline": len(baseline),
            "print": len(during),
            "post": len(post),
        },
        "metrics": metrics,
        "voc_recovery_seconds": _recovery_seconds(
            post,
            end=end,
            metric="voc_index",
            baseline=metrics["voc_index"]["baseline_mean"],
        ),
        "limitations": [
            "Associations are observational and do not establish that the printer caused a change.",
            "Recovery is the first of three consecutive samples at or below baseline plus tolerance.",
        ],
    }


def _environment_points(records: Iterable[Any]) -> list[dict[str, Any]]:
    points = []
    for table in records:
        for record in getattr(table, "records", []):
            values = getattr(record, "values", {})
            timestamp = values.get("_time")
            if not isinstance(timestamp, datetime):
                continue
            point: dict[str, Any] = {"time": timestamp.astimezone(timezone.utc)}
            for metric in ENVIRONMENT_METRICS:
                value = values.get(metric)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    point[metric] = float(value)
            points.append(point)
    return sorted(points, key=lambda item: item["time"])


def _values(points: list[dict[str, Any]], metric: str) -> list[float]:
    return [float(point[metric]) for point in points if metric in point]


def _recovery_seconds(
    points: list[dict[str, Any]],
    *,
    end: datetime,
    metric: str,
    baseline: float | None,
) -> int | None:
    if baseline is None:
        return None
    tolerance = max(5.0, abs(float(baseline)) * 0.1)
    consecutive = 0
    first: datetime | None = None
    for point in points:
        value = point.get(metric)
        if (
            isinstance(value, (int, float))
            and float(value) <= float(baseline) + tolerance
        ):
            consecutive += 1
            first = point["time"] if consecutive == 1 else first
            if consecutive >= 3 and first is not None:
                return max(0, round((first - end).total_seconds()))
        else:
            consecutive = 0
            first = None
    return None


def _unavailable_summary(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "observational": True}


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
