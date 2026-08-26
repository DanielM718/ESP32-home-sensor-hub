"""Skill Framework v2 semantic capabilities and local analytical reduction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import cast

from butters.integrations.desktop import DesktopWorkflow
from butters.integrations.history import DashboardHistoryAdapter, HistorySeries
from butters.integrations.model import (
    IntegrationError,
    PrinterSession,
    PrinterSnapshotProvider,
)
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.skills.analytics import (
    compare_summaries,
    correlate,
    detect_spike,
    summarize_points,
)
from butters.skills.model import (
    ActionClass,
    AuthenticationLevel,
    CompareWindowsArgs,
    CorrelationArgs,
    DesktopArgs,
    PrintDetailsArgs,
    PrintEnvironmentAnalysisArgs,
    RecentPrintsArgs,
    SensorHistoryArgs,
    SensorWindowArgs,
    SkillArguments,
    SkillError,
    SkillResult,
    SpikeArgs,
    StructuredSkillResult,
)
from butters.skills.registry import (
    SkillRegistry,
    SkillSpec,
    current_cancel_event,
    optional_string,
    required_string,
    required_string_tuple,
    strict_arguments,
)

HISTORY_LOOKBACKS = ("1h", "24h", "7d", "30d")
HISTORY_BUCKETS = ("auto", "1m", "15m", "1h", "6h")
PRINT_METRICS = (
    "temperature",
    "humidity",
    "co2",
    "pm1",
    "pm25",
    "pm4",
    "pm10",
    "voc_index",
    "nox_index",
)


class V2SkillImplementations:
    def __init__(
        self,
        entities: EntityRegistry,
        metrics: MetricRegistry,
        history: DashboardHistoryAdapter,
        printer: PrinterSnapshotProvider,
        desktop: DesktopWorkflow,
    ) -> None:
        self.entities = entities
        self.metrics = metrics
        self.history = history
        self.printer = printer
        self.desktop = desktop

    def authorize_desktop(self, arguments: SkillArguments) -> None:
        if cast(DesktopArgs, arguments).machine != "desktop":
            raise SkillError("policy_denied", "machine is not allow-listed")

    def authorize_history(self, arguments: SkillArguments) -> None:
        args = cast(SensorHistoryArgs | SensorWindowArgs, arguments)
        entity = self.entities.get(args.entity)
        if entity is None or entity.sensor_type == "printer":
            raise SkillError("policy_denied", "entity is not allow-listed for history")
        for metric_id in args.metrics:
            metric = self.metrics.get(metric_id)
            if metric is None or entity.sensor_type not in metric.sensor_types:
                raise SkillError(
                    "policy_denied", "metric is not allow-listed for entity"
                )

    def authorize_compare(self, arguments: SkillArguments) -> None:
        self.authorize_history(cast(CompareWindowsArgs, arguments))

    def authorize_spike(self, arguments: SkillArguments) -> None:
        args = cast(SpikeArgs, arguments)
        self.authorize_history(
            SensorWindowArgs(
                args.entity, (args.metric,), args.start, args.end, args.lookback
            )
        )

    def authorize_correlation(self, arguments: SkillArguments) -> None:
        args = cast(CorrelationArgs, arguments)
        self.authorize_history(
            SensorWindowArgs(
                args.entity,
                (args.metric_x, args.metric_y),
                args.start,
                args.end,
                args.lookback,
            )
        )

    def authorize_printer(self, arguments: SkillArguments) -> None:
        entity_id = (
            cast(RecentPrintsArgs, arguments).entity
            if isinstance(arguments, RecentPrintsArgs)
            else cast(PrintDetailsArgs, arguments).entity
        )
        entity = self.entities.get(entity_id)
        if entity is None or entity.sensor_type != "printer":
            raise SkillError("policy_denied", "printer is not allow-listed")

    def authorize_print_analysis(self, arguments: SkillArguments) -> None:
        args = cast(PrintEnvironmentAnalysisArgs, arguments)
        printer = self.entities.get(args.printer)
        environment = self.entities.get(args.environment)
        if printer is None or printer.sensor_type != "printer":
            raise SkillError("policy_denied", "printer is not allow-listed")
        if environment is None or environment.sensor_type != "air_quality":
            raise SkillError("policy_denied", "environment station is not allow-listed")
        for metric in args.metrics:
            if metric not in PRINT_METRICS:
                raise SkillError("policy_denied", "print metric is not allow-listed")

    def get_desktop_status(self, arguments: SkillArguments) -> SkillResult:
        state = self.desktop.status(cast(DesktopArgs, arguments).machine)
        return StructuredSkillResult(
            "desktop_status",
            state.safe_dict(),
            {"observed": ["network_reachable", "ssh_ready", "parsec_ready"]},
        )

    def start_remote_desktop_session(self, arguments: SkillArguments) -> SkillResult:
        data = self.desktop.start_remote_session(
            cast(DesktopArgs, arguments).machine,
            cancel_event=current_cancel_event(),
        )
        return StructuredSkillResult(
            "desktop_remote_session",
            data,
            {
                "observed": ["network_reachable", "ssh_ready", "parsec_ready"],
                "calculated": ["elapsed_ms"],
                "unknown": ["parsec_ready"] if data.get("parsec_ready") is None else [],
            },
        )

    def get_sensor_history(self, arguments: SkillArguments) -> SkillResult:
        args = cast(SensorHistoryArgs, arguments)
        series = self._history(args)
        return StructuredSkillResult(
            "sensor_history",
            asdict(series),
            {
                "observed": ["points"],
                "unknown": self._missing_metrics(series),
            },
        )

    def summarize_sensor_window(self, arguments: SkillArguments) -> SkillResult:
        args = cast(SensorWindowArgs, arguments)
        series = self._window(
            args.entity, args.metrics, args.start, args.end, args.lookback
        )
        summaries = summarize_points(series.points, args.metrics)
        return StructuredSkillResult(
            "sensor_window_summary",
            {
                "entity": args.entity,
                "window": {
                    "start": series.start,
                    "end": series.end,
                    "bucket": series.bucket,
                },
                "sample_count": len(series.points),
                "metrics": summaries,
            },
            {
                "observed": ["window", "sample_count"],
                "calculated": ["metrics"],
                "unknown": [
                    key
                    for key, value in summaries.items()
                    if not value.get("available")
                ],
            },
        )

    def compare_sensor_windows(self, arguments: SkillArguments) -> SkillResult:
        args = cast(CompareWindowsArgs, arguments)
        first = self._window(
            args.entity, args.metrics, args.first_start, args.first_end, None
        )
        second = self._window(
            args.entity, args.metrics, args.second_start, args.second_end, None
        )
        first_summary = summarize_points(first.points, args.metrics)
        second_summary = summarize_points(second.points, args.metrics)
        return StructuredSkillResult(
            "sensor_window_comparison",
            {
                "entity": args.entity,
                "first_window": {
                    "start": first.start,
                    "end": first.end,
                    "sample_count": len(first.points),
                },
                "second_window": {
                    "start": second.start,
                    "end": second.end,
                    "sample_count": len(second.points),
                },
                "metrics": compare_summaries(
                    first_summary, second_summary, args.metrics
                ),
            },
            {
                "observed": ["first_window", "second_window"],
                "calculated": ["metrics"],
                "inferred": [],
            },
        )

    def detect_metric_spikes(self, arguments: SkillArguments) -> SkillResult:
        args = cast(SpikeArgs, arguments)
        series = self._window(
            args.entity, (args.metric,), args.start, args.end, args.lookback
        )
        return StructuredSkillResult(
            "metric_spikes",
            {
                "entity": args.entity,
                "window": {"start": series.start, "end": series.end},
                **detect_spike(series.points, args.metric),
            },
            {
                "observed": ["window", "sample_count"],
                "calculated": ["spike_magnitude", "spike_time"],
            },
        )

    def correlate_metrics(self, arguments: SkillArguments) -> SkillResult:
        args = cast(CorrelationArgs, arguments)
        series = self._window(
            args.entity,
            (args.metric_x, args.metric_y),
            args.start,
            args.end,
            args.lookback,
        )
        return StructuredSkillResult(
            "metric_correlation",
            {
                "entity": args.entity,
                "window": {"start": series.start, "end": series.end},
                **correlate(series.points, args.metric_x, args.metric_y),
            },
            {
                "observed": ["window", "sample_count"],
                "calculated": ["pearson_r"],
                "inferred": [],
                "unknown": [],
            },
        )

    def get_recent_prints(self, arguments: SkillArguments) -> SkillResult:
        args = cast(RecentPrintsArgs, arguments)
        sessions = self.printer.recent_sessions(args.limit)
        return StructuredSkillResult(
            "recent_prints",
            {
                "printer": args.entity,
                "count": len(sessions),
                "prints": [asdict(item) for item in sessions],
            },
            {"observed": ["prints"]},
        )

    def get_print_details(self, arguments: SkillArguments) -> SkillResult:
        args = cast(PrintDetailsArgs, arguments)
        session = self.printer.session(args.print_id)
        return StructuredSkillResult(
            "print_details",
            {
                "printer": args.entity,
                "available": session is not None,
                "print": asdict(session) if session else None,
            },
            {
                "observed": ["print"] if session else [],
                "unknown": [] if session else ["print"],
            },
        )

    def analyze_print_environment(self, arguments: SkillArguments) -> SkillResult:
        args = cast(PrintEnvironmentAnalysisArgs, arguments)
        if args.print_selector == "current_vs_previous":
            current = self.printer.current_session()
            recent = self.printer.recent_sessions(10)
            previous = next(
                (
                    item
                    for item in recent
                    if item.ended_at is not None
                    and (
                        current is None
                        or (
                            item.print_id != current.print_id
                            and item.started_at != current.started_at
                        )
                    )
                ),
                None,
            )
            analyses = [
                self._analyze_session(args, label, session)
                for label, session in (("current", current), ("previous", previous))
            ]
            return StructuredSkillResult(
                "print_environment_comparison",
                {
                    "printer": args.printer,
                    "environment": args.environment,
                    "available": all(bool(item.get("available")) for item in analyses),
                    "prints": analyses,
                    "unknown": [
                        str(item["selector"])
                        for item in analyses
                        if not item.get("available")
                    ],
                },
                _evidence_labels(),
            )
        session = self._selected_session(args.print_selector)
        return StructuredSkillResult(
            "print_environment_analysis",
            self._analyze_session(args, args.print_selector, session),
            _evidence_labels(),
        )

    def _selected_session(self, selector: str) -> PrinterSession | None:
        if selector == "current":
            return self.printer.current_session()
        recent = self.printer.recent_sessions(10)
        if selector == "previous":
            return next((item for item in recent if item.ended_at is not None), None)
        return None

    def _analyze_session(
        self,
        args: PrintEnvironmentAnalysisArgs,
        label: str,
        session: PrinterSession | None,
    ) -> dict[str, object]:
        if session is None:
            return {"selector": label, "available": False, "unknown": ["print_session"]}
        if session.started_at is None:
            return {
                "selector": label,
                "available": False,
                "print": asdict(session),
                "unknown": ["print_start_time"],
            }
        start = _parse_time(session.started_at)
        end = (
            _parse_time(session.ended_at)
            if session.ended_at
            else datetime.now(timezone.utc)
        )
        if end <= start:
            return {
                "selector": label,
                "available": False,
                "print": asdict(session),
                "unknown": ["valid_print_window"],
            }
        duration = end - start
        baseline_duration = (
            timedelta(minutes=args.baseline_minutes)
            if args.baseline_minutes is not None
            else duration
        )
        baseline_start = start - min(baseline_duration, timedelta(days=7))
        if end - baseline_start > timedelta(days=30):
            return {
                "selector": label,
                "available": False,
                "print": asdict(session),
                "unknown": ["sensor_history_outside_maximum_lookback"],
            }
        try:
            series = self._window(
                args.environment,
                args.metrics,
                _iso(baseline_start),
                _iso(end),
                None,
            )
        except IntegrationError as exc:
            return {
                "selector": label,
                "available": False,
                "print": asdict(session),
                "unknown": ["sensor_history"],
                "error_code": exc.code,
            }
        baseline = tuple(
            point for point in series.points if str(point["time"]) < _iso(start)
        )
        during = tuple(
            point for point in series.points if str(point["time"]) >= _iso(start)
        )
        baseline_summary = summarize_points(baseline, args.metrics)
        during_summary = summarize_points(during, args.metrics)
        return {
            "selector": label,
            "available": bool(during),
            "print": asdict(session),
            "windows": {
                "baseline_start": _iso(baseline_start),
                "print_start": _iso(start),
                "print_end": _iso(end),
            },
            "sample_counts": {"baseline": len(baseline), "during_print": len(during)},
            "metrics": compare_summaries(
                baseline_summary, during_summary, args.metrics
            ),
            "causal": False,
            "limitation": "observed association does not establish that the printer caused a change",
        }

    def _history(self, args: SensorHistoryArgs) -> HistorySeries:
        return self.history.history(
            entity_id=args.entity,
            metric_ids=args.metrics,
            start=args.start,
            end=args.end,
            lookback=args.lookback,
            bucket=args.bucket,
            max_points=args.max_points,
        )

    def _window(
        self,
        entity: str,
        metrics: tuple[str, ...],
        start: str | None,
        end: str | None,
        lookback: str | None,
    ) -> HistorySeries:
        return self.history.history(
            entity_id=entity,
            metric_ids=metrics,
            start=start,
            end=end,
            lookback=lookback,
            bucket="auto",
            max_points=256,
        )

    @staticmethod
    def _missing_metrics(series: HistorySeries) -> list[str]:
        return [
            metric
            for metric in series.metrics
            if not any(metric in point for point in series.points)
        ]


def register_v2_skills(
    registry: SkillRegistry,
    entities: EntityRegistry,
    metrics: MetricRegistry,
    history: DashboardHistoryAdapter,
    printer: PrinterSnapshotProvider,
    desktop: DesktopWorkflow,
) -> None:
    impl = V2SkillImplementations(entities, metrics, history, printer, desktop)
    entity_ids = [
        item.entity_id for item in entities.entities if item.sensor_type != "printer"
    ]
    printer_ids = [
        item.entity_id for item in entities.entities if item.sensor_type == "printer"
    ]
    metric_ids = [item.metric_id for item in metrics.metrics]

    def spec(
        name: str,
        description: str,
        action: ActionClass,
        parser: object,
        authorizer: object,
        method: object,
        schema: dict[str, object],
        *,
        timeout: float = 10.0,
        side_effects: str = "none",
        explicit: bool = False,
        authentication: AuthenticationLevel = AuthenticationLevel.NONE,
        local_console_allowed: bool = False,
        configured: bool = True,
        available: bool = True,
        unavailable_reason: str | None = None,
    ) -> SkillSpec:
        return SkillSpec(
            name,
            description,
            action,
            parser,  # type: ignore[arg-type]
            authorizer,  # type: ignore[arg-type]
            method,  # type: ignore[arg-type]
            timeout,
            version="2.0.0",
            category=action.value,
            input_schema=schema,
            output_schema={
                "type": "object",
                "description": "bounded structured result",
            },
            result_description="Structured bounded semantic result with evidence labels.",
            permission_summary=(action.value, "local_policy_validation"),
            explicit_intent_required=explicit,
            confirmation_required=explicit,
            side_effects=side_effects,
            max_result_bytes=(
                65536
                if name == "get_sensor_history"
                else 32768
                if name == "analyze_print_environment"
                else 16384
            ),
            source_reference="butters.skills.v2",
            authentication=authentication,
            local_console_allowed=local_console_allowed,
            configured=configured,
            available=available,
            unavailable_reason=unavailable_reason,
        )

    history_schema = _schema(
        {
            "entity": _enum(entity_ids),
            "metrics": {
                "type": "array",
                "items": _enum(metric_ids),
                "minItems": 1,
                "maxItems": 12,
            },
            "start": {"type": ["string", "null"]},
            "end": {"type": ["string", "null"]},
            "lookback": {
                "type": ["string", "null"],
                "enum": [*HISTORY_LOOKBACKS, None],
            },
        },
        ["entity", "metrics", "start", "end", "lookback"],
    )
    registry.register(
        spec(
            "get_desktop_status",
            "Observe network, SSH, and Parsec readiness for the one configured desktop.",
            ActionClass.READ_ONLY,
            _parse_desktop,
            impl.authorize_desktop,
            impl.get_desktop_status,
            _schema({"machine": _enum(["desktop"])}, ["machine"]),
            timeout=5,
        )
    )
    registry.register(
        spec(
            "start_remote_desktop_session",
            "Run the fixed WOL, readiness, and remote-mode workflow for the configured desktop.",
            ActionClass.ACTION,
            _parse_desktop,
            impl.authorize_desktop,
            impl.start_remote_desktop_session,
            _schema({"machine": _enum(["desktop"])}, ["machine"]),
            timeout=desktop.settings.total_timeout_seconds + 5,
            side_effects="wake configured desktop and request its fixed remote mode",
            explicit=True,
            authentication=AuthenticationLevel.ELEVATED,
            local_console_allowed=True,
            configured=desktop.settings.remote_enabled,
            available=(
                desktop.settings.remote_enabled and desktop.broker_settings.enabled
            ),
            unavailable_reason=(
                None
                if desktop.settings.remote_enabled and desktop.broker_settings.enabled
                else "privileged action broker is not provisioned"
            ),
        )
    )
    registry.register(
        spec(
            "get_sensor_history",
            "Return a bounded time-ordered history for registered sensor metrics.",
            ActionClass.READ_ONLY,
            _parse_history,
            impl.authorize_history,
            impl.get_sensor_history,
            _schema(
                {
                    **cast(dict[str, object], history_schema["properties"]),
                    "bucket": _enum(list(HISTORY_BUCKETS)),
                    "max_points": {"type": "integer", "minimum": 1, "maximum": 256},
                },
                [
                    "entity",
                    "metrics",
                    "start",
                    "end",
                    "lookback",
                    "bucket",
                    "max_points",
                ],
            ),
        )
    )
    registry.register(
        spec(
            "summarize_sensor_window",
            "Calculate bounded local statistics for a sensor time window.",
            ActionClass.ANALYTICAL,
            _parse_window,
            impl.authorize_history,
            impl.summarize_sensor_window,
            history_schema,
        )
    )
    registry.register(
        spec(
            "compare_sensor_windows",
            "Compare deterministic statistics for two explicit sensor windows.",
            ActionClass.ANALYTICAL,
            _parse_compare,
            impl.authorize_compare,
            impl.compare_sensor_windows,
            _schema(
                {
                    "entity": _enum(entity_ids),
                    "metrics": {
                        "type": "array",
                        "items": _enum(metric_ids),
                        "minItems": 1,
                        "maxItems": 12,
                    },
                    "first_start": {"type": "string"},
                    "first_end": {"type": "string"},
                    "second_start": {"type": "string"},
                    "second_end": {"type": "string"},
                },
                [
                    "entity",
                    "metrics",
                    "first_start",
                    "first_end",
                    "second_start",
                    "second_end",
                ],
            ),
        )
    )
    registry.register(
        spec(
            "detect_metric_spikes",
            "Find the largest bounded deviation from a window median.",
            ActionClass.ANALYTICAL,
            _parse_spike,
            impl.authorize_spike,
            impl.detect_metric_spikes,
            _schema(
                {
                    "entity": _enum(entity_ids),
                    "metric": _enum(metric_ids),
                    "start": {"type": ["string", "null"]},
                    "end": {"type": ["string", "null"]},
                    "lookback": {
                        "type": ["string", "null"],
                        "enum": [*HISTORY_LOOKBACKS, None],
                    },
                },
                ["entity", "metric", "start", "end", "lookback"],
            ),
        )
    )
    registry.register(
        spec(
            "correlate_metrics",
            "Calculate guarded Pearson correlation for paired sensor samples; never infer causation.",
            ActionClass.ANALYTICAL,
            _parse_correlation,
            impl.authorize_correlation,
            impl.correlate_metrics,
            _schema(
                {
                    "entity": _enum(entity_ids),
                    "metric_x": _enum(metric_ids),
                    "metric_y": _enum(metric_ids),
                    "start": {"type": ["string", "null"]},
                    "end": {"type": ["string", "null"]},
                    "lookback": {
                        "type": ["string", "null"],
                        "enum": [*HISTORY_LOOKBACKS, None],
                    },
                },
                ["entity", "metric_x", "metric_y", "start", "end", "lookback"],
            ),
        )
    )
    registry.register(
        spec(
            "get_recent_prints",
            "Return bounded structured recent print sessions.",
            ActionClass.READ_ONLY,
            _parse_recent_prints,
            impl.authorize_printer,
            impl.get_recent_prints,
            _schema(
                {
                    "entity": _enum(printer_ids),
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["entity", "limit"],
            ),
        )
    )
    registry.register(
        spec(
            "get_print_details",
            "Return one print previously identified by the bounded printer history.",
            ActionClass.READ_ONLY,
            _parse_print_details,
            impl.authorize_printer,
            impl.get_print_details,
            _schema(
                {
                    "entity": _enum(printer_ids),
                    "print_id": {"type": "string", "maxLength": 128},
                },
                ["entity", "print_id"],
            ),
        )
    )
    registry.register(
        spec(
            "analyze_print_environment",
            "Compare local environmental statistics during a selected print with a bounded preceding baseline.",
            ActionClass.ANALYTICAL,
            _parse_print_analysis,
            impl.authorize_print_analysis,
            impl.analyze_print_environment,
            _schema(
                {
                    "printer": _enum(printer_ids),
                    "environment": _enum(
                        [
                            item.entity_id
                            for item in entities.entities
                            if item.sensor_type == "air_quality"
                        ]
                    ),
                    "metrics": {
                        "type": "array",
                        "items": _enum(list(PRINT_METRICS)),
                        "minItems": 1,
                        "maxItems": len(PRINT_METRICS),
                    },
                    "print_selector": _enum(
                        ["current", "previous", "current_vs_previous"]
                    ),
                    "baseline_minutes": {
                        "type": ["integer", "null"],
                        "minimum": 5,
                        "maximum": 1440,
                    },
                },
                [
                    "printer",
                    "environment",
                    "metrics",
                    "print_selector",
                    "baseline_minutes",
                ],
            ),
            timeout=20,
        )
    )


def _parse_desktop(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"machine"}))
    return DesktopArgs(required_string(values, "machine"))


def _parse_history(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(
        values,
        required=frozenset(
            {"entity", "metrics", "start", "end", "lookback", "bucket", "max_points"}
        ),
    )
    return SensorHistoryArgs(
        required_string(values, "entity"),
        required_string_tuple(values, "metrics", maximum=12),
        optional_string(values, "start"),
        optional_string(values, "end"),
        optional_string(values, "lookback"),
        required_string(values, "bucket"),
        _integer(values, "max_points", 1, 256),
    )


def _parse_window(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(
        values, required=frozenset({"entity", "metrics", "start", "end", "lookback"})
    )
    return SensorWindowArgs(
        required_string(values, "entity"),
        required_string_tuple(values, "metrics", maximum=12),
        optional_string(values, "start"),
        optional_string(values, "end"),
        optional_string(values, "lookback"),
    )


def _parse_compare(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(
        values,
        required=frozenset(
            {
                "entity",
                "metrics",
                "first_start",
                "first_end",
                "second_start",
                "second_end",
            }
        ),
    )
    return CompareWindowsArgs(
        required_string(values, "entity"),
        required_string_tuple(values, "metrics", maximum=12),
        required_string(values, "first_start"),
        required_string(values, "first_end"),
        required_string(values, "second_start"),
        required_string(values, "second_end"),
    )


def _parse_spike(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(
        values, required=frozenset({"entity", "metric", "start", "end", "lookback"})
    )
    return SpikeArgs(
        required_string(values, "entity"),
        required_string(values, "metric"),
        optional_string(values, "start"),
        optional_string(values, "end"),
        optional_string(values, "lookback"),
    )


def _parse_correlation(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(
        values,
        required=frozenset(
            {"entity", "metric_x", "metric_y", "start", "end", "lookback"}
        ),
    )
    return CorrelationArgs(
        required_string(values, "entity"),
        required_string(values, "metric_x"),
        required_string(values, "metric_y"),
        optional_string(values, "start"),
        optional_string(values, "end"),
        optional_string(values, "lookback"),
    )


def _parse_recent_prints(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"entity", "limit"}))
    return RecentPrintsArgs(
        required_string(values, "entity"), _integer(values, "limit", 1, 20)
    )


def _parse_print_details(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"entity", "print_id"}))
    print_id = required_string(values, "print_id")
    if len(print_id) > 128 or not all(
        character.isalnum() or character in "-_" for character in print_id
    ):
        raise SkillError("invalid_arguments", "print_id is invalid")
    return PrintDetailsArgs(required_string(values, "entity"), print_id)


def _parse_print_analysis(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(
        values,
        required=frozenset(
            {"printer", "environment", "metrics", "print_selector", "baseline_minutes"}
        ),
    )
    raw_baseline = values.get("baseline_minutes")
    baseline = (
        None if raw_baseline is None else _integer(values, "baseline_minutes", 5, 1440)
    )
    return PrintEnvironmentAnalysisArgs(
        required_string(values, "printer"),
        required_string(values, "environment"),
        required_string_tuple(values, "metrics", maximum=len(PRINT_METRICS)),
        required_string(values, "print_selector"),
        baseline,
    )


def _integer(values: Mapping[str, object], key: str, minimum: int, maximum: int) -> int:
    value = values.get(key)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise SkillError(
            "invalid_arguments", f"{key} must be an integer from {minimum} to {maximum}"
        )
    return value


def _schema(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _enum(values: list[str]) -> dict[str, object]:
    return {"type": "string", "enum": values}


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SkillError("invalid_response", "printer timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SkillError("invalid_response", "printer timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _evidence_labels() -> dict[str, object]:
    return {
        "observed": ["print", "windows", "sample_counts"],
        "calculated": ["metrics"],
        "inferred": [],
        "unknown": [],
        "causal_limit": "association is not proof of causation",
    }
