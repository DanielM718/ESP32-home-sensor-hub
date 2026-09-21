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


MULTI_MEASUREMENT_PHRASES = (
    "what is the temperature and humidity in box 3",
    "what are the temperature and humidity in box three",
    "give me humidity and temperature for box three",
)


@pytest.mark.parametrize(
    ("phrase", "metrics"),
    [
        ("what is the temperature in box 2", ["temperature"]),
        ("what is the humidity in box 2", ["humidity"]),
    ],
)
def test_one_measurement_keeps_the_single_value_contract(
    router: IntentRouter, phrase: str, metrics: list[str]
) -> None:
    route = router.route(phrase)

    assert route.status == "matched"
    assert route.skill == "get_sensor_value"
    assert route.arguments == {"entity": "filament_box_2", "metric": metrics[0]}


@pytest.mark.parametrize(
    ("phrase", "metrics"),
    [
        (MULTI_MEASUREMENT_PHRASES[0], ["temperature", "humidity"]),
        (MULTI_MEASUREMENT_PHRASES[1], ["temperature", "humidity"]),
        (MULTI_MEASUREMENT_PHRASES[2], ["humidity", "temperature"]),
        (
            "printer room temperature and humidity and co2",
            ["temperature", "humidity", "co2"],
        ),
        (
            "temperature, humidity, and temperature in box 3",
            ["temperature", "humidity"],
        ),
        (
            "relative humidity and temp and humidity in box three",
            ["humidity", "temperature"],
        ),
        (
            "PRINTER ROOM: PM10, CO₂, VOC, and NOx!",
            ["pm10", "co2", "voc_index", "nox_index"],
        ),
        (
            "printer room co2, temperature, humidity, pm2.5, pm10, voc, and nox",
            [
                "co2",
                "temperature",
                "humidity",
                "pm25",
                "pm10",
                "voc_index",
                "nox_index",
            ],
        ),
    ],
)
def test_several_named_measurements_route_as_one_request(
    router: IntentRouter, phrase: str, metrics: list[str]
) -> None:
    route = router.route(phrase)
    entity = "printer_room" if "printer room" in phrase.casefold() else "filament_box_3"

    assert route.status == "matched"
    assert route.skill == "get_sensor_values"
    assert route.arguments == {"entity": entity, "metrics": metrics}
    assert route.confidence >= 0.9


@pytest.mark.parametrize("phrase", MULTI_MEASUREMENT_PHRASES)
def test_named_measurements_are_never_treated_as_measurement_ambiguity(
    router: IntentRouter, phrase: str
) -> None:
    """Listing the measurements is the answer to "which one", not a question."""

    route = router.route(phrase)

    assert route.status != "clarification"
    assert "Which measurement" not in (route.message or "")
    assert "metric" not in route.missing_arguments


@pytest.mark.parametrize(
    ("phrase", "message"),
    [
        ("what is the temperature and humidity", "Which sensor did you mean?"),
        (
            "temperature and humidity in a filament box",
            "Which filament box did you mean?",
        ),
    ],
)
def test_multiple_measurements_still_clarify_a_missing_sensor(
    router: IntentRouter, phrase: str, message: str
) -> None:
    route = router.route(phrase)

    assert route.status == "clarification"
    assert route.message == message
    assert route.incomplete
    assert route.skill == "get_sensor_values"
    assert route.arguments == {"metrics": ["temperature", "humidity"]}
    assert route.missing_arguments == ("entity",)


def test_a_measurement_the_sensor_cannot_report_is_still_refused(
    router: IntentRouter,
) -> None:
    route = router.route("what is the temperature and co2 in box 3")

    assert route.status == "unsupported"
    assert route.message == "Filament box three does not provide CO2."


def test_multiple_measurements_preserve_entity_ambiguity(
    router: IntentRouter,
) -> None:
    route = router.route("temperature and humidity in box 1 and box 2")

    assert route.status == "clarification"
    assert route.skill == "get_sensor_values"
    assert route.arguments == {"metrics": ["temperature", "humidity"]}
    assert route.missing_arguments == ("entity",)
    assert route.ambiguity_candidates == (
        "filament_box_1",
        "filament_box_2",
    )
    assert "Filament box one" in (route.message or "")
    assert "Filament box two" in (route.message or "")


@pytest.mark.parametrize(
    "phrase",
    (
        "attempt and humidifier labels in box 3",
        "restart the temperature and humidity sensors in box 3",
    ),
)
def test_unrelated_conjunctions_do_not_become_multi_measurement_reads(
    router: IntentRouter, phrase: str
) -> None:
    route = router.route(phrase)

    assert route.skill != "get_sensor_values"
    assert not route.matched


def test_metric_without_location_is_explicitly_incomplete(router: IntentRouter) -> None:
    route = router.route("what is the carbon dioxide level")

    assert route.incomplete
    assert route.skill == "get_sensor_value"
    assert route.arguments == {"metric": "co2"}
    assert route.missing_arguments == ("entity",)


def test_benign_fillers_do_not_change_router_meaning(router: IntentRouter) -> None:
    route = router.route("what is the uh carbon dioxide level in the printer room")

    assert route.matched
    assert route.arguments == {"entity": "printer_room", "metric": "co2"}
    assert "uh" not in route.normalized_text


@pytest.mark.parametrize(
    "phrase",
    [
        "turn on the printer exhaust",
        "restart influxdb",
        "set the humidity to twenty",
    ],
)
def test_control_requests_are_explicitly_unsupported(
    router: IntentRouter, phrase: str
) -> None:
    route = router.route(phrase)

    assert route.status == "unsupported"
    assert "read-only" in (route.message or "")


def test_wake_desktop_routes_to_the_registered_bounded_action(
    router: IntentRouter,
) -> None:
    route = router.route("wake my desktop")

    assert route.matched
    assert route.skill == "wake_desktop"
    assert route.arguments == {"machine": "desktop"}


@pytest.mark.parametrize(
    ("phrase", "skill"),
    (
        ("turn off my monitors", "monitors_off"),
        ("headless mode", "monitors_off"),
        ("remote mode", "monitors_off"),
        ("turn on my monitors", "monitors_on"),
        ("local mode", "monitors_on"),
        ("restore local mode", "monitors_on"),
    ),
)
def test_desktop_monitor_compatibility_phrases_are_deterministic(
    router: IntentRouter, phrase: str, skill: str
) -> None:
    route = router.route(phrase)

    assert route.matched
    assert route.skill == skill
    assert route.arguments == {"machine": "desktop"}


def test_unknown_complex_request_remains_unresolved(router: IntentRouter) -> None:
    route = router.route("explain why my print failed yesterday")

    assert route.status == "unsupported"
    assert route.skill is None


# --------------------- NAS and Jellyfin availability ------------------------
#
# A live Butters test asked "What is the current status of my NAS and
# Jellyfin?" and got "Which sensor did you mean?". The NAS status skill was
# fine; the deterministic matcher only knew the two phrases "nas status" and
# "is my nas online", so anything else fell through to sensor handling, found
# no sensor entity, and asked for clarification.


@pytest.mark.parametrize(
    "phrase",
    [
        "nas status",
        "is my nas online",
        "what is the current status of my nas",
        "what is the current status of my nas and jellyfin",
        "how are my nas and jellyfin doing",
        "is the nas healthy",
        "is my nas reachable",
        # Jellyfin alone is legitimate here: NasAdapter.status() carries the
        # jellyfin observation, so this skill can actually answer it.
        "is jellyfin running",
        "is jellyfin available",
        "what is the status of jellyfin",
    ],
)
def test_nas_and_jellyfin_availability_is_answered_deterministically(
    router: IntentRouter, phrase: str
) -> None:
    route = router.route(phrase)

    assert route.status == "matched", route.message
    assert route.skill == "get_nas_status"
    assert route.arguments == {}
    # Deterministic: high confidence and no model is consulted for any of them.
    assert route.confidence >= 0.9


@pytest.mark.parametrize(
    "phrase",
    [
        "what is the current status of my nas and jellyfin",
        "what is the status of jellyfin",
        "what is the current status of my nas",
    ],
)
def test_the_nas_question_never_asks_which_sensor(
    router: IntentRouter, phrase: str
) -> None:
    route = router.route(phrase)

    assert route.status != "clarification"
    assert "Which sensor" not in (route.message or "")
    assert route.skill not in {"get_sensor_status", "get_sensor_value"}


@pytest.mark.parametrize(
    ("phrase", "skill"),
    [
        # "status" belongs to whichever subject the sentence actually names.
        ("what is the status of all sensors", "get_sensor_status"),
        ("is filament box 1 online", "get_sensor_status"),
        ("printer status", "get_printer_status"),
        ("desktop status", "get_desktop_status"),
        ("butters service status", "get_butters_service_status"),
        ("what is the server status", "get_server_health"),
        ("heater status", "get_environment_control_status"),
        ("storage status", "get_storage_status"),
        ("action broker status", "get_action_broker_status"),
        ("pi status", "get_butters_host_status"),
        ("dependency health", "get_network_service_health"),
    ],
)
def test_other_status_questions_are_not_stolen_by_the_nas_matcher(
    router: IntentRouter, phrase: str, skill: str
) -> None:
    route = router.route(phrase)

    assert route.status == "matched", route.message
    assert route.skill == skill


def test_a_sensor_question_still_asks_which_sensor(router: IntentRouter) -> None:
    route = router.route("what is the humidity sensor status")

    assert route.status == "clarification"
    assert "Which sensor" in (route.message or "")


@pytest.mark.parametrize(
    ("phrase", "skill"),
    [
        ("wake the nas", "wake_nas"),
        ("wake my nas", "wake_nas"),
        ("turn on my nas", "wake_nas"),
    ],
)
def test_naming_the_nas_in_an_action_still_acts(
    router: IntentRouter, phrase: str, skill: str
) -> None:
    """Reporting must not swallow acting. "up" and "down" are deliberately
    not operational words for this matcher, because "shut down the nas" and
    "wake up the nas" contain them."""

    route = router.route(phrase)

    assert route.status == "matched"
    assert route.skill == skill


@pytest.mark.parametrize("phrase", ["shut down the nas", "shutdown nas", "open jellyfin"])
def test_naming_the_nas_without_asking_after_it_is_not_a_status_request(
    router: IntentRouter, phrase: str
) -> None:
    route = router.route(phrase)

    assert route.skill != "get_nas_status"
