from __future__ import annotations

from butters.responses.formatter import ResponseFormatter
from butters.skills.model import (
    ActionClass,
    ComparisonMissing,
    ComparisonResult,
    SensorValueResult,
    SkillExecution,
    SkillFailure,
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
