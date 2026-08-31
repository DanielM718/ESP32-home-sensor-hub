"""Read-only printer API and SEN66 correlation queries."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from app.config import InfluxSettings
from app.printer_intelligence import PrinterIntelligenceStore
from app.printer_model import PrintSession, ValueProvenance
from app.printer_persistence import PrinterStore
from app.queries import QueryValidationError
from app.workflows import AMS_FIELDS, BOOLEAN_FIELDS, NUMERIC_FIELDS, PRINTER_FIELDS

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
PRINTER_TELEMETRY_MEASUREMENT = "printer_telemetry"
PRINTER_TELEMETRY_RANGES = {
    "1h": ("-1h", "1m", "live_raw_downsampled_1m"),
    "6h": ("-6h", "5m", "live_raw_downsampled_5m"),
    "24h": ("-24h", "15m", "live_raw_downsampled_15m"),
    "7d": ("-7d", "30m", "durable_5m_downsampled_30m"),
    # The shared Monitoring graph also offers 30d. Like 7d it reads the durable
    # bucket, so the tier stays honestly labelled as five-minute permanent data.
    "30d": ("-30d", "3h", "durable_5m_downsampled_3h"),
}
STABLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class PrinterTelemetryQuery:
    range_key: str
    flux_start: str
    window_every: str
    data_tier: str
    printer_id: str | None = None
    ams_id: str | None = None
    sensor_type: str = "all"
    fields: tuple[str, ...] = PRINTER_FIELDS + AMS_FIELDS


def printer_telemetry_query_from_params(
    params: Mapping[str, str | None],
) -> PrinterTelemetryQuery:
    range_key = str(params.get("range") or "24h").strip()
    if range_key not in PRINTER_TELEMETRY_RANGES:
        raise QueryValidationError(
            "range must be one of: " + ", ".join(PRINTER_TELEMETRY_RANGES)
        )
    sensor_type = str(params.get("sensor_type") or "all").strip()
    if sensor_type not in {"all", "printer", "ams"}:
        raise QueryValidationError("sensor_type must be one of: all, printer, ams")
    printer_id = _stable_query_id(params.get("printer_id"), "printer_id")
    ams_id = _stable_query_id(params.get("ams_id"), "ams_id")
    if ams_id is not None and printer_id is None:
        raise QueryValidationError("ams_id requires printer_id")
    if ams_id is not None and sensor_type == "printer":
        raise QueryValidationError("ams_id cannot be used with sensor_type=printer")
    raw_fields = str(params.get("fields") or "").strip()
    fields = (
        tuple(
            dict.fromkeys(
                item.strip() for item in raw_fields.split(",") if item.strip()
            )
        )
        if raw_fields
        else PRINTER_FIELDS + AMS_FIELDS
    )
    if not fields:
        raise QueryValidationError("fields must include at least one telemetry field")
    invalid = [field for field in fields if field not in PRINTER_FIELDS + AMS_FIELDS]
    if invalid:
        raise QueryValidationError(
            "unsupported printer telemetry field: " + ", ".join(invalid)
        )
    supported_for_type = (
        PRINTER_FIELDS
        if sensor_type == "printer"
        else AMS_FIELDS
        if sensor_type == "ams"
        else PRINTER_FIELDS + AMS_FIELDS
    )
    incompatible = [field for field in fields if field not in supported_for_type]
    if incompatible:
        raise QueryValidationError(
            f"field is not supported by sensor_type={sensor_type}: "
            + ", ".join(incompatible)
        )
    flux_start, window_every, data_tier = PRINTER_TELEMETRY_RANGES[range_key]
    return PrinterTelemetryQuery(
        range_key=range_key,
        flux_start=flux_start,
        window_every=window_every,
        data_tier=data_tier,
        printer_id=printer_id,
        ams_id=ams_id,
        sensor_type=sensor_type,
        fields=fields,
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
        raw_retention_seconds: int = 72 * 60 * 60,
        query_api: Any | None = None,
        rolling_window_days: int = 30,
        minimum_mode_history_days: int = 7,
    ) -> None:
        self.influx = influx
        self.database_path = Path(database_path)
        self.environment_location = environment_location
        self.baseline_minutes = baseline_minutes
        self.recovery_minutes = recovery_minutes
        self.raw_retention_seconds = raw_retention_seconds
        self._client = None
        self._query_api = query_api
        self.intelligence = PrinterIntelligenceStore(
            self.database_path,
            rolling_window_days=rolling_window_days,
            minimum_mode_history_days=minimum_mode_history_days,
        )

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
        result["usage"] = self.intelligence.usage_summary(state.printer_id)
        result["history_import"] = self.intelligence.history_status(state.printer_id)
        return result

    def sessions(self, *, limit: int = 20) -> dict[str, Any]:
        if not self.database_path.exists():
            return {"sessions": [], "available": False}
        sessions = PrinterStore(self.database_path).list_sessions(limit=limit)
        return {"sessions": [item.to_dict() for item in sessions], "available": True}

    def history(self, *, limit: int = 100) -> dict[str, Any]:
        if not self.database_path.exists():
            return {"available": False, "history": [], "import": {}}
        return {
            "available": self.database_path.exists(),
            "history": self.intelligence.history(limit=limit),
            "import": self.intelligence.history_status(),
        }

    def history_item(self, history_id: str) -> dict[str, Any] | None:
        if not self.database_path.exists():
            return None
        return self.intelligence.history_item(history_id)

    def telemetry(self, query: PrinterTelemetryQuery) -> dict[str, Any]:
        # Drive the bucket from the tier the response will advertise, so a new
        # range can never read live data while labelling itself durable.
        bucket = (
            self.influx.bucket
            if query.data_tier.startswith("durable_")
            else self.influx.live_bucket
        )
        records = self._query(printer_telemetry_flux(bucket, query))
        return printer_telemetry_response(records, query)

    def usage(self) -> dict[str, Any]:
        """Canonical tracked print time and usage provenance."""

        if not self.database_path.exists():
            return {
                "available": False,
                "status": "not_configured",
                "usage": {},
            }
        return {
            "available": True,
            "status": "ok",
            "usage": self.intelligence.usage_summary(),
            "history_import": self.intelligence.history_status(),
        }

    def maintenance(self) -> dict[str, Any]:
        if not self.database_path.exists():
            return {
                "available": False,
                "usage": {},
                "summary": {},
                "tasks": [],
                "completion_history": [],
                "recent_notifications": [],
                "local_record_only": True,
                "printer_control": False,
            }
        return self.intelligence.maintenance()

    def maintenance_events(
        self, *, limit: int = 100, pending_only: bool = False
    ) -> dict[str, Any]:
        if not self.database_path.exists():
            return {"available": False, "events": []}
        return {
            "available": True,
            "events": self.intelligence.notification_events(
                limit=limit, pending_only=pending_only
            ),
            "local_record_only": True,
            "printer_control": False,
        }

    def complete_maintenance(
        self, task_id: str, *, notes: str, completed_at: datetime
    ) -> dict[str, Any]:
        if not self.database_path.exists():
            raise RuntimeError("printer observer is not configured")
        return self.intelligence.complete_maintenance(
            task_id, notes=notes, completed_at=completed_at
        )

    def complete_all_maintenance(
        self, *, notes: str, completed_at: datetime
    ) -> dict[str, Any]:
        if not self.database_path.exists():
            raise RuntimeError("printer observer is not configured")
        return self.intelligence.complete_all_maintenance(
            notes=notes, completed_at=completed_at
        )

    def environment_summary(self, history_id: str | None = None) -> dict[str, Any]:
        if not self.database_path.exists():
            return _unavailable_summary("no_print_session")
        store = PrinterStore(self.database_path)
        # "Last print" prefers a finished session while an active print is in
        # progress. On a new installation with no history, the active session
        # is still useful as a partial observational window.
        session = None
        if history_id:
            history = self.intelligence.history_item(history_id)
            session = _history_session(history) if history is not None else None
            if session is None:
                return _unavailable_summary("print_interval_unknown")
        else:
            session = store.latest_finished_session() or store.latest_session()
        if session is None:
            return _unavailable_summary("no_print_session")
        end = session.ended_at or datetime.now(timezone.utc)
        query_start = session.started_at - timedelta(minutes=self.baseline_minutes)
        query_stop = end + timedelta(minutes=self.recovery_minutes)
        if datetime.now(timezone.utc) - query_start > timedelta(
            seconds=self.raw_retention_seconds
        ):
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


def printer_telemetry_flux(bucket: str, query: PrinterTelemetryQuery) -> str:
    selected_numeric = tuple(field for field in query.fields if field in NUMERIC_FIELDS)
    selected_boolean = tuple(field for field in query.fields if field in BOOLEAN_FIELDS)
    filters = [
        f"  |> filter(fn: (r) => r._measurement == {json.dumps(PRINTER_TELEMETRY_MEASUREMENT)})"
    ]
    if query.sensor_type != "all":
        filters.append(
            f"  |> filter(fn: (r) => r.component_type == {json.dumps(query.sensor_type)})"
        )
    if query.printer_id is not None:
        filters.append(
            f"  |> filter(fn: (r) => r.printer_id == {json.dumps(query.printer_id)})"
        )
    if query.ams_id is not None:
        filters.append(
            f"  |> filter(fn: (r) => r.component_id == {json.dumps(query.ams_id)})"
        )
    base = "\n".join(
        [
            f"data = from(bucket: {json.dumps(bucket)})",
            f"  |> range(start: {query.flux_start})",
            *filters,
        ]
    )
    streams: list[str] = []
    sections = [base]
    if selected_numeric:
        sections.extend(
            [
                "",
                "numeric = data",
                f"  |> filter(fn: (r) => contains(value: r._field, set: {json.dumps(list(selected_numeric))}))",
                '  |> group(columns: ["printer_id", "component_type", "component_id", "source", "_field"])',
                f"  |> aggregateWindow(every: {query.window_every}, fn: mean, createEmpty: false)",
            ]
        )
        streams.append("numeric")
    if selected_boolean:
        sections.extend(
            [
                "",
                "status = data",
                f"  |> filter(fn: (r) => contains(value: r._field, set: {json.dumps(list(selected_boolean))}))",
                '  |> group(columns: ["printer_id", "component_type", "component_id", "source", "_field"])',
                f"  |> aggregateWindow(every: {query.window_every}, fn: last, createEmpty: false)",
            ]
        )
        streams.append("status")
    sections.extend(
        [
            "",
            streams[0]
            if len(streams) == 1
            else f"union(tables: [{', '.join(streams)}])",
            '  |> sort(columns: ["_time"])',
            "",
        ]
    )
    return "\n".join(sections)


def printer_telemetry_response(
    tables: Iterable[Any], query: PrinterTelemetryQuery
) -> dict[str, Any]:
    series: dict[tuple[str, str], dict[str, Any]] = {}
    points: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for table in tables:
        records = getattr(table, "records", None)
        rows = records if records is not None else (table,)
        for record in rows:
            values = getattr(record, "values", {})
            component_type = str(values.get("component_type") or "")
            printer_id = str(values.get("printer_id") or "")
            component_id = str(values.get("component_id") or "")
            if (
                component_type not in {"printer", "ams"}
                or not printer_id
                or (component_type == "ams" and not component_id)
            ):
                continue
            source_id = (
                printer_id
                if component_type == "printer"
                else f"{printer_id}/{component_id}"
            )
            key = (component_type, source_id)
            item = series.setdefault(
                key,
                {
                    "id": source_id,
                    "source_id": source_id,
                    "sensor_type": component_type,
                    "printer_id": printer_id,
                    "component_id": component_id,
                    "ams_id": component_id if component_type == "ams" else None,
                    "label": (
                        f"{printer_id} · {component_id}"
                        if component_type == "ams"
                        else f"Printer · {printer_id}"
                    ),
                    "available_fields": [],
                },
            )
            field = str(
                values.get("_field")
                or (record.get_field() if hasattr(record, "get_field") else "")
            )
            if field not in query.fields:
                continue
            timestamp = values.get("_time")
            if timestamp is None and hasattr(record, "get_time"):
                timestamp = record.get_time()
            time_key = (
                _iso(timestamp) if isinstance(timestamp, datetime) else str(timestamp)
            )
            value = values.get("_value")
            if value is None and hasattr(record, "get_value"):
                value = record.get_value()
            if field in BOOLEAN_FIELDS:
                if not isinstance(value, bool):
                    continue
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                continue
            if not time_key or time_key == "None":
                continue
            point = points.setdefault(key, {}).setdefault(time_key, {"time": time_key})
            point[field] = value
            if field not in item["available_fields"]:
                item["available_fields"].append(field)
    response_series = []
    for key, item in series.items():
        item["available_fields"].sort()
        item["points"] = sorted(
            points.get(key, {}).values(), key=lambda row: row["time"]
        )
        response_series.append(item)
    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "range": query.range_key,
        "window": query.window_every,
        "data_tier": query.data_tier,
        "series": sorted(
            response_series,
            key=lambda item: (str(item["sensor_type"]), str(item["source_id"])),
        ),
    }


def _stable_query_id(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not STABLE_ID_RE.fullmatch(text):
        raise QueryValidationError(f"{name} must be a stable 1-64 character slug")
    return text


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


def _history_session(history: dict[str, Any]) -> PrintSession | None:
    started = _parse_time(history.get("started_at"))
    ended = _parse_time(history.get("ended_at"))
    if started is None:
        return None
    if history.get("source") == "bambu_cloud_history" and ended is None:
        return None
    return PrintSession(
        session_id=str(history.get("history_id") or "history"),
        printer_id=str(history.get("printer_id") or "x2d"),
        job_id=str(history["job_id"]) if history.get("job_id") is not None else None,
        job_name=str(history["job_name"]) if history.get("job_name") else None,
        started_at=started,
        start_provenance=ValueProvenance.OBSERVED,
        ended_at=ended,
        end_provenance=(
            ValueProvenance.OBSERVED if ended is not None else ValueProvenance.UNKNOWN
        ),
        result=str(history["result"]) if history.get("result") else None,
        material=str(history["material"]) if history.get("material") else None,
        material_provenance=ValueProvenance.OBSERVED,
        active_tool=(
            str(history["active_tool"]) if history.get("active_tool") else None
        ),
        ams_slot=str(history["ams_slot"]) if history.get("ams_slot") else None,
        source=str(history.get("source") or "unknown"),
        updated_at=ended or started,
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
