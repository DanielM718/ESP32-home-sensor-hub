from __future__ import annotations

from butters.assistant_config import load_assistant_settings
from butters.integrations.model import (
    SensorRecord,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.skills.implementations import build_read_only_registry
from butters.skills.model import (
    ActionClass,
    ComparisonResult,
    SensorStatusResult,
    SensorValueResult,
    SensorValuesResult,
)


class SnapshotProvider:
    def __init__(self, records: tuple[SensorRecord, ...]) -> None:
        self.value = SensorSnapshot("2026-08-11T12:00:00Z", records)
        self.calls = 0

    def snapshot(self) -> SensorSnapshot:
        self.calls += 1
        return self.value


class HealthProvider:
    def snapshot(self) -> ServerHealthSnapshot:
        return ServerHealthSnapshot(
            1000,
            0.2,
            0.3,
            0.4,
            2_000_000_000,
            10_000_000,
            20_000_000_000,
            30_000_000_000,
            55.0,
            "0x0",
            (),
        )


def _record(
    sensor_type: str,
    source_id: str,
    *,
    status: str = "online",
    age: int = 10,
    **values: object,
) -> SensorRecord:
    return SensorRecord(
        sensor_type,
        source_id,
        "2026-08-11T11:59:50Z",
        age,
        status,
        dict(values),
        tuple(values),
    )


def _registry(records: tuple[SensorRecord, ...]):
    settings = load_assistant_settings()
    entities = EntityRegistry(settings.entities)
    return build_read_only_registry(
        SnapshotProvider(records), HealthProvider(), entities, MetricRegistry()
    )


def test_sensor_value_is_structured_and_battery_is_scaled() -> None:
    registry = _registry(
        (_record("environment", "1", battery_mv=3442, battery_measurement_ok=True),)
    )

    execution = registry.execute(
        "get_sensor_value", {"entity": "filament_box_1", "metric": "battery_voltage"}
    )

    assert execution.ok
    assert isinstance(execution.result, SensorValueResult)
    assert execution.result.value == 3.442
    assert execution.result.unit == "V"


def test_stale_value_is_not_reported_as_current() -> None:
    registry = _registry(
        (_record("environment", "1", status="stale", age=4000, humidity=19.0),)
    )

    execution = registry.execute(
        "get_sensor_value", {"entity": "filament_box_1", "metric": "humidity"}
    )

    assert execution.ok
    assert isinstance(execution.result, SensorValueResult)
    assert not execution.result.available
    assert execution.result.value is None
    assert execution.result.reason == "sensor is stale"


def test_invalid_battery_flag_returns_unavailable() -> None:
    registry = _registry(
        (_record("environment", "2", battery_mv=3442, battery_measurement_ok=False),)
    )

    execution = registry.execute(
        "get_sensor_value", {"entity": "filament_box_2", "metric": "battery_voltage"}
    )

    assert execution.ok
    assert isinstance(execution.result, SensorValueResult)
    assert (
        execution.result.reason
        == "battery measurement is not valid in the latest packet"
    )


def test_comparison_ignores_missing_and_stale_values() -> None:
    registry = _registry(
        (
            _record("environment", "1", humidity=18.0),
            _record("environment", "2", status="stale", humidity=99.0),
            _record("environment", "3", humidity=27.0),
        )
    )

    execution = registry.execute(
        "compare_sensor_metric",
        {"group": "filament_boxes", "metric": "humidity", "operation": "max"},
    )

    assert execution.ok
    assert isinstance(execution.result, ComparisonResult)
    assert execution.result.entity == "filament_box_3"
    assert execution.result.value == 27.0
    assert execution.result.considered_count == 2
    assert [item.entity for item in execution.result.missing] == ["filament_box_2"]


def test_all_sensor_status_represents_missing_sources_as_offline() -> None:
    registry = _registry((_record("environment", "1", humidity=18.0),))

    execution = registry.execute("get_sensor_status", {"entity": None})

    assert execution.ok
    assert isinstance(execution.result, SensorStatusResult)
    assert not execution.result.all_reporting
    assert execution.result.reporting_count == 1
    assert execution.result.configured_count == 4
    assert [item.status for item in execution.result.entities] == [
        "offline",
        "online",
        "offline",
        "offline",
    ]


def test_single_status_is_structured() -> None:
    registry = _registry((_record("environment", "1", humidity=18.0),))

    execution = registry.execute("get_sensor_status", {"entity": "filament_box_1"})

    assert execution.ok
    assert isinstance(execution.result, SensorStatusResult)
    assert execution.result.all_reporting


def test_several_measurements_come_from_one_snapshot() -> None:
    provider = SnapshotProvider(
        (_record("environment", "3", temperature_c=24.2, humidity=18.4),)
    )
    settings = load_assistant_settings()
    registry = build_read_only_registry(
        provider, HealthProvider(), EntityRegistry(settings.entities), MetricRegistry()
    )

    execution = registry.execute(
        "get_sensor_values",
        {"entity": "filament_box_3", "metrics": ["temperature", "humidity"]},
    )

    assert execution.ok
    assert isinstance(execution.result, SensorValuesResult)
    assert [item.metric for item in execution.result.measurements] == [
        "temperature",
        "humidity",
    ]
    assert [item.value for item in execution.result.measurements] == [24.2, 18.4]
    assert all(item.available for item in execution.result.measurements)
    assert provider.calls == 1


def test_a_missing_measurement_is_reported_rather_than_invented() -> None:
    registry = _registry((_record("environment", "3", temperature_c=24.2),))

    execution = registry.execute(
        "get_sensor_values",
        {"entity": "filament_box_3", "metrics": ["temperature", "humidity"]},
    )

    assert execution.ok
    assert isinstance(execution.result, SensorValuesResult)
    temperature, humidity = execution.result.measurements
    assert temperature.available and temperature.value == 24.2
    assert not humidity.available
    assert humidity.value is None
    assert humidity.reason == "measurement is unavailable"


def test_multi_measurement_policy_denies_disallowed_entities_and_metrics() -> None:
    registry = _registry((_record("environment", "3", temperature_c=24.2),))

    invalid_entity = registry.execute(
        "get_sensor_values", {"entity": "secret_sensor", "metrics": ["temperature"]}
    )
    invalid_metric = registry.execute(
        "get_sensor_values",
        {"entity": "filament_box_3", "metrics": ["temperature", "password"]},
    )
    wrong_sensor_type = registry.execute(
        "get_sensor_values",
        {"entity": "filament_box_3", "metrics": ["temperature", "co2"]},
    )
    empty = registry.execute(
        "get_sensor_values", {"entity": "filament_box_3", "metrics": []}
    )
    not_a_list = registry.execute(
        "get_sensor_values", {"entity": "filament_box_3", "metrics": "temperature"}
    )

    assert invalid_entity.failure and invalid_entity.failure.code == "policy_denied"
    assert invalid_metric.failure and invalid_metric.failure.code == "policy_denied"
    assert wrong_sensor_type.failure
    assert wrong_sensor_type.failure.code == "policy_denied"
    assert empty.failure and empty.failure.code == "invalid_arguments"
    assert not_a_list.failure and not_a_list.failure.code == "invalid_arguments"


def test_multi_measurement_arguments_are_bounded_and_deduplicated() -> None:
    provider = SnapshotProvider(
        (_record("environment", "3", temperature_c=24.2, humidity=18.4),)
    )
    settings = load_assistant_settings()
    registry = build_read_only_registry(
        provider, HealthProvider(), EntityRegistry(settings.entities), MetricRegistry()
    )

    one = registry.execute(
        "get_sensor_values",
        {"entity": "filament_box_3", "metrics": ["humidity"]},
    )
    eight_duplicates = registry.execute(
        "get_sensor_values",
        {"entity": "filament_box_3", "metrics": ["humidity"] * 8},
    )
    ordered_duplicates = registry.execute(
        "get_sensor_values",
        {
            "entity": "filament_box_3",
            "metrics": ["humidity", "temperature", "humidity"],
        },
    )
    nine = registry.execute(
        "get_sensor_values",
        {"entity": "filament_box_3", "metrics": ["humidity"] * 9},
    )
    non_string = registry.execute(
        "get_sensor_values",
        {"entity": "filament_box_3", "metrics": ["humidity", 7]},
    )
    malformed_entity = registry.execute(
        "get_sensor_values", {"entity": "  ", "metrics": ["humidity"]}
    )
    unexpected = registry.execute(
        "get_sensor_values",
        {
            "entity": "filament_box_3",
            "metrics": ["humidity"],
            "command": "write",
        },
    )

    assert one.ok and isinstance(one.result, SensorValuesResult)
    assert [item.metric for item in one.result.measurements] == ["humidity"]
    assert eight_duplicates.ok and isinstance(
        eight_duplicates.result, SensorValuesResult
    )
    assert [item.metric for item in eight_duplicates.result.measurements] == [
        "humidity"
    ]
    assert ordered_duplicates.ok and isinstance(
        ordered_duplicates.result, SensorValuesResult
    )
    assert [item.metric for item in ordered_duplicates.result.measurements] == [
        "humidity",
        "temperature",
    ]
    assert nine.failure and nine.failure.code == "invalid_arguments"
    assert non_string.failure and non_string.failure.code == "invalid_arguments"
    assert malformed_entity.failure
    assert malformed_entity.failure.code == "invalid_arguments"
    assert unexpected.failure and unexpected.failure.code == "invalid_arguments"
    assert provider.calls == 3


def test_policy_denies_unknown_entity_metric_and_skill() -> None:
    registry = _registry((_record("environment", "1", humidity=18.0),))

    invalid_entity = registry.execute(
        "get_sensor_value", {"entity": "secret_sensor", "metric": "humidity"}
    )
    invalid_metric = registry.execute(
        "get_sensor_value", {"entity": "filament_box_1", "metric": "password"}
    )
    unknown = registry.execute("run_shell", {"command": "id"})

    assert invalid_entity.failure and invalid_entity.failure.code == "policy_denied"
    assert invalid_metric.failure and invalid_metric.failure.code == "policy_denied"
    assert unknown.failure and unknown.failure.code == "unknown_skill"


def test_argument_parser_rejects_missing_and_unexpected_values() -> None:
    registry = _registry((_record("environment", "1", humidity=18.0),))

    missing = registry.execute("get_sensor_value", {"entity": "filament_box_1"})
    unexpected = registry.execute(
        "get_sensor_value",
        {"entity": "filament_box_1", "metric": "humidity", "command": "write"},
    )

    assert missing.failure and missing.failure.code == "invalid_arguments"
    assert unexpected.failure and unexpected.failure.code == "invalid_arguments"


def test_every_registered_skill_is_read_only() -> None:
    registry = _registry((_record("environment", "1", humidity=18.0),))

    assert registry.skills
    assert {spec.action_class for spec in registry.skills} == {ActionClass.READ_ONLY}


def test_proposal_validation_applies_policy_without_querying_adapter() -> None:
    provider = SnapshotProvider((_record("environment", "1", humidity=18.0),))
    settings = load_assistant_settings()
    registry = build_read_only_registry(
        provider,
        HealthProvider(),
        EntityRegistry(settings.entities),
        MetricRegistry(),
    )

    valid = registry.validate_proposal(
        "get_sensor_value", {"entity": "filament_box_1", "metric": "humidity"}
    )
    denied = registry.validate_proposal(
        "get_sensor_value", {"entity": "secret", "metric": "humidity"}
    )
    unknown = registry.validate_proposal("run_shell", {"command": "id"})

    assert valid is None
    assert provider.calls == 0
    assert denied is not None and denied.code == "policy_denied"
    assert unknown is not None and unknown.code == "unknown_skill"
