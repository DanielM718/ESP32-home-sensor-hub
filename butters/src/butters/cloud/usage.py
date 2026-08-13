"""Persistent non-secret provider usage, cost estimation, and hard budgets."""

from __future__ import annotations

import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from butters.assistant_config import CloudSettings
from butters.cloud.model import CloudTokenUsage, ReasoningConfiguration


@dataclass(frozen=True, slots=True)
class CloudUsageRecord:
    timestamp: str
    request_category: str
    model: str
    reasoning_effort: str
    escalation_level: int
    input_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tool_rounds: int
    wall_seconds: float
    estimated_cost_usd: float
    success: bool
    escalation_occurred: bool
    error_code: str | None = None
    provider: str = "openai"
    operation_category: str = "text_reasoning"
    tool_calls: int = 0
    route_category: str = "cloud"
    request_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class RequestUsageSummary:
    timestamp: str
    request_id: str
    session_id: str
    source: str
    route_category: str
    model: str | None
    provider: str | None
    model_avoided: bool
    wall_seconds: float
    success: bool
    error_code: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    provider TEXT NOT NULL,
    operation_category TEXT NOT NULL,
    request_category TEXT NOT NULL,
    route_category TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    escalation_level INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    tool_rounds INTEGER NOT NULL,
    tool_calls INTEGER NOT NULL,
    wall_seconds REAL NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    success INTEGER NOT NULL,
    escalation_occurred INTEGER NOT NULL,
    error_code TEXT,
    request_id TEXT,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_usage_timestamp
    ON provider_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_provider_usage_session
    ON provider_usage(session_id, timestamp);
CREATE TABLE IF NOT EXISTS request_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    request_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    route_category TEXT NOT NULL,
    model TEXT,
    provider TEXT,
    model_avoided INTEGER NOT NULL,
    wall_seconds REAL NOT NULL,
    success INTEGER NOT NULL,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_request_usage_timestamp
    ON request_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_request_usage_session
    ON request_usage(session_id, timestamp);
CREATE TABLE IF NOT EXISTS spend_totals (
    day TEXT PRIMARY KEY,
    estimated_cost_usd REAL NOT NULL
);
"""


class UsageLedger:
    """Thread-safe ledger that never stores prompts, transcripts, audio, or output."""

    def __init__(
        self,
        settings: CloudSettings,
        database_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.database_path = Path(database_path) if database_path is not None else None
        self._memory_records: list[CloudUsageRecord] = []
        self._memory_requests: list[RequestUsageSummary] = []
        self._memory_spend: dict[str, float] = {}
        self._lock = threading.RLock()
        self._context = threading.local()
        if self.database_path is not None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                count = int(connection.execute("SELECT COUNT(*) FROM spend_totals").fetchone()[0])
                if count == 0:
                    connection.execute(
                        "INSERT INTO spend_totals(day, estimated_cost_usd) "
                        "SELECT substr(timestamp,1,10), SUM(estimated_cost_usd) "
                        "FROM provider_usage GROUP BY substr(timestamp,1,10)"
                    )
            try:
                os.chmod(self.database_path, 0o600)
            except OSError:
                pass

    _PROVIDER_COLUMNS = (
        "timestamp, request_category, model, reasoning_effort, "
        "escalation_level, input_tokens, cached_tokens, cache_write_tokens, "
        "output_tokens, reasoning_tokens, tool_rounds, wall_seconds, "
        "estimated_cost_usd, success, escalation_occurred, error_code, "
        "provider, operation_category, tool_calls, route_category, "
        "request_id, session_id"
    )
    _REQUEST_COLUMNS = (
        "timestamp, request_id, session_id, source, route_category, "
        "model, provider, model_avoided, wall_seconds, success, error_code"
    )

    @property
    def records(self) -> list[CloudUsageRecord]:
        """Every retained provider row.

        Diagnostic/test accessor only. Reporting paths use bounded queries: at
        the configured retention cap this materializes tens of thousands of rows.
        """

        if self.database_path is None:
            return list(self._memory_records)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._PROVIDER_COLUMNS} FROM provider_usage ORDER BY id"
            ).fetchall()
        return [_provider_row(row) for row in rows]

    def estimated_cost(self, model: str, usage: CloudTokenUsage) -> float:
        price = self.settings.pricing.get(model)
        if price is None:
            return float("inf")
        uncached = max(0, usage.input_tokens - usage.cached_tokens - usage.cache_write_tokens)
        return (
            uncached * price.input_per_million_usd
            + usage.cached_tokens * price.cached_input_per_million_usd
            + usage.cache_write_tokens * price.input_per_million_usd * 1.25
            + usage.output_tokens * price.output_per_million_usd
        ) / 1_000_000

    def conservative_request_estimate(
        self, model: str, evidence_bytes: int, max_output_tokens: int
    ) -> float:
        # UTF-8 bytes are a safe upper bound for token count across ordinary
        # and adversarial short inputs; dividing by an average chars/token
        # ratio would under-reserve pathological input.
        approximate_input = max(512, evidence_bytes + 1500)
        return self.estimated_cost(
            model,
            CloudTokenUsage(
                input_tokens=approximate_input,
                output_tokens=max_output_tokens,
            ),
        )

    def permits(self, estimate: float) -> bool:
        if not math.isfinite(estimate) or estimate < 0:
            return False
        if estimate > self.settings.max_estimated_cost_per_request_usd:
            return False
        now = datetime.now(timezone.utc)
        day_prefix = now.date().isoformat()
        month_prefix = day_prefix[:7]
        daily, monthly = self._cost_totals(day_prefix, month_prefix)
        return (
            daily + estimate <= self.settings.daily_budget_usd
            and monthly + estimate <= self.settings.monthly_budget_usd
        )

    def record(
        self,
        category: str,
        configuration: ReasoningConfiguration,
        usage: CloudTokenUsage,
        *,
        tool_rounds: int,
        wall_seconds: float,
        success: bool,
        escalation_occurred: bool,
        error_code: str | None = None,
        provider: str = "openai",
        operation_category: str = "text_reasoning",
        tool_calls: int = 0,
        route_category: str = "cloud",
        request_id: str | None = None,
        session_id: str | None = None,
        estimated_cost_override: float | None = None,
    ) -> CloudUsageRecord:
        context = getattr(self._context, "value", {})
        request_id = request_id or context.get("request_id")
        session_id = session_id or context.get("session_id")
        if route_category == "cloud" and context.get("route_category"):
            route_category = str(context["route_category"])
        cost = self.estimated_cost(configuration.model, usage)
        if estimated_cost_override is not None:
            if not math.isfinite(estimated_cost_override) or estimated_cost_override < 0:
                raise ValueError("pricing_unknown")
            cost = max(cost, estimated_cost_override)
        if not math.isfinite(cost):
            raise ValueError("pricing_unknown")
        record = CloudUsageRecord(
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            category[:64],
            configuration.model[:128],
            configuration.effort[:32],
            int(configuration.level),
            max(0, usage.input_tokens),
            max(0, usage.cached_tokens),
            max(0, usage.cache_write_tokens),
            max(0, usage.output_tokens),
            max(0, usage.reasoning_tokens),
            max(0, tool_rounds),
            max(0.0, wall_seconds),
            cost,
            bool(success),
            bool(escalation_occurred),
            error_code[:64] if error_code else None,
            provider[:64],
            operation_category[:64],
            max(0, tool_calls),
            route_category[:64],
            request_id[:128] if request_id else None,
            session_id[:128] if session_id else None,
        )
        self._append_provider_record(record)
        return record

    @contextmanager
    def request_context(
        self,
        *,
        request_id: str,
        session_id: str,
        route_category: str,
    ):
        """Associate nested provider records with one worker-thread request."""
        previous = getattr(self._context, "value", None)
        self._context.value = {
            "request_id": request_id,
            "session_id": session_id,
            "route_category": route_category,
        }
        try:
            yield
        finally:
            if previous is None:
                try:
                    del self._context.value
                except AttributeError:
                    pass
            else:
                self._context.value = previous

    def record_external(
        self,
        *,
        provider: str,
        operation_category: str,
        model: str,
        estimated_cost_usd: float,
        wall_seconds: float,
        success: bool,
        request_id: str | None = None,
        session_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_code: str | None = None,
    ) -> CloudUsageRecord:
        """Record a pre-priced STT/TTS operation without storing its content."""
        if not math.isfinite(estimated_cost_usd) or estimated_cost_usd < 0:
            raise ValueError("pricing_unknown")
        record = CloudUsageRecord(
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            operation_category[:64],
            model[:128],
            "not_applicable",
            0,
            max(0, input_tokens),
            0,
            0,
            max(0, output_tokens),
            0,
            0,
            max(0.0, wall_seconds),
            estimated_cost_usd,
            bool(success),
            False,
            error_code[:64] if error_code else None,
            provider[:64],
            operation_category[:64],
            0,
            operation_category[:64],
            request_id[:128] if request_id else None,
            session_id[:128] if session_id else None,
        )
        self._append_provider_record(record)
        return record

    def record_request(
        self,
        *,
        request_id: str,
        session_id: str,
        source: str,
        route_category: str,
        model: str | None,
        provider: str | None,
        model_avoided: bool,
        wall_seconds: float,
        success: bool,
        error_code: str | None = None,
    ) -> RequestUsageSummary:
        record = RequestUsageSummary(
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            request_id[:128],
            session_id[:128],
            source[:32],
            route_category[:64],
            model[:128] if model else None,
            provider[:64] if provider else None,
            bool(model_avoided),
            max(0.0, wall_seconds),
            bool(success),
            error_code[:64] if error_code else None,
        )
        with self._lock:
            if self.database_path is None:
                self._memory_requests.append(record)
                if len(self._memory_requests) > self.settings.max_usage_records:
                    del self._memory_requests[: len(self._memory_requests) - self.settings.max_usage_records]
            else:
                with self._connect() as connection:
                    connection.execute(
                        """INSERT INTO request_usage (
                            timestamp, request_id, session_id, source, route_category,
                            model, provider, model_avoided, wall_seconds, success, error_code
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            record.timestamp,
                            record.request_id,
                            record.session_id,
                            record.source,
                            record.route_category,
                            record.model,
                            record.provider,
                            int(record.model_avoided),
                            record.wall_seconds,
                            int(record.success),
                            record.error_code,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM request_usage WHERE id IN "
                        "(SELECT id FROM request_usage ORDER BY id DESC LIMIT -1 OFFSET ?)",
                        (self.settings.max_usage_records,),
                    )
        return record

    def request_records(self) -> list[RequestUsageSummary]:
        """Every retained request row; see `records` for the bounding caveat."""

        if self.database_path is None:
            return list(self._memory_requests)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._REQUEST_COLUMNS} FROM request_usage ORDER BY id"
            ).fetchall()
        return [_request_row(row) for row in rows]

    def _append_provider_record(self, record: CloudUsageRecord) -> None:
        with self._lock:
            if self.database_path is None:
                self._memory_records.append(record)
                day = record.timestamp[:10]
                self._memory_spend[day] = self._memory_spend.get(day, 0.0) + record.estimated_cost_usd
                for old_day in sorted(self._memory_spend)[:-400]:
                    self._memory_spend.pop(old_day, None)
                if len(self._memory_records) > self.settings.max_usage_records:
                    del self._memory_records[: len(self._memory_records) - self.settings.max_usage_records]
            else:
                with self._connect() as connection:
                    connection.execute(
                        """INSERT INTO provider_usage (
                            timestamp, provider, operation_category, request_category,
                            route_category, model, reasoning_effort, escalation_level,
                            input_tokens, cached_tokens, cache_write_tokens, output_tokens,
                            reasoning_tokens, tool_rounds, tool_calls, wall_seconds,
                            estimated_cost_usd, success, escalation_occurred, error_code,
                            request_id, session_id
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            record.timestamp,
                            record.provider,
                            record.operation_category,
                            record.request_category,
                            record.route_category,
                            record.model,
                            record.reasoning_effort,
                            record.escalation_level,
                            record.input_tokens,
                            record.cached_tokens,
                            record.cache_write_tokens,
                            record.output_tokens,
                            record.reasoning_tokens,
                            record.tool_rounds,
                            record.tool_calls,
                            record.wall_seconds,
                            record.estimated_cost_usd,
                            int(record.success),
                            int(record.escalation_occurred),
                            record.error_code,
                            record.request_id,
                            record.session_id,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM provider_usage WHERE id IN "
                        "(SELECT id FROM provider_usage ORDER BY id DESC LIMIT -1 OFFSET ?)",
                        (self.settings.max_usage_records,),
                    )
                    connection.execute(
                        "INSERT INTO spend_totals(day, estimated_cost_usd) VALUES (?,?) "
                        "ON CONFLICT(day) DO UPDATE SET estimated_cost_usd="
                        "spend_totals.estimated_cost_usd + excluded.estimated_cost_usd",
                        (record.timestamp[:10], record.estimated_cost_usd),
                    )
                    connection.execute(
                        "DELETE FROM spend_totals WHERE day < date('now', '-400 days')"
                    )
    def summary(self, *, session_id: str | None = None) -> dict[str, object]:
        """Aggregate retained usage without materializing the whole ledger.

        Every figure is computed by a bounded SQL aggregate or a small
        `GROUP BY` over low-cardinality category columns, so the cost of this
        report does not grow with the retention cap.
        """

        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        week_start = (now - timedelta(days=6)).date().isoformat()
        month = today[:7]
        month_start = month + "-01"

        if self.database_path is None:
            return self._memory_summary(session_id, today, tomorrow, week_start, month)

        with self._lock, self._connect() as connection:
            has_requests = bool(
                connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM request_usage"
                    + (" WHERE session_id=?" if session_id is not None else "")
                    + ")",
                    (session_id,) if session_id is not None else (),
                ).fetchone()[0]
            )

            def window(table: str, low: str, high: str | None) -> tuple[str, tuple[object, ...]]:
                clauses = ["timestamp >= ?"]
                values: list[object] = [low]
                if high is not None:
                    clauses.append("timestamp < ?")
                    values.append(high)
                if session_id is not None:
                    clauses.append("session_id = ?")
                    values.append(session_id)
                return f"FROM {table} WHERE " + " AND ".join(clauses), tuple(values)

            def provider_totals(low: str, high: str | None) -> dict[str, object]:
                clause, values = window("provider_usage", low, high)
                row = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(estimated_cost_usd),0), "
                    "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                    "COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),0) " + clause,
                    values,
                ).fetchone()
                return {
                    "requests": int(row[0]),
                    "cost_usd": round(float(row[1]), 6),
                    "input_tokens": int(row[2]),
                    "output_tokens": int(row[3]),
                    "errors": int(row[4]),
                }

            def request_totals(low: str, high: str | None) -> tuple[int, int, int]:
                clause, values = window("request_usage", low, high)
                row = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),0), "
                    "COALESCE(SUM(model_avoided),0) " + clause,
                    values,
                ).fetchone()
                return int(row[0]), int(row[1]), int(row[2])

            def combined(low: str, high: str | None) -> dict[str, object]:
                values = provider_totals(low, high)
                count, errors, avoided = request_totals(low, high)
                if has_requests:
                    values["requests"] = count
                    values["errors"] = errors
                values["model_avoided"] = avoided
                return values

            def distribution(table: str, column: str) -> dict[str, int]:
                clause = f" WHERE session_id=?" if session_id is not None else ""
                rows = connection.execute(
                    f"SELECT {column}, COUNT(*) FROM {table}{clause} "
                    f"GROUP BY {column} ORDER BY {column}",
                    (session_id,) if session_id is not None else (),
                ).fetchall()
                return {str(row[0]): int(row[1]) for row in rows}

            def latency(table: str, column: str) -> dict[str, dict[str, float | int]]:
                clause = " WHERE session_id=?" if session_id is not None else ""
                rows = connection.execute(
                    f"SELECT {column}, COUNT(*), AVG(wall_seconds), MAX(wall_seconds) "
                    f"FROM {table}{clause} GROUP BY {column} ORDER BY {column}",
                    (session_id,) if session_id is not None else (),
                ).fetchall()
                return {
                    str(row[0]): {
                        "count": int(row[1]),
                        "average_ms": round(float(row[2] or 0.0) * 1000, 3),
                        "max_ms": round(float(row[3] or 0.0) * 1000, 3),
                    }
                    for row in rows
                }

            routes = distribution("request_usage", "route_category") if has_requests else {}
            if not routes:
                routes = distribution("provider_usage", "route_category")
            route_latency = latency("request_usage", "route_category") if has_requests else {}
            if not route_latency:
                route_latency = latency("provider_usage", "route_category")
            error_clause = " AND session_id=?" if session_id is not None else ""
            error_rows = connection.execute(
                "SELECT timestamp, provider, model, error_code, route_category "
                f"FROM provider_usage WHERE success=0{error_clause} ORDER BY id DESC LIMIT 20",
                (session_id,) if session_id is not None else (),
            ).fetchall()
            avoided_total = connection.execute(
                "SELECT COALESCE(SUM(model_avoided),0) FROM request_usage"
                + (" WHERE session_id=?" if session_id is not None else ""),
                (session_id,) if session_id is not None else (),
            ).fetchone()[0]

            return {
                "today": combined(today, tomorrow),
                "last_7_days": combined(week_start, tomorrow),
                "current_month": combined(month_start, tomorrow),
                "route_distribution": routes,
                "deterministic_or_model_avoided": int(avoided_total),
                "model_distribution": distribution("provider_usage", "model"),
                "provider_distribution": distribution("provider_usage", "provider"),
                "latency_by_route": route_latency,
                "latency_by_operation": latency("provider_usage", "operation_category"),
                "recent_errors": [
                    {
                        "timestamp": row[0],
                        "provider": row[1],
                        "model": row[2],
                        "error_code": row[3],
                        "route": row[4],
                    }
                    for row in reversed(error_rows)
                ],
                "pricing": {
                    "source": self.settings.pricing_source,
                    "date": self.settings.pricing_date,
                    "unknown_models_fail_closed": True,
                },
            }

    def _memory_summary(
        self,
        session_id: str | None,
        today: str,
        tomorrow: str,
        week_start: str,
        month: str,
    ) -> dict[str, object]:
        records = self._memory_records
        requests = self._memory_requests
        if session_id is not None:
            records = [item for item in records if item.session_id == session_id]
            requests = [item for item in requests if item.session_id == session_id]

        def aggregate(selected: list[CloudUsageRecord]) -> dict[str, object]:
            return {
                "requests": len(selected),
                "cost_usd": round(sum(item.estimated_cost_usd for item in selected), 6),
                "input_tokens": sum(item.input_tokens for item in selected),
                "output_tokens": sum(item.output_tokens for item in selected),
                "errors": sum(not item.success for item in selected),
            }

        routes: dict[str, int] = {}
        for item in requests:
            routes[item.route_category] = routes.get(item.route_category, 0) + 1
        if not requests:
            for item in records:
                routes[item.route_category] = routes.get(item.route_category, 0) + 1
        models: dict[str, int] = {}
        providers: dict[str, int] = {}
        for item in records:
            models[item.model] = models.get(item.model, 0) + 1
            providers[item.provider] = providers.get(item.provider, 0) + 1

        def combined(low: str, high: str) -> dict[str, object]:
            provider_items = [item for item in records if low <= item.timestamp < high]
            request_items = [item for item in requests if low <= item.timestamp < high]
            values = aggregate(provider_items)
            if requests:
                values["requests"] = len(request_items)
                values["errors"] = sum(not item.success for item in request_items)
            values["model_avoided"] = sum(item.model_avoided for item in request_items)
            return values

        latency: dict[str, dict[str, float | int]] = {}
        for route in sorted(routes):
            values = [item.wall_seconds for item in requests if item.route_category == route]
            if not values:
                values = [item.wall_seconds for item in records if item.route_category == route]
            if not values:
                continue
            latency[route] = {
                "count": len(values),
                "average_ms": round(sum(values) * 1000 / len(values), 3),
                "max_ms": round(max(values) * 1000, 3),
            }
        provider_latency: dict[str, dict[str, float | int]] = {}
        for operation in sorted({item.operation_category for item in records}):
            values = [item.wall_seconds for item in records if item.operation_category == operation]
            provider_latency[operation] = {
                "count": len(values),
                "average_ms": round(sum(values) * 1000 / len(values), 3),
                "max_ms": round(max(values) * 1000, 3),
            }
        return {
            "today": combined(today, tomorrow),
            "last_7_days": combined(week_start, tomorrow),
            "current_month": combined(month + "-01", tomorrow),
            "route_distribution": routes,
            "deterministic_or_model_avoided": sum(item.model_avoided for item in requests),
            "model_distribution": models,
            "provider_distribution": providers,
            "latency_by_route": latency,
            "latency_by_operation": provider_latency,
            "recent_errors": [
                {
                    "timestamp": item.timestamp,
                    "provider": item.provider,
                    "model": item.model,
                    "error_code": item.error_code,
                    "route": item.route_category,
                }
                for item in records
                if not item.success
            ][-20:],
            "pricing": {
                "source": self.settings.pricing_source,
                "date": self.settings.pricing_date,
                "unknown_models_fail_closed": True,
            },
        }

    def recent(self, limit: int = 100) -> list[dict[str, object]]:
        bounded = max(1, min(limit, 500))
        if self.database_path is None:
            return [asdict(item) for item in self._memory_records[-bounded:]]
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._PROVIDER_COLUMNS} FROM provider_usage ORDER BY id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [asdict(_provider_row(row)) for row in reversed(rows)]

    def recent_requests(self, limit: int = 100) -> list[dict[str, object]]:
        bounded = max(1, min(limit, 500))
        if self.database_path is None:
            return [asdict(item) for item in self._memory_requests[-bounded:]]
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT {self._REQUEST_COLUMNS} FROM request_usage ORDER BY id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [asdict(_request_row(row)) for row in reversed(rows)]

    def _cost_totals(self, day_prefix: str, month_prefix: str) -> tuple[float, float]:
        if self.database_path is None:
            daily = self._memory_spend.get(day_prefix, 0.0)
            monthly = sum(
                value for day, value in self._memory_spend.items()
                if day.startswith(month_prefix)
            )
            return daily, monthly
        with self._lock, self._connect() as connection:
            daily = connection.execute(
                "SELECT COALESCE((SELECT estimated_cost_usd FROM spend_totals WHERE day=?), 0)",
                (day_prefix,),
            ).fetchone()[0]
            monthly = connection.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM spend_totals WHERE day LIKE ?",
                (month_prefix + "%",),
            ).fetchone()[0]
        return float(daily), float(monthly)

    def _connect(self) -> sqlite3.Connection:
        assert self.database_path is not None
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


def _provider_row(row: tuple[object, ...]) -> CloudUsageRecord:
    return CloudUsageRecord(*row[:13], bool(row[13]), bool(row[14]), *row[15:])


def _request_row(row: tuple[object, ...]) -> RequestUsageSummary:
    return RequestUsageSummary(
        *row[:7], bool(row[7]), float(row[8]), bool(row[9]), row[10]
    )
