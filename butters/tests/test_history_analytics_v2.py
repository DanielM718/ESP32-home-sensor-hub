from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from butters.assistant_config import load_assistant_settings
from butters.integrations.desktop import DesktopWorkflow
from butters.integrations.history import DashboardHistoryAdapter, HistorySeries
from butters.integrations.model import IntegrationError, PrinterSession
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.skills.analytics import (
    compare_summaries,
    correlate,
    detect_spike,
    summarize_points,
)
from butters.skills.model import PrintEnvironmentAnalysisArgs
from butters.skills.v2 import V2SkillImplementations

NOW = datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc)


class Response:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


def _adapter(payload: dict[str, object], observed: list[str] | None = None):
    settings = load_assistant_settings()
    entities = EntityRegistry(settings.entities)
    metrics = MetricRegistry()

    def opener(request, **_kwargs):
        if observed is not None:
            observed.append(request.full_url)
        return Response(payload)

    return DashboardHistoryAdapter(
        settings.integration,
        entities,
        metrics,
        opener=opener,
        now=lambda: NOW,
    )


def _payload(points: list[dict[str, object]]) -> dict[str, object]:
    return {
        "generated_at": "2026-08-15T16:00:00Z",
        "range": "1h",
        "window": "1m",
        "data_tier": "live_1m",
        "series": [
            {"sensor_type": "air_quality", "location": "office", "points": points}
        ],
    }


def test_history_uses_only_typed_dashboard_query_and_stable_time_order() -> None:
    urls: list[str] = []
    adapter = _adapter(
        _payload(
            [
                {"time": "2026-08-15T15:30:00Z", "voc_index": 120, "pm25": None},
                {"time": "2026-08-15T15:10:00Z", "voc_index": 80, "pm25": 2.5},
                {"time": "2026-08-15T15:20:00Z", "pm25": 3.5},
            ]
        ),
        urls,
    )
    result = adapter.history(
        entity_id="printer_room",
        metric_ids=("voc_index", "pm25"),
        start=None,
        end=None,
        lookback="1h",
        bucket="auto",
        max_points=20,
    )
    assert [point["time"] for point in result.points] == sorted(
        point["time"] for point in result.points
    )
    assert result.points[1] == {"time": "2026-08-15T15:20:00Z", "pm25": 3.5}
    assert len(urls) == 1
    assert "/api/readings?" in urls[0]
    assert "location=office" in urls[0]
    assert "query=" not in urls[0].casefold()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"entity_id": "unknown"}, "policy_denied"),
        ({"metric_ids": ("password",)}, "policy_denied"),
        ({"lookback": "31d"}, "invalid_arguments"),
        ({"max_points": 2049}, "invalid_arguments"),
        ({"bucket": "1s"}, "invalid_arguments"),
    ],
)
def test_history_rejects_invalid_entity_metric_lookback_count_and_bucket(
    kwargs, code
) -> None:
    values = {
        "entity_id": "printer_room",
        "metric_ids": ("voc_index",),
        "start": None,
        "end": None,
        "lookback": "1h",
        "bucket": "auto",
        "max_points": 100,
    }
    values.update(kwargs)
    with pytest.raises((IntegrationError, ValueError)) as denied:
        _adapter(_payload([])).history(**values)
    if isinstance(denied.value, IntegrationError):
        assert denied.value.code == code


def test_history_enforces_explicit_thirty_day_limit_and_result_count() -> None:
    adapter = _adapter(_payload([{"time": "2026-08-15T15:00:00Z", "voc_index": 1}]))
    with pytest.raises(IntegrationError, match="thirty days") as old:
        adapter.history(
            entity_id="printer_room",
            metric_ids=("voc_index",),
            start="2026-07-01T00:00:00Z",
            end="2026-07-01T01:00:00Z",
            lookback=None,
        )
    assert old.value.code == "lookback_limit"
    with pytest.raises(IntegrationError) as large:
        adapter.history(
            entity_id="printer_room",
            metric_ids=("voc_index",),
            start=None,
            end=None,
            lookback="1h",
            max_points=0,
        )
    assert large.value.code == "invalid_arguments"


def test_local_statistics_cover_summary_spike_comparison_and_correlation() -> None:
    points = tuple(
        {
            "time": f"2026-08-15T15:{minute:02d}:00Z",
            "voc_index": float(value),
            "pm25": float(value) / 10,
        }
        for minute, value in enumerate((10, 12, 14, 50, 18, 20))
    )
    summary = summarize_points(points, ("voc_index",))
    assert summary["voc_index"]["count"] == 6
    assert summary["voc_index"]["median"] == 16
    assert summary["voc_index"]["absolute_delta"] == 10
    assert summary["voc_index"]["slope_per_hour"] is not None
    spike = detect_spike(points, "voc_index")
    assert spike["spike_value"] == 50
    assert spike["spike_time"] == "2026-08-15T15:03:00Z"
    relation = correlate(points, "voc_index", "pm25")
    assert relation["pearson_r"] == 1
    assert relation["causal"] is False
    comparison = compare_summaries(
        summarize_points(points[:3], ("voc_index",)),
        summarize_points(points[3:], ("voc_index",)),
        ("voc_index",),
    )
    assert comparison["voc_index"]["mean_delta"] > 0


def test_empty_and_short_windows_report_insufficient_evidence() -> None:
    assert summarize_points((), ("voc_index",))["voc_index"] == {
        "count": 0,
        "available": False,
    }
    assert (
        detect_spike(({"time": "t", "voc_index": 1.0},), "voc_index")["available"]
        is False
    )
    assert correlate((), "voc_index", "pm25")["available"] is False


class History:
    def history(self, **_kwargs) -> HistorySeries:
        return HistorySeries(
            "printer_room",
            "2026-08-15T12:00:00Z",
            "2026-08-15T16:00:00Z",
            "1m",
            ("voc_index", "pm25"),
            (
                {"time": "2026-08-15T12:30:00Z", "voc_index": 50.0, "pm25": 2.0},
                {"time": "2026-08-15T12:45:00Z", "voc_index": 60.0, "pm25": 3.0},
                {"time": "2026-08-15T13:15:00Z", "voc_index": 100.0, "pm25": 5.0},
                {"time": "2026-08-15T13:30:00Z", "voc_index": 120.0, "pm25": 6.0},
            ),
            "live_1m",
        )


class UnavailableHistory:
    def history(self, **_kwargs) -> HistorySeries:
        raise IntegrationError("upstream_unavailable", "fixture detail")


class Printer:
    session_value = PrinterSession(
        "x2d",
        "print-one",
        "job-one",
        "part.3mf",
        "2026-08-15T13:00:00Z",
        "2026-08-15T14:00:00Z",
        3600,
        100.0,
        None,
        "completed",
        {},
        "local_observation",
    )

    def current_session(self):
        return self.session_value

    def recent_sessions(self, _limit):
        return (self.session_value,)

    def session(self, _print_id):
        return self.session_value


def test_print_window_analysis_uses_preprint_baseline_and_marks_causation_false() -> (
    None
):
    settings = load_assistant_settings()
    entities = EntityRegistry(settings.entities)
    metrics = MetricRegistry()
    implementation = V2SkillImplementations(
        entities,
        metrics,
        History(),  # type: ignore[arg-type]
        Printer(),  # type: ignore[arg-type]
        DesktopWorkflow(settings.desktop),
    )
    result = implementation.analyze_print_environment(
        PrintEnvironmentAnalysisArgs(
            "x2d",
            "printer_room",
            ("voc_index", "pm25"),
            "current",
            60,
        )
    )
    assert result.kind == "print_environment_analysis"
    assert result.data["sample_counts"] == {"baseline": 2, "during_print": 2}
    assert result.data["metrics"]["voc_index"]["mean_delta"] == 55
    assert result.data["causal"] is False


def test_missing_print_or_start_time_is_explicitly_unknown() -> None:
    settings = load_assistant_settings()
    entities = EntityRegistry(settings.entities)
    metrics = MetricRegistry()
    printer = Printer()
    printer.session_value = None  # type: ignore[assignment]
    implementation = V2SkillImplementations(
        entities,
        metrics,
        History(),  # type: ignore[arg-type]
        printer,  # type: ignore[arg-type]
        DesktopWorkflow(settings.desktop),
    )
    result = implementation.analyze_print_environment(
        PrintEnvironmentAnalysisArgs(
            "x2d", "printer_room", ("voc_index",), "current", None
        )
    )
    assert result.data["available"] is False
    assert result.data["unknown"] == ["print_session"]


def test_print_analysis_reports_unavailable_history_without_leaking_detail() -> None:
    settings = load_assistant_settings()
    implementation = V2SkillImplementations(
        EntityRegistry(settings.entities),
        MetricRegistry(),
        UnavailableHistory(),  # type: ignore[arg-type]
        Printer(),  # type: ignore[arg-type]
        DesktopWorkflow(settings.desktop),
    )
    result = implementation.analyze_print_environment(
        PrintEnvironmentAnalysisArgs(
            "x2d", "printer_room", ("voc_index",), "current", 60
        )
    )
    assert result.data["available"] is False
    assert result.data["unknown"] == ["sensor_history"]
    assert result.data["error_code"] == "upstream_unavailable"
    assert "fixture detail" not in str(result.data)
