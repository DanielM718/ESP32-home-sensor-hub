from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.cloud.general import GeneralCloudTurn
from butters.cloud.model import CloudReasonerError, ToolRequest
from butters.integrations.history import HistorySeries
from butters.integrations.model import (
    PrinterIntelligenceSnapshot,
    PrinterSession,
    PrinterSnapshot,
    SensorRecord,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.stt.normalization import DomainVocabulary
from butters.web.service import BetaAssistantService


class Sensors:
    def snapshot(self):
        return SensorSnapshot(
            "2026-08-15T14:00:00Z",
            (
                SensorRecord(
                    "environment",
                    "3",
                    "2026-08-15T14:00:00Z",
                    1,
                    "online",
                    {"humidity": 42.0},
                ),
                SensorRecord(
                    "air_quality",
                    "office",
                    "2026-08-15T14:00:00Z",
                    1,
                    "online",
                    {
                        "temperature_c": 25.0,
                        "humidity": 45.0,
                        "co2": 600,
                        "pm25": 4.0,
                        "voc_index": 80,
                    },
                ),
            ),
        )


class Health:
    def snapshot(self):
        return ServerHealthSnapshot(1, 0, 0, 0, 1, 0, 1, 1, 40, "0x0", ())


SESSION = PrinterSession(
    "x2d",
    "print-1",
    "job-1",
    "part.3mf",
    "2026-08-15T13:00:00Z",
    "2026-08-15T14:00:00Z",
    3600,
    100,
    "PLA",
    "completed",
    {},
    "local_observation",
)


class Printer:
    def current(self):
        return PrinterSnapshot(
            "x2d",
            "X2D",
            True,
            "printing",
            "2026-08-15T14:00:00Z",
            {
                "job_name": "part.3mf",
                "progress_percent": 50,
                "print_started_at": "2026-08-15T13:00:00Z",
            },
        )

    def environment_summary(self):
        raise RuntimeError("not used")

    def intelligence(self):
        return PrinterIntelligenceSnapshot({}, (), (), ())

    def current_session(self):
        return SESSION

    def recent_sessions(self, _limit):
        return (SESSION,)

    def session(self, print_id):
        return SESSION if print_id == SESSION.print_id else None


class History:
    calls = 0

    def history(self, **_kwargs):
        self.calls += 1
        return HistorySeries(
            "printer_room",
            "2026-08-15T12:00:00Z",
            "2026-08-15T14:00:00Z",
            "1m",
            tuple(_kwargs["metric_ids"]),
            (
                {"time": "2026-08-15T12:30:00Z", "voc_index": 50.0, "pm25": 2.0},
                {"time": "2026-08-15T12:45:00Z", "voc_index": 60.0, "pm25": 2.5},
                {"time": "2026-08-15T13:15:00Z", "voc_index": 100.0, "pm25": 5.0},
                {"time": "2026-08-15T13:30:00Z", "voc_index": 120.0, "pm25": 6.0},
            ),
            "live_1m",
        )


class Reasoner:
    def __init__(self, turns=(), *, available=True, error: str | None = None):
        self.turns = list(turns)
        self._available = available
        self.error = error
        self.calls = 0
        self.inputs = []

    @property
    def available(self):
        return self._available

    def reason(self, **kwargs):
        self.calls += 1
        self.inputs.append(kwargs)
        if self.error:
            raise CloudReasonerError(self.error, "fixture")
        if self.turns:
            return self.turns.pop(0)
        return GeneralCloudTurn(
            kwargs["model"],
            kwargs["effort"],
            0.01,
            response_id=f"response-{self.calls}",
            response_text=(
                "OBSERVED: the print window and sensor samples were available. "
                "CALCULATED: VOC was higher during the print. INFERRED: the timing supports an association, not proof of causation."
            ),
        )


def _service(tmp_path: Path, reasoner: Reasoner):
    base = load_assistant_settings()
    settings = replace(
        base,
        cloud=replace(
            base.cloud,
            enabled=reasoner.available,
            allow_paid_calls=reasoner.available,
            max_retries=0,
        ),
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(base.web, state_dir=tmp_path, development_mode=True),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    printer = Printer()
    assistant = create_assistant(
        settings,
        vocabulary,
        sensor_adapter=Sensors(),
        server_adapter=Health(),
        printer_adapter=printer,
    )
    implementation = assistant.skills.get(
        "analyze_print_environment"
    ).implementation.__self__
    implementation.history = History()
    return BetaAssistantService(
        settings,
        vocabulary,
        assistant=assistant,
        general_reasoner=reasoner,
        state_dir=tmp_path,
    )


def test_representative_tier_zero_requests_never_call_cloud(tmp_path: Path) -> None:
    reasoner = Reasoner()
    service = _service(tmp_path, reasoner)
    session = service.sessions.create()
    humidity = service.handle_text(session, "What is the humidity in box 3?")
    readings = service.handle_text(
        session, "What are the readings in the printer room?"
    )
    printer = service.handle_text(session, "What is my printer doing right now?")
    assert humidity.route == readings.route == printer.route == "deterministic"
    assert humidity.skill == "get_sensor_value"
    assert readings.skill == "get_sensor_values"
    assert printer.skill == "get_printer_status"
    assert reasoner.calls == 0


def test_print_environment_request_prefetches_local_calculation_then_synthesizes(
    tmp_path: Path,
) -> None:
    reasoner = Reasoner()
    service = _service(tmp_path, reasoner)
    response = service.handle_text(
        service.sessions.create(),
        "Compare VOC and PM2.5 during this print with the hour before it.",
    )
    assert response.route == "general_cloud"
    assert reasoner.calls == 1
    context = reasoner.inputs[0]["context"]
    evidence = next(
        item["content"]
        for item in context
        if "BOUNDED LOCAL OBSERVATIONS" in item["content"]
    )
    assert "mean_delta" in evidence
    assert "causal" in evidence
    assert "169.254" not in evidence
    assert "/home/dmejiame/scripts" not in evidence
    trace = service.traces.get(response.trace_id)
    assert trace and any(event.status == "prefetch_complete" for event in trace.events)


def test_print_heating_request_routes_to_temperature_analysis(tmp_path: Path) -> None:
    reasoner = Reasoner()
    service = _service(tmp_path, reasoner)
    response = service.handle_text(
        service.sessions.create(),
        "Did this print noticeably heat the printer room?",
    )
    assert response.route == "general_cloud"
    context = reasoner.inputs[0]["context"]
    evidence = next(
        item["content"]
        for item in context
        if "BOUNDED LOCAL OBSERVATIONS" in item["content"]
    )
    assert '"temperature"' in evidence
    assert '"voc_index"' not in evidence


def test_cloud_disabled_print_analysis_is_honest_and_local_features_still_work(
    tmp_path: Path,
) -> None:
    reasoner = Reasoner(available=False)
    service = _service(tmp_path, reasoner)
    response = service.handle_text(
        service.sessions.create(),
        "Do you think this print is causing the VOC increase?",
    )
    assert response.route == "deterministic"
    assert reasoner.calls == 0
    assert "Cloud reasoning is currently unavailable" in response.response_text
    assert "completed" not in response.response_text.casefold()


def test_unknown_or_action_tool_requested_by_cloud_is_locally_denied(
    tmp_path: Path,
) -> None:
    for request in (
        ToolRequest("call-1", "unregistered_skill", {}),
        ToolRequest("call-2", "start_remote_desktop_session", {"machine": "desktop"}),
        ToolRequest(
            "call-3",
            "get_sensor_history",
            {
                "entity": "printer_room",
                "metrics": ["voc_index"],
                "start": None,
                "end": None,
                "lookback": "1h",
                "bucket": "auto",
                "max_points": 10,
                "query": "secrets",
            },
        ),
    ):
        reasoner = Reasoner(
            [
                GeneralCloudTurn(
                    "gpt-5.6-terra",
                    "high",
                    0.01,
                    response_id="response-tool",
                    tool_request=request,
                )
            ]
        )
        service = _service(tmp_path / request.call_id, reasoner)
        response = service.handle_text(
            service.sessions.create(),
            "Do you think this print is causing the VOC increase?",
        )
        assert response.route == "local_fallback"
        # Either refusal is correct. A name outside the offered list is stopped
        # at the provider boundary before the policy layer sees it; anything the
        # provider was legitimately handed is stopped by the policy.
        assert response.stopping_reason and (
            response.stopping_reason.startswith("tool_policy_")
            or response.stopping_reason == "tool_not_offered"
        )


def test_repeated_tool_call_and_provider_timeout_stop_cleanly(tmp_path: Path) -> None:
    call = ToolRequest(
        "call-1",
        "summarize_sensor_window",
        {
            "entity": "printer_room",
            "metrics": ["voc_index"],
            "start": None,
            "end": None,
            "lookback": "1h",
        },
    )
    turns = [
        GeneralCloudTurn(
            "gpt-5.6-terra", "high", 0.01, response_id="one", tool_request=call
        ),
        GeneralCloudTurn(
            "gpt-5.6-terra", "high", 0.01, response_id="two", tool_request=call
        ),
    ]
    repeated = _service(tmp_path / "repeat", Reasoner(turns))
    # The prompt has to select summarize_sensor_window, otherwise the provider
    # is never handed it and the call is refused at the boundary before the
    # repeat detector this test exists to exercise can run.
    response = repeated.handle_text(
        repeated.sessions.create(),
        "Do you think there is a correlation between this print and the VOC increase?",
    )
    assert response.stopping_reason == "repeated_tool_call"

    timed = _service(tmp_path / "timeout", Reasoner(error="timeout"))
    failure = timed.handle_text(
        timed.sessions.create(),
        "Do you think this print is causing the VOC increase?",
    )
    assert failure.route == "local_fallback"
    assert failure.stopping_reason == "timeout"


# --- the derived catalog is the provider boundary in both directions -------

UNROUTABLE = "Do you think there is a correlation between this print and the VOC increase?"


def _cloud_response(tmp_path: Path, tool: ToolRequest, prompt: str = UNROUTABLE):
    reasoner = Reasoner(
        [
            GeneralCloudTurn(
                "gpt-5.6-terra", "high", 0.01, response_id="r1", tool_request=tool
            )
        ]
    )
    service = _service(tmp_path, reasoner)
    return service.handle_text(service.sessions.create(), prompt)


@pytest.mark.parametrize(
    "skill",
    [
        # Registered, enabled, read-only, and excluded from the model catalog on
        # privacy or administrator-audience grounds. _relevant_skill_tools
        # refuses to build a schema for these; execution has to refuse them too.
        "get_butters_host_status",
        "get_storage_status",
        "get_network_service_health",
        "get_nas_status",
        "get_environment_control_status",
        "wait_for_desktop_reachability",
    ],
)
def test_a_provider_cannot_run_a_tool_it_was_never_handed(
    tmp_path: Path, skill: str
) -> None:
    response = _cloud_response(
        tmp_path / skill, ToolRequest("call-1", skill, {})
    )

    assert response.stopping_reason == "tool_not_offered"
    assert response.route == "local_fallback"


def test_a_provider_cannot_run_a_tool_the_prompt_did_not_select(
    tmp_path: Path,
) -> None:
    """Narrowing by prompt is the boundary, not merely a prompt-size saving.

    get_printer_status is a perfectly ordinary catalogued read, but this prompt
    never selects it, so the provider was never told it existed.
    """

    response = _cloud_response(
        tmp_path, ToolRequest("call-1", "get_printer_status", {"entity": "x2d"})
    )

    assert response.stopping_reason == "tool_not_offered"


def test_a_tool_the_prompt_did_select_still_runs(tmp_path: Path) -> None:
    """The boundary must not close the cloud tool path it is guarding."""

    response = _cloud_response(
        tmp_path,
        ToolRequest(
            "call-1",
            "summarize_sensor_window",
            {
                "entity": "printer_room",
                "metrics": ["voc_index"],
                "start": None,
                "end": None,
                "lookback": "1h",
            },
        ),
    )

    assert response.stopping_reason != "tool_not_offered"
