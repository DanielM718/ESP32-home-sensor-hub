from __future__ import annotations

from butters.responses.formatter import ResponseFormatter
from butters.skills.model import (
    ActionClass,
    ComparisonMissing,
    ComparisonResult,
    SensorValueResult,
    SensorValuesResult,
    SkillExecution,
    SkillFailure,
)


def _measurement(
    metric: str,
    metric_name: str,
    value: float | None,
    unit: str,
    *,
    reason: str | None = None,
) -> SensorValueResult:
    return SensorValueResult(
        "filament_box_3",
        "Filament box three",
        metric,
        metric_name,
        value,
        unit,
        "2026-08-11T12:00:00Z",
        10,
        "online",
        value is not None,
        reason,
    )


def test_sensor_response_uses_concise_spoken_units() -> None:
    result = SensorValueResult(
        "filament_box_3",
        "Filament box three",
        "humidity",
        "humidity",
        18.4,
        "%",
        "2026-08-11T12:00:00Z",
        10,
        "online",
        True,
    )
    execution = SkillExecution("get_sensor_value", ActionClass.READ_ONLY, 0.1, result)

    assert ResponseFormatter().format_execution(execution) == (
        "Filament box three humidity is 18 percent."
    )


def test_several_measurements_are_answered_in_one_response() -> None:
    result = SensorValuesResult(
        "filament_box_3",
        "Filament box three",
        "online",
        (
            _measurement("temperature", "temperature", 24.2, "°C"),
            _measurement("humidity", "humidity", 18.4, "%"),
        ),
    )
    execution = SkillExecution("get_sensor_values", ActionClass.READ_ONLY, 0.1, result)

    assert ResponseFormatter().format_execution(execution) == (
        "Filament box three temperature is 24.2 degrees Celsius "
        "and humidity is 18 percent."
    )


def test_several_measurements_keep_the_requested_order() -> None:
    result = SensorValuesResult(
        "filament_box_3",
        "Filament box three",
        "online",
        (
            _measurement("humidity", "humidity", 18.4, "%"),
            _measurement("temperature", "temperature", 24.2, "°C"),
        ),
    )
    execution = SkillExecution("get_sensor_values", ActionClass.READ_ONLY, 0.1, result)

    assert ResponseFormatter().format_execution(execution) == (
        "Filament box three humidity is 18 percent "
        "and temperature is 24.2 degrees Celsius."
    )


def test_three_measurements_use_stable_list_punctuation_and_units() -> None:
    result = SensorValuesResult(
        "filament_box_3",
        "Filament box three",
        "online",
        (
            _measurement("temperature", "temperature", 24.2, "°C"),
            _measurement("humidity", "humidity", 18.4, "%"),
            _measurement("battery_voltage", "battery voltage", 3.701, "V"),
        ),
    )
    execution = SkillExecution("get_sensor_values", ActionClass.READ_ONLY, 0.1, result)

    assert ResponseFormatter().format_execution(execution) == (
        "Filament box three temperature is 24.2 degrees Celsius, "
        "humidity is 18 percent, and battery voltage is 3.701 volts."
    )


def test_missing_measurement_is_named_rather_than_invented() -> None:
    result = SensorValuesResult(
        "filament_box_3",
        "Filament box three",
        "online",
        (
            _measurement("temperature", "temperature", 24.2, "°C"),
            _measurement(
                "humidity", "humidity", None, "%", reason="measurement is unavailable"
            ),
        ),
    )
    execution = SkillExecution("get_sensor_values", ActionClass.READ_ONLY, 0.1, result)
    text = ResponseFormatter().format_execution(execution)

    assert text == (
        "Filament box three temperature is 24.2 degrees Celsius. "
        "Filament box three humidity is unavailable: measurement is unavailable."
    )
    assert "percent" not in text


def test_all_missing_and_empty_measurement_sets_format_without_fabrication() -> None:
    missing = SensorValuesResult(
        "filament_box_3",
        "Filament box three",
        "online",
        (
            _measurement(
                "temperature",
                "temperature",
                None,
                "°C",
                reason="measurement is unavailable",
            ),
            _measurement(
                "humidity",
                "humidity",
                None,
                "%",
                reason="measurement is unavailable",
            ),
        ),
    )
    empty = SensorValuesResult("filament_box_3", "Filament box three", "online", ())

    missing_text = ResponseFormatter().format_execution(
        SkillExecution("get_sensor_values", ActionClass.READ_ONLY, 0.1, missing)
    )
    empty_text = ResponseFormatter().format_execution(
        SkillExecution("get_sensor_values", ActionClass.READ_ONLY, 0.1, empty)
    )

    assert missing_text == (
        "Filament box three temperature is unavailable: measurement is unavailable. "
        "Filament box three humidity is unavailable: measurement is unavailable."
    )
    assert empty_text == ""


def test_comparison_response_mentions_excluded_data() -> None:
    result = ComparisonResult(
        "filament_boxes",
        "humidity",
        "max",
        "filament_box_2",
        "Filament box two",
        27.0,
        "%",
        "2026-08-11T12:00:00Z",
        10,
        2,
        (ComparisonMissing("filament_box_3", "Filament box three", "stale", "stale"),),
    )
    execution = SkillExecution(
        "compare_sensor_metric", ActionClass.READ_ONLY, 0.1, result
    )

    response = ResponseFormatter().format_execution(execution)

    assert "most humid at 27 percent" in response
    assert "Excluded unavailable data from Filament box three" in response


def test_policy_failure_does_not_echo_sensitive_details() -> None:
    execution = SkillExecution(
        "run_shell",
        None,
        0.0,
        failure=SkillFailure("policy_denied", "command=/secret/path"),
    )

    response = ResponseFormatter().format_execution(execution)

    assert response == "That request is not permitted by the read-only skill policy."
    assert "secret" not in response
