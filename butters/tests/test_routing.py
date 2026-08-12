from __future__ import annotations

import pytest
from butters.assistant_config import load_assistant_settings
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.routing.router import IntentRouter


@pytest.fixture
def router() -> IntentRouter:
    settings = load_assistant_settings()
    return IntentRouter(EntityRegistry(settings.entities), MetricRegistry())


@pytest.mark.parametrize(
    ("phrase", "skill", "arguments"),
    [
        (
            "what is the humidity in box three",
            "get_sensor_value",
            {"entity": "filament_box_3", "metric": "humidity"},
        ),
        (
            "what's box 3 humidity",
            "get_sensor_value",
            {"entity": "filament_box_3", "metric": "humidity"},
        ),
        (
            "how humid is filament box three",
            "get_sensor_value",
            {"entity": "filament_box_3", "metric": "humidity"},
        ),
        (
            "humidity for container 3",
            "get_sensor_value",
            {"entity": "filament_box_3", "metric": "humidity"},
        ),
        (
            "what's the co two level in the printer room",
            "get_sensor_value",
            {"entity": "printer_room", "metric": "co2"},
        ),
        (
            "printer room carbon dioxide reading",
            "get_sensor_value",
            {"entity": "printer_room", "metric": "co2"},
        ),
        (
            "what's the printer room p m two point five",
            "get_sensor_value",
            {"entity": "printer_room", "metric": "pm25"},
        ),
        (
            "what is the printer room VOC index",
            "get_sensor_value",
            {"entity": "printer_room", "metric": "voc_index"},
        ),
        (
            "what is the printer room NOx index",
            "get_sensor_value",
            {"entity": "printer_room", "metric": "nox_index"},
        ),
        (
            "printer room temperature",
            "get_sensor_value",
            {"entity": "printer_room", "metric": "temperature"},
        ),
        (
            "when was filament box two last seen",
            "get_sensor_last_seen",
            {"entity": "filament_box_2"},
        ),
        (
            "is every sensor reporting",
            "get_sensor_status",
            {"entity": None},
        ),
        (
            "which filament box has the highest humidity",
            "compare_sensor_metric",
            {"group": "filament_boxes", "metric": "humidity", "operation": "max"},
        ),
        (
            "how is the printer room air quality",
            "get_room_air_quality",
            {"entity": "printer_room"},
        ),
        ("what is the server status", "get_server_health", {}),
    ],
)
def test_phrase_variations_route_by_concepts(
    router: IntentRouter,
    phrase: str,
    skill: str,
    arguments: dict[str, object],
) -> None:
    route = router.route(phrase)

    assert route.status == "matched"
    assert route.skill == skill
    assert route.arguments == arguments
    assert route.confidence >= 0.9


@pytest.mark.parametrize(
    ("phrase", "message"),
    [
        ("what is the humidity", "Which sensor"),
        ("humidity in a filament box", "Which filament box"),
        ("when was the sensor last seen", "Which sensor"),
        ("box one and box two humidity", "Which sensor"),
    ],
)
def test_ambiguous_requests_require_clarification(
    router: IntentRouter, phrase: str, message: str
) -> None:
    route = router.route(phrase)

    assert route.status == "clarification"
    assert message in (route.message or "")
    if phrase != "box one and box two humidity":
        assert route.incomplete
        assert route.missing_arguments == ("entity",)


def test_metric_without_location_is_explicitly_incomplete(router: IntentRouter) -> None:
    route = router.route("what is the carbon dioxide level")

    assert route.incomplete
    assert route.skill == "get_sensor_value"
    assert route.arguments == {"metric": "co2"}
    assert route.missing_arguments == ("entity",)


def test_benign_fillers_do_not_change_router_meaning(router: IntentRouter) -> None:
    route = router.route(
        "what is the uh carbon dioxide level in the printer room"
    )

    assert route.matched
    assert route.arguments == {"entity": "printer_room", "metric": "co2"}
    assert "uh" not in route.normalized_text


@pytest.mark.parametrize(
    "phrase",
    [
        "turn on the printer exhaust",
        "restart influxdb",
        "wake my desktop",
        "set the humidity to twenty",
    ],
)
def test_control_requests_are_explicitly_unsupported(
    router: IntentRouter, phrase: str
) -> None:
    route = router.route(phrase)

    assert route.status == "unsupported"
    assert "read-only" in (route.message or "")


def test_unknown_complex_request_remains_unresolved(router: IntentRouter) -> None:
    route = router.route("explain why my print failed yesterday")

    assert route.status == "unsupported"
    assert route.skill is None
