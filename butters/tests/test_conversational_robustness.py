from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from butters.assistant import create_assistant
from butters.assistant_config import EntitySettings, load_assistant_settings
from butters.integrations.model import (
    SensorRecord,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.routing.conversation import (
    pending_from_route,
    route_conversation_turn,
)
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.routing.router import IntentRouter
from butters.stt.normalization import DomainVocabulary
from butters.web.service import BetaAssistantService


@pytest.fixture
def router() -> IntentRouter:
    settings = load_assistant_settings()
    return IntentRouter(EntityRegistry(settings.entities), MetricRegistry())


@pytest.mark.parametrize(
    ("text", "entity", "metric"),
    (
        ("What is the CO2 level in the printer room", "printer_room", "co2"),
        ("What is the CO2 level in the printed room", "printer_room", "co2"),
        ("CO2 in the prnter room", "printer_room", "co2"),
        ("CO2 in the printer rom", "printer_room", "co2"),
        ("Tempature in box 3", "filament_box_3", "temperature"),
        ("temprature, please, in BOX THREE!", "filament_box_3", "temperature"),
        ("the humidty in box 2", "filament_box_2", "humidity"),
        ("uh, humdity for container two", "filament_box_2", "humidity"),
        ("CO₂ in the printer room", "printer_room", "co2"),
        ("co 2 in the printer room", "printer_room", "co2"),
        ("PM 2.5 in the printer room", "printer_room", "pm25"),
        ("pm25 in the printer room", "printer_room", "pm25"),
    ),
)
def test_registered_typing_and_transcription_variants_are_bounded(
    router: IntentRouter,
    text: str,
    entity: str,
    metric: str,
) -> None:
    route = router.route(text)

    assert route.matched
    assert route.arguments == {"entity": entity, "metric": metric}


@pytest.mark.parametrize(
    ("text", "entity", "metrics"),
    (
        (
            "What are the readings in the printer room?",
            "printer_room",
            [
                "temperature",
                "humidity",
                "co2",
                "pm25",
                "pm10",
                "voc_index",
                "nox_index",
            ],
        ),
        (
            "What are the readings in box 2?",
            "filament_box_2",
            ["temperature", "humidity", "battery_voltage"],
        ),
        (
            "Show me all measurements for box three.",
            "filament_box_3",
            ["temperature", "humidity", "battery_voltage"],
        ),
        (
            "Give me everything the printer room sensor is measuring.",
            "printer_room",
            [
                "temperature",
                "humidity",
                "co2",
                "pm25",
                "pm10",
                "voc_index",
                "nox_index",
            ],
        ),
        (
            "How are the printer room readings?",
            "printer_room",
            [
                "temperature",
                "humidity",
                "co2",
                "pm25",
                "pm10",
                "voc_index",
                "nox_index",
            ],
        ),
    ),
)
def test_aggregate_intent_expands_capabilities_in_registry_order(
    router: IntentRouter,
    text: str,
    entity: str,
    metrics: list[str],
) -> None:
    route = router.route(text)

    assert route.matched and route.aggregate
    assert route.skill == "get_sensor_values"
    assert route.arguments == {"entity": entity, "metrics": metrics}
    assert len(metrics) == len(set(metrics))


def test_explicit_metrics_take_priority_over_aggregate_nouns(
    router: IntentRouter,
) -> None:
    route = router.route("What are the humidity and temperature readings in box 3?")

    assert route.matched and not route.aggregate
    assert route.arguments == {
        "entity": "filament_box_3",
        "metrics": ["humidity", "temperature"],
    }


@pytest.mark.parametrize(
    "text",
    (
        "astrology readings are confusing",
        "these readings in a printed book were lovely",
        "temperate prose about a printed bloom",
        "attempt and humidifier labels in box 3",
        "the vmc index in a catalog",
        "nax index documentation",
        "tell me about the vapor in this novel",
    ),
)
def test_unrelated_language_does_not_silently_become_a_sensor_read(
    router: IntentRouter, text: str
) -> None:
    route = router.route(text)

    assert not route.matched


def test_short_metric_tokens_are_exact_only(router: IntentRouter) -> None:
    assert not router.route("What is the VMC index in the printer room?").matched
    assert not router.route("What is the NAX index in the printer room?").matched


def test_exact_alias_wins_over_a_fuzzy_neighbor() -> None:
    registry = EntityRegistry(
        (
            EntitySettings("exact", "Printer rat", "air_quality", "a", (), ()),
            EntitySettings("neighbor", "Printer ram", "air_quality", "b", (), ()),
        )
    )

    resolution = registry.resolve("co2 in printer rat")

    assert resolution.entity is not None
    assert resolution.entity.entity_id == "exact"
    assert not resolution.fuzzy


def test_fuzzy_near_tie_clarifies_in_stable_order() -> None:
    entities = (
        EntitySettings("alpha", "Station plane", "air_quality", "a", (), ()),
        EntitySettings("beta", "Station place", "air_quality", "b", (), ()),
    )
    router = IntentRouter(EntityRegistry(entities), MetricRegistry())

    route = router.route("What is the CO2 in station plate?")

    assert route.incomplete
    assert route.arguments == {"metric": "co2"}
    assert route.ambiguity_candidates == ("alpha", "beta")
    assert "Station plane" in (route.message or "")
    assert "Station place" in (route.message or "")


class CountingSensors:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> SensorSnapshot:
        self.calls += 1
        return SensorSnapshot(
            "2026-08-15T12:00:00Z",
            (
                SensorRecord(
                    "environment",
                    "2",
                    "2026-08-15T11:59:55Z",
                    5,
                    "online",
                    {
                        "temperature_c": 23.4,
                        "humidity": 41.6,
                        "battery_mv": 3712,
                        "battery_measurement_ok": False,
                    },
                    ("temperature_c", "humidity", "battery_mv"),
                ),
                SensorRecord(
                    "environment",
                    "3",
                    "2026-08-15T11:59:55Z",
                    5,
                    "online",
                    {"temperature_c": 24.1, "humidity": 38.0},
                    ("temperature_c", "humidity"),
                ),
                SensorRecord(
                    "air_quality",
                    "office",
                    "2026-08-15T11:59:55Z",
                    5,
                    "online",
                    {
                        "temperature_c": 22.8,
                        "humidity": 44.0,
                        "co2": 712,
                        "pm25": 2.4,
                        "pm10": 4.1,
                        "voc_index": 91,
                        "nox_index": 2,
                    },
                ),
            ),
        )


class Health:
    def snapshot(self) -> ServerHealthSnapshot:
        return ServerHealthSnapshot(1, 0, 0, 0, 1, 0, 1, 1, 40, "0x0", ())


class NoCloud:
    available = False


def _service(tmp_path: Path) -> tuple[BetaAssistantService, CountingSensors]:
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(base.web, state_dir=tmp_path, development_mode=True),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    sensors = CountingSensors()
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(
        settings,
        vocabulary,
        sensor_adapter=sensors,  # type: ignore[arg-type]
        server_adapter=Health(),  # type: ignore[arg-type]
    )
    return (
        BetaAssistantService(
            settings,
            vocabulary,
            assistant=assistant,
            general_reasoner=NoCloud(),  # type: ignore[arg-type]
            state_dir=tmp_path,
        ),
        sensors,
    )


def test_aggregate_execution_uses_one_snapshot_and_names_missing_values(
    tmp_path: Path,
) -> None:
    service, sensors = _service(tmp_path)
    session = service.sessions.create()

    response = service.handle_text(session, "What are the readings in box 2?")

    assert response.skill == "get_sensor_values"
    assert sensors.calls == 1
    assert response.response_text == (
        "Filament box two temperature is 23.4 degrees Celsius and humidity is "
        "42 percent. Filament box two battery voltage is unavailable: battery "
        "measurement is not valid in the latest packet."
    )


def test_missing_entity_clarification_executes_original_metric(
    tmp_path: Path,
) -> None:
    service, sensors = _service(tmp_path)
    session = service.sessions.create()

    first = service.handle_text(session, "What is the CO2 level?")
    second = service.handle_text(session, "Printer room")

    assert first.route == "clarification"
    assert second.route == "deterministic" and second.skill == "get_sensor_value"
    assert "712 ppm" in second.response_text
    assert sensors.calls == 1
    assert session.pending_clarification is None
    assert [item.text for item in session.messages if item.role == "user"] == [
        "What is the CO2 level?",
        "Printer room",
    ]


def test_too_weak_typo_can_be_clarified_without_losing_the_metric(
    tmp_path: Path,
) -> None:
    service, _sensors = _service(tmp_path)
    session = service.sessions.create()

    first = service.handle_text(session, "What is CO2 in the preentar rune?")
    second = service.handle_text(session, "Printer room")

    assert first.route == "clarification"
    assert second.skill == "get_sensor_value"
    assert "CO2 is 712 ppm" in second.response_text


def test_missing_metric_clarification_fills_only_the_metric(tmp_path: Path) -> None:
    service, _sensors = _service(tmp_path)
    session = service.sessions.create()

    first = service.handle_text(session, "What should I check in box 3?")
    second = service.handle_text(session, "Humidity")

    assert first.route == "clarification"
    assert second.skill == "get_sensor_value"
    assert "Filament box three humidity is 38 percent" in second.response_text
    assert session.pending_clarification is None


def test_complete_new_request_replaces_pending_clarification(tmp_path: Path) -> None:
    service, sensors = _service(tmp_path)
    session = service.sessions.create()

    service.handle_text(session, "What is the CO2 level?")
    replacement = service.handle_text(session, "What is the humidity in box 3?")

    assert replacement.skill == "get_sensor_value"
    assert "Filament box three humidity" in replacement.response_text
    assert "CO2" not in replacement.response_text
    assert sensors.calls == 1
    assert session.pending_clarification is None


def test_pending_state_is_session_scoped_and_clear_removes_it(tmp_path: Path) -> None:
    service, _sensors = _service(tmp_path)
    first_session = service.sessions.create(peer_key="peer:a")
    second_session = service.sessions.create(peer_key="peer:b")

    service.handle_text(first_session, "What is the CO2 level?")
    isolated = service.handle_text(second_session, "Printer room")

    assert first_session.pending_clarification is not None
    assert second_session.pending_clarification is None
    assert isolated.route == "unsupported"

    service.clear_conversation(first_session)
    assert first_session.pending_clarification is None
    assert first_session.messages == []


def test_invalid_reply_keeps_original_expiry_and_then_expires(
    router: IntentRouter,
) -> None:
    first = router.route("What is the humidity?")
    pending = pending_from_route(
        first,
        "What is the humidity?",
        now=10.0,
        ttl_seconds=5.0,
    )
    assert pending is not None

    retry = route_conversation_turn(
        router,
        "y level",
        pending,
        now=12.0,
        ttl_seconds=5.0,
    )
    expired = route_conversation_turn(
        router,
        "Printer room",
        retry.pending,
        now=15.0,
        ttl_seconds=5.0,
    )

    assert retry.disposition == "retry" and retry.pending is not None
    assert retry.pending.created_monotonic == 10.0
    assert retry.pending.expires_monotonic == 15.0
    assert expired.disposition == "expired"
    assert not expired.route.matched
    assert expired.pending is None


@pytest.mark.parametrize(
    "control",
    (
        "turn on the printer",
        "turn off box 3",
        "restart the printer room station",
        "shut down the printer room sensor",
        "set box 1 to 40 percent",
    ),
)
def test_control_request_is_refused_even_while_a_clarification_is_pending(
    router: IntentRouter, control: str
) -> None:
    """An open slot must never turn a control phrase into a sensor reading."""

    first = router.route("What is the temperature?")
    pending = pending_from_route(first, first.normalized_text, now=0, ttl_seconds=30)
    assert pending is not None

    outcome = route_conversation_turn(
        router, control, pending, now=1.0, ttl_seconds=30
    )

    assert not outcome.route.matched
    assert outcome.route.message == router.route(control).message
    assert "Control requests are disabled" in (outcome.route.message or "")
    assert outcome.pending is None


def test_ambiguous_entity_reply_cannot_escape_original_candidates(
    router: IntentRouter,
) -> None:
    first = router.route("temperature in box 1 and box 2")
    pending = pending_from_route(first, first.normalized_text, now=0, ttl_seconds=30)
    assert pending is not None

    outside = router.continue_clarification(pending, "box 3")
    selected = router.continue_clarification(pending, "box 2")

    assert outside.incomplete
    assert outside.ambiguity_candidates == (
        "filament_box_1",
        "filament_box_2",
    )
    assert selected.matched
    assert selected.arguments == {
        "entity": "filament_box_2",
        "metric": "temperature",
    }
