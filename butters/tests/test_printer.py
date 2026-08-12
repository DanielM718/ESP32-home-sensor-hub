from __future__ import annotations

import io
import json

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
from butters.skills.implementations import build_read_only_registry
from butters.skills.model import (
    ActionClass,
    CurrentPrintResult,
    LastPrintResult,
    PrintEnvironmentResult,
    PrinterMaintenanceResult,
    PrinterStatusResult,
    PrinterTemperaturesResult,
    PrinterUsageResult,
    SkillExecution,
)
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
                "locally_observed_print_hours": 12.5,
                "locally_observed_completed_print_count": 8,
                "printer_reported_lifetime_hours": None,
                "maintenance_effective_lifetime_hours": 12.5,
            },
            maintenance_tasks=(
                {"name": "User inspection", "enabled": True, "overdue": True},
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
        )


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
        ("what printer maintenance is overdue", "get_printer_maintenance"),
        ("when was the printer last serviced", "get_printer_maintenance"),
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
        "get_last_print": LastPrintResult,
    }
    for name, result_type in expected.items():
        execution = registry.execute(name, {"entity": "x2d"})
        assert execution.ok
        assert isinstance(execution.result, result_type)


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
            "usage": {"locally_observed_print_hours": 12.5},
            "tasks": [],
            "completion_history": [],
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
    assert adapter.intelligence().usage["locally_observed_print_hours"] == 12.5


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
    assert "12.5 locally observed print hours" in usage
    assert "does not expose a printer-reported lifetime counter" in usage
    assert "Overdue printer maintenance" in maintenance
    assert "dragon.3mf" in last and "1 hour 30 minutes" in last
