from __future__ import annotations

from butters.assistant import AsyncAssistantResponder, create_assistant
from butters.assistant_config import load_assistant_settings
from butters.integrations.model import (
    SensorRecord,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.stt.normalization import load_domain_vocabulary

from butters.config import default_vocabulary_path


class Sensors:
    def snapshot(self) -> SensorSnapshot:
        return SensorSnapshot(
            "2026-08-11T12:00:00Z",
            (
                SensorRecord(
                    "air_quality",
                    "office",
                    "2026-08-11T11:59:55Z",
                    5,
                    "online",
                    {"co2": 742},
                    ("co2",),
                ),
            ),
        )


class Health:
    def snapshot(self) -> ServerHealthSnapshot:
        return ServerHealthSnapshot(
            1.0, 0.1, 0.1, 0.1, 2_000_000_000, 0, 1, 2, 50.0, "0x0", ()
        )


def test_text_mode_runs_normalize_route_skill_and_format() -> None:
    settings = load_assistant_settings()
    vocabulary = load_domain_vocabulary(default_vocabulary_path())
    assistant = create_assistant(
        settings,
        vocabulary,
        sensor_adapter=Sensors(),  # type: ignore[arg-type]
        server_adapter=Health(),  # type: ignore[arg-type]
    )

    response = assistant.handle_text("what's the co two level")

    assert response.normalized_text == "what's the CO2 level"
    assert response.route.skill == "get_sensor_value"
    assert response.execution is not None and response.execution.ok
    assert response.response_text == "Printer room CO2 is 742 ppm."


def test_text_mode_ambiguity_never_calls_skill() -> None:
    settings = load_assistant_settings()
    vocabulary = load_domain_vocabulary(default_vocabulary_path())
    assistant = create_assistant(
        settings,
        vocabulary,
        sensor_adapter=Sensors(),  # type: ignore[arg-type]
        server_adapter=Health(),  # type: ignore[arg-type]
    )

    response = assistant.handle_text("what is the humidity")

    assert response.route.status == "clarification"
    assert response.execution is None
    assert response.response_text == "Which sensor did you mean?"


def test_async_responder_keeps_live_handoff_bounded_and_closes() -> None:
    settings = load_assistant_settings()
    assistant = create_assistant(
        settings,
        load_domain_vocabulary(default_vocabulary_path()),
        sensor_adapter=Sensors(),  # type: ignore[arg-type]
        server_adapter=Health(),  # type: ignore[arg-type]
    )
    responses = []
    responder = AsyncAssistantResponder(assistant, responses.append, max_pending=1)

    assert responder.submit("what is the CO2 level")
    responder.close()

    assert [response.response_text for response in responses] == [
        "Printer room CO2 is 742 ppm."
    ]
    assert not responder.submit("what is the CO2 level")
