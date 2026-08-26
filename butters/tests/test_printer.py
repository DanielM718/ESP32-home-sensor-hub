from __future__ import annotations

import io
import json
from dataclasses import asdict, fields

import pytest
from butters.assistant import create_assistant
from butters.assistant_config import IntegrationSettings, load_assistant_settings
from butters.integrations.model import (
    PrintEnvironmentSnapshot,
    PrinterIntelligenceSnapshot,
    PrinterSnapshot,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.integrations.printer import DashboardPrinterAdapter
from butters.responses.formatter import ResponseFormatter
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.routing.router import IntentRouter
from butters.skills.implementations import (
    MAINTENANCE_MAX_RESULT_BYTES,
    build_read_only_registry,
)
from butters.skills.model import (
    ActionClass,
    CurrentPrintResult,
    LastPrintResult,
    PrintEnvironmentResult,
    PrinterMaintenanceEventsResult,
    PrinterMaintenanceResult,
    PrinterStatusResult,
    PrinterTemperaturesResult,
    PrinterUsageResult,
    SkillExecution,
)
from butters.skills.registry import SkillSpec
from butters.stt.normalization import load_domain_vocabulary

from butters.config import default_vocabulary_path


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class Printers:
    def __init__(self, state: PrinterSnapshot | None = None) -> None:
        self.state = state or PrinterSnapshot(
            printer_id="x2d",
            printer_model="X2D",
            online=True,
            normalized_state="printing",
            observed_at="2026-08-11T12:00:00Z",
            values={
                "job_name": "dragon.3mf",
                "progress_percent": 37.5,
                "remaining_seconds": 5400,
                "current_layer": 74,
                "total_layers": 200,
                "active_material": "PLA",
                "nozzle_1_temperature": 220,
                "nozzle_1_target": 225,
                "nozzle_2_temperature": 250,
                "bed_temperature": 60,
                "chamber_temperature": 38,
            },
            provenance={"active_material": "observed"},
        )

    def current(self) -> PrinterSnapshot:
        return self.state

    def environment_summary(self) -> PrintEnvironmentSnapshot:
        return PrintEnvironmentSnapshot(
            available=True,
            reason=None,
            observational=True,
            session={"job_name": "dragon.3mf", "result": "completed"},
            metrics={
                "pm25": {"print_peak": 12.5},
                "voc_index": {"change_from_baseline": 30.0},
            },
            voc_recovery_seconds=600,
        )

    def intelligence(self) -> PrinterIntelligenceSnapshot:
        return PrinterIntelligenceSnapshot(
            usage={
                "tracked_print_seconds": 45_000,
                "tracked_print_hours": 12.5,
                "tracked_job_count": 8,
                "tracked_history_complete": False,
                "rolling_tracked_print_hours_per_day": 2.5,
                "maintenance_mode": "normal",
                "maintenance_mode_reason": "tracked_average_between_thresholds",
            },
            maintenance_tasks=(
                {
                    "maintenance_task_id": "x2d_xy_axis_clean_lubricate",
                    "name": "XY axis cleaning",
                    "enabled": True,
                    "state": "overdue",
                    "overdue": True,
                    "warning": False,
                },
            ),
            completion_history=({"completed_at": "2026-08-01T12:00:00Z"},),
            print_history=(
                {
                    "job_name": "dragon.3mf",
                    "result": "completed",
                    "duration_seconds": 5400,
                    "source": "locally_observed",
                },
            ),
            maintenance_summary={"overdue_count": 1},
            maintenance_notifications=(
                {
                    "event_type": "maintenance_overdue",
                    "created_at": "2026-08-15T12:00:00Z",
                },
            ),
        )

    def usage(self) -> dict[str, object]:
        return self.intelligence().usage

    def maintenance(self) -> PrinterIntelligenceSnapshot:
        return self.intelligence()

    def maintenance_events(self, limit: int):
        return self.intelligence().maintenance_notifications[:limit]


class Sensors:
    def snapshot(self) -> SensorSnapshot:
        return SensorSnapshot("2026-08-11T12:00:00Z", ())


class Health:
    def snapshot(self) -> ServerHealthSnapshot:
        return ServerHealthSnapshot(
            1.0, 0.1, 0.1, 0.1, 1_000_000_000, 0, 1, 2, 50.0, "0x0", ()
        )


def _registry(printer: Printers | None = None):
    settings = load_assistant_settings()
    return build_read_only_registry(
        Sensors(),
        Health(),
        EntityRegistry(settings.entities),
        MetricRegistry(),
        printer or Printers(),
    )


@pytest.mark.parametrize(
    ("phrase", "skill"),
    (
        ("is the printer running", "get_printer_status"),
        ("what is the printer doing", "get_printer_status"),
        ("what is the X2D printing", "get_current_print"),
        ("how much longer is left", "get_current_print"),
        ("what layer is it on", "get_current_print"),
        ("what material is printing", "get_current_print"),
        (
            "what is the nozzle temperature on the Bambu printer",
            "get_printer_temperatures",
        ),
        ("what was peak PM2.5 during the last print", "get_print_environment_summary"),
        ("how did VOC change during the last print", "get_print_environment_summary"),
        (
            "how long did air quality take to recover after the print",
            "get_print_environment_summary",
        ),
        ("how many hours has the printer run", "get_printer_usage"),
        ("how many prints has the X2D done", "get_printer_usage"),
        ("how much has my printer printed", "get_printer_usage"),
        ("how heavily have I been using the printer", "get_printer_usage"),
        ("what printer maintenance is overdue", "get_printer_maintenance"),
        ("what maintenance is coming up", "get_printer_maintenance"),
        ("does my printer need maintenance", "get_printer_maintenance"),
        (
            "why does this maintenance item say baseline required",
            "get_printer_maintenance",
        ),
        ("when was the printer last serviced", "get_printer_maintenance"),
        ("show printer maintenance events", "get_printer_maintenance_events"),
        ("how long was the last print", "get_last_print"),
    ),
)
def test_printer_questions_route_to_typed_read_only_skills(
    phrase: str, skill: str
) -> None:
    settings = load_assistant_settings()
    router = IntentRouter(EntityRegistry(settings.entities), MetricRegistry())
    route = router.route(phrase)
    assert route.matched
    assert route.skill == skill
    assert route.arguments == {"entity": "x2d"}


def test_printer_room_alias_wins_over_generic_printer_alias() -> None:
    settings = load_assistant_settings()
    router = IntentRouter(EntityRegistry(settings.entities), MetricRegistry())
    route = router.route("what is the printer room temperature")
    assert route.skill == "get_sensor_value"
    assert route.arguments == {"entity": "printer_room", "metric": "temperature"}


@pytest.mark.parametrize(
    "phrase",
    (
        "start the printer",
        "stop the X2D",
        "pause the Bambu printer",
        "set the printer bed temperature to 70",
    ),
)
def test_printer_control_language_remains_unsupported(phrase: str) -> None:
    settings = load_assistant_settings()
    route = IntentRouter(EntityRegistry(settings.entities), MetricRegistry()).route(
        phrase
    )
    assert route.status == "unsupported"
    assert "read-only" in (route.message or "")


def test_printer_skills_return_typed_results() -> None:
    registry = _registry()
    expected = {
        "get_printer_status": PrinterStatusResult,
        "get_current_print": CurrentPrintResult,
        "get_printer_temperatures": PrinterTemperaturesResult,
        "get_print_environment_summary": PrintEnvironmentResult,
        "get_printer_usage": PrinterUsageResult,
        "get_printer_maintenance": PrinterMaintenanceResult,
        "get_printer_maintenance_events": PrinterMaintenanceEventsResult,
        "get_last_print": LastPrintResult,
    }
    for name, result_type in expected.items():
        execution = registry.execute(name, {"entity": "x2d"})
        assert execution.ok
        assert isinstance(execution.result, result_type)
        if isinstance(execution.result, PrinterUsageResult):
            assert execution.result.intelligence.print_history == ()
        if isinstance(execution.result, PrinterMaintenanceEventsResult):
            assert len(execution.result.events) <= 20


def test_unknown_printer_and_generic_sensor_skill_are_default_denied() -> None:
    registry = _registry()
    unknown = registry.execute("get_printer_status", {"entity": "garage_printer"})
    wrong_skill = registry.execute("get_sensor_status", {"entity": "x2d"})
    assert unknown.failure and unknown.failure.code == "policy_denied"
    assert wrong_skill.failure and wrong_skill.failure.code == "policy_denied"


def test_no_printer_control_skill_is_registered() -> None:
    registry = _registry()
    names = {spec.name for spec in registry.skills}
    assert names >= {
        "get_printer_status",
        "get_current_print",
        "get_printer_temperatures",
        "get_print_environment_summary",
        "get_printer_usage",
        "get_printer_maintenance",
        "get_printer_maintenance_events",
        "get_last_print",
    }
    assert names.isdisjoint(
        {"start_print", "stop_print", "pause_print", "resume_print", "cancel_print"}
    )
    assert {spec.action_class for spec in registry.skills} == {ActionClass.READ_ONLY}


def test_printer_responses_preserve_material_provenance_and_observational_wording() -> (
    None
):
    formatter = ResponseFormatter()
    current = formatter.format_execution(
        SkillExecution(
            "get_current_print",
            ActionClass.READ_ONLY,
            0.01,
            CurrentPrintResult(Printers().current()),
        )
    )
    environment = formatter.format_execution(
        SkillExecution(
            "get_print_environment_summary",
            ActionClass.READ_ONLY,
            0.01,
            PrintEnvironmentResult(Printers().environment_summary()),
        )
    )
    assert "observed material PLA" in current
    assert "layer 74 of 200" in current
    assert "peak PM2.5 was 12.5" in environment
    assert "observational association, not proof of causation" in environment


def test_end_to_end_assistant_uses_printer_provider() -> None:
    settings = load_assistant_settings()
    assistant = create_assistant(
        settings,
        load_domain_vocabulary(default_vocabulary_path()),
        sensor_adapter=Sensors(),  # type: ignore[arg-type]
        server_adapter=Health(),  # type: ignore[arg-type]
        printer_adapter=Printers(),
    )
    response = assistant.handle_text("what is the X2D printing")
    assert response.execution is not None and response.execution.ok
    assert "dragon.3mf" in response.response_text
    assert "1 hour 30 minutes remaining" in response.response_text


def test_dashboard_printer_adapter_parses_current_and_environment_payloads() -> None:
    payloads = {
        "/api/printer": {
            "printer_id": "x2d",
            "printer_model": "X2D",
            "online": True,
            "normalized_state": "idle",
            "observed_at": "2026-08-11T12:00:00Z",
            "provenance": {},
        },
        "/api/printer/environment-summary": {
            "available": True,
            "observational": True,
            "session": {"session_id": "one"},
            "metrics": {"pm25": {"print_peak": 4.2}},
            "voc_recovery_seconds": 300,
        },
        "/api/printer/maintenance": {
            "usage": {"tracked_print_hours": 12.5},
            "summary": {"baseline_required_count": 1},
            "tasks": [
                {
                    "maintenance_task_id": "camera",
                    "name": "Camera cleaning",
                    "state": "baseline_required",
                    "baseline_required": True,
                    "warning": False,
                }
            ],
            "completion_history": [],
            "recent_notifications": [],
            "manufacturer_source": {"source": "bambu_lab_x2d_wiki"},
        },
        "/api/printer/usage": {
            "usage": {
                "tracked_print_seconds": 45_000,
                "tracked_print_hours": 12.5,
                "tracked_job_count": 8,
                "tracked_history_complete": False,
            }
        },
        "/api/printer/maintenance/events?limit=20&pending=false": {
            "events": [
                {
                    "event_type": event_type,
                    "created_at": "2026-08-15T12:00:00Z",
                }
                for event_type in (
                    "maintenance_due_soon",
                    "maintenance_due",
                    "maintenance_overdue",
                    "maintenance_returned_to_ok",
                    "heavy_use_mode_entered",
                    "heavy_use_mode_exited",
                )
            ]
        },
        "/api/printer/history?limit=100": {
            "history": [{"job_name": "cube", "duration_seconds": 60}],
        },
    }

    def opener(request, **_kwargs):
        path = request.full_url.removeprefix("http://127.0.0.1:8080")
        return Response(json.dumps(payloads[path]).encode())

    adapter = DashboardPrinterAdapter(IntegrationSettings(), opener=opener)
    assert adapter.current().normalized_state == "idle"
    assert adapter.environment_summary().metrics["pm25"]["print_peak"] == 4.2
    assert adapter.usage()["tracked_print_seconds"] == 45_000
    maintenance = adapter.maintenance()
    assert maintenance.maintenance_tasks[0]["state"] == "baseline_required"
    assert maintenance.manufacturer_source["source"] == "bambu_lab_x2d_wiki"
    assert [item["event_type"] for item in adapter.maintenance_events()] == [
        "maintenance_due_soon",
        "maintenance_due",
        "maintenance_overdue",
        "maintenance_returned_to_ok",
        "heavy_use_mode_entered",
        "heavy_use_mode_exited",
    ]
    assert adapter.intelligence().usage["tracked_print_hours"] == 12.5


def test_printer_intelligence_responses_remain_read_only_and_provenance_clear() -> None:
    formatter = ResponseFormatter()
    intelligence = Printers().intelligence()
    usage = formatter.format_execution(
        SkillExecution(
            "get_printer_usage",
            ActionClass.READ_ONLY,
            0.01,
            PrinterUsageResult(intelligence),
        )
    )
    maintenance = formatter.format_execution(
        SkillExecution(
            "get_printer_maintenance",
            ActionClass.READ_ONLY,
            0.01,
            PrinterMaintenanceResult(intelligence),
        )
    )
    last = formatter.format_execution(
        SkillExecution(
            "get_last_print",
            ActionClass.READ_ONLY,
            0.01,
            LastPrintResult(intelligence),
        )
    )
    assert "Tracked Print Time is 12.5 hours" in usage
    assert "not lifetime usage" in usage
    assert "Overdue printer maintenance" in maintenance
    assert "dragon.3mf" in last and "1 hour 30 minutes" in last


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        (
            "baseline_required",
            "Maintenance baseline has not been recorded yet",
        ),
        ("due_soon", "coming due soon"),
        ("due", "maintenance is due"),
        ("overdue", "Overdue printer maintenance"),
        ("advisory", "Manufacturer inspection advisories"),
        ("ok", "currently OK"),
    ),
)
def test_maintenance_state_wording_preserves_semantics(
    state: str, expected: str
) -> None:
    intelligence = PrinterIntelligenceSnapshot(
        usage={},
        maintenance_tasks=(
            {
                "name": "Test task",
                "enabled": True,
                "state": state,
                "baseline_required": state == "baseline_required",
                "warning": state == "due_soon",
                "next_due_at": None,
            },
        ),
        completion_history=(),
        print_history=(),
    )
    text = ResponseFormatter().format_execution(
        SkillExecution(
            "get_printer_maintenance",
            ActionClass.READ_ONLY,
            0.01,
            PrinterMaintenanceResult(intelligence),
        )
    )
    assert expected in text
    if state == "baseline_required":
        assert "not due or overdue" in text


CADENCE = (
    "Every month at >= 5 printing hours/day, every 2 months at 1-5 printing "
    "hours/day, every 3 months below 1 printing hour/day"
)


def _maintenance_task(index: int, state: str) -> dict[str, object]:
    """One task shaped like DashboardPrinterAdapter.MAINTENANCE_TASK_FIELDS."""

    return {
        "maintenance_task_id": f"x2d_manufacturer_maintenance_task_{index:02d}",
        "name": f"Clean and lubricate manufacturer catalog item {index:02d}",
        "enabled": True,
        "state": state,
        "baseline_required": state == "baseline_required",
        "warning": False,
        "due": False,
        "overdue": False,
        "trigger_kind": "usage_tiered_months",
        "cadence": CADENCE,
        "next_due_at": "2026-11-15T12:00:00+00:00",
        "remaining_days": 92,
        "manufacturer_source": "Bambu Lab X2D maintenance wiki",
        "manufacturer_source_url": "https://wiki.bambulab.com/en/x2d/maintenance",
        "manufacturer_source_revision": "2026-05-01",
        "warning_source": "dashboard_policy",
        "last_completed_at": "2026-08-01T12:00:00+00:00",
        "completion_count": 3,
        "applied_interval_months": 2,
        "maintenance_mode_applied": "normal",
    }


def _catalog_maintenance(
    task_count: int = 11, baseline_count: int = 3
) -> PrinterIntelligenceSnapshot:
    """Rebuild the production-shaped maintenance payload the skill must return."""

    tasks = tuple(
        _maintenance_task(
            index, "baseline_required" if index < baseline_count else "ok"
        )
        for index in range(task_count)
    )
    return PrinterIntelligenceSnapshot(
        usage={
            "tracked_print_seconds": 45_000,
            "tracked_print_hours": 12.5,
            "tracked_job_count": 8,
            "tracked_history_complete": False,
            "tracked_history_completeness_reasons": [
                "tracked_history_started_after_first_print"
            ],
            "tracked_history_provenance": "locally_observed",
            "rolling_tracked_print_hours_per_day": 2.5,
            "maintenance_mode": "normal",
            "maintenance_mode_reason": "tracked_average_between_1_and_5_print_hours_per_day",
        },
        maintenance_tasks=tasks,
        completion_history=tuple(
            {
                "maintenance_task_id": f"x2d_manufacturer_maintenance_task_{index:02d}",
                "completed_at": "2026-08-01T12:00:00+00:00",
                "effective_usage_hours": 12.5,
                "completed_print_count": 8,
                "source": "dashboard_ui",
            }
            for index in range(20)
        ),
        print_history=(),
        maintenance_summary={
            "baseline_required_count": baseline_count,
            "due_count": 0,
            "overdue_count": 0,
            "due_soon_count": 0,
            "advisory_count": 0,
        },
        maintenance_notifications=tuple(
            {
                "event_id": f"maintenance-event-{index:04d}",
                "subject_type": "maintenance_task",
                "subject_id": f"x2d_manufacturer_maintenance_task_{index:02d}",
                "event_type": "maintenance_baseline_required",
                "previous_state": "ok",
                "new_state": "baseline_required",
                "created_at": "2026-08-15T12:00:00+00:00",
                "delivery_status": "delivered",
                "delivered_at": "2026-08-15T12:00:01+00:00",
            }
            for index in range(20)
        ),
        manufacturer_source={
            "source": "Bambu Lab X2D maintenance wiki",
            "source_url": "https://wiki.bambulab.com/en/x2d/maintenance",
            "source_revision": "2026-05-01",
        },
    )


class CatalogPrinters(Printers):
    """Printer provider whose maintenance payload matches the seeded catalog."""

    def __init__(self, task_count: int = 11, baseline_count: int = 3) -> None:
        super().__init__()
        self._maintenance = _catalog_maintenance(task_count, baseline_count)

    def maintenance(self) -> PrinterIntelligenceSnapshot:
        return self._maintenance


def _default_result_budget() -> int:
    """SkillSpec uses slots, so read the declared default from its field."""

    field = next(item for item in fields(SkillSpec) if item.name == "max_result_bytes")
    return int(field.default)


def _encoded_result_bytes(result: object) -> int:
    """Encode exactly the way SkillRegistry.execute enforces its budget."""

    return len(
        json.dumps(
            asdict(result),
            default=str,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )


def test_maintenance_skill_budget_admits_the_full_seeded_catalog() -> None:
    registry = _registry(CatalogPrinters())
    execution = registry.execute("get_printer_maintenance", {"entity": "x2d"})
    assert execution.failure is None
    assert execution.ok
    result = execution.result
    assert isinstance(result, PrinterMaintenanceResult)
    encoded = _encoded_result_bytes(result)
    # The production failure was measured at roughly 9663 bytes against the
    # 8192-byte SkillSpec default.
    assert encoded >= 9663
    assert encoded <= MAINTENANCE_MAX_RESULT_BYTES
    assert len(result.intelligence.maintenance_tasks) == 11
    summary = result.intelligence.maintenance_summary
    assert summary["baseline_required_count"] == 3
    assert summary["due_count"] == 0
    assert summary["overdue_count"] == 0


def test_catalog_maintenance_result_would_fail_under_the_default_budget() -> None:
    result = PrinterMaintenanceResult(_catalog_maintenance())
    assert _encoded_result_bytes(result) > _default_result_budget()


def test_baseline_required_catalog_answer_still_says_not_due_or_overdue() -> None:
    registry = _registry(CatalogPrinters())
    execution = registry.execute("get_printer_maintenance", {"entity": "x2d"})
    text = ResponseFormatter().format_execution(execution)
    assert "Maintenance baseline has not been recorded yet" in text
    assert "not due or overdue" in text
    assert "Overdue printer maintenance" not in text
    assert "maintenance is due" not in text


def test_maintenance_skill_still_rejects_results_beyond_its_own_bound() -> None:
    registry = _registry(CatalogPrinters(task_count=200, baseline_count=3))
    execution = registry.execute("get_printer_maintenance", {"entity": "x2d"})
    assert execution.failure is not None
    assert execution.failure.code == "result_too_large"
    text = ResponseFormatter().format_execution(execution)
    assert "safe response limit" in text
    assert "The read-only request could not be completed." not in text


def test_only_the_maintenance_skill_raises_its_result_budget() -> None:
    registry = _registry(CatalogPrinters())
    budgets = {spec.name: spec.max_result_bytes for spec in registry.skills}
    assert budgets.pop("get_printer_maintenance") == MAINTENANCE_MAX_RESULT_BYTES
    assert set(budgets.values()) == {_default_result_budget()}
    assert _default_result_budget() == 8192
    # The raised budget stays a bounded schema-derived cap, not an open limit.
    assert MAINTENANCE_MAX_RESULT_BYTES < 64 * 1024


def test_sibling_printer_skills_still_succeed_under_the_default_budget() -> None:
    registry = _registry(CatalogPrinters())
    for name, result_type in (
        ("get_printer_usage", PrinterUsageResult),
        ("get_printer_maintenance_events", PrinterMaintenanceEventsResult),
    ):
        execution = registry.execute(name, {"entity": "x2d"})
        assert execution.ok, name
        assert isinstance(execution.result, result_type)
