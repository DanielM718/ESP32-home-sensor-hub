"""Small allow-listed tool catalog exposed to a routing-only model."""

from __future__ import annotations

from butters.llm.model import ToolDefinition
from butters.routing.entities import EntityRegistry, MetricRegistry


def build_tool_catalog(
    entities: EntityRegistry, metrics: MetricRegistry
) -> tuple[ToolDefinition, ...]:
    entity_ids = [
        entity.entity_id
        for entity in entities.entities
        if entity.sensor_type != "printer"
    ]
    metric_ids = [metric.metric_id for metric in metrics.metrics]
    air_entities = [
        entity.entity_id
        for entity in entities.entities
        if entity.sensor_type == "air_quality"
    ]
    printer_entities = [
        entity.entity_id
        for entity in entities.entities
        if entity.sensor_type == "printer"
    ]
    return (
        ToolDefinition(
            "get_sensor_value",
            "Read one current measurement from one configured sensor.",
            _object_schema(
                {
                    "entity": _enum_string(entity_ids),
                    "metric": _enum_string(metric_ids),
                },
                ("entity", "metric"),
            ),
        ),
        ToolDefinition(
            "get_sensor_status",
            "Read reporting status for one sensor, or all sensors when entity is null.",
            _object_schema(
                {
                    "entity": {
                        "type": ["string", "null"],
                        "enum": [*entity_ids, None],
                    }
                }
            ),
        ),
        ToolDefinition(
            "get_sensor_last_seen",
            "Read when one configured sensor last reported.",
            _object_schema(
                {"entity": _enum_string(entity_ids)},
                ("entity",),
            ),
        ),
        ToolDefinition(
            "compare_sensor_metric",
            "Compare current humidity across configured filament boxes.",
            _object_schema(
                {
                    "group": {"type": "string", "enum": ["filament_boxes"]},
                    "metric": {"type": "string", "enum": ["humidity"]},
                    "operation": {"type": "string", "enum": ["max"]},
                },
                ("group", "metric", "operation"),
            ),
        ),
        ToolDefinition(
            "get_room_air_quality",
            "Read a concise multi-metric air-quality snapshot for one room.",
            _object_schema(
                {"entity": _enum_string(air_entities)},
                ("entity",),
            ),
        ),
        ToolDefinition(
            "get_server_health",
            "Read Raspberry Pi resources and fixed allow-listed service status.",
            _object_schema({}),
        ),
        *(
            ToolDefinition(
                name,
                description,
                _object_schema(
                    {"entity": _enum_string(printer_entities)},
                    ("entity",),
                ),
            )
            for name, description in (
                ("get_printer_status", "Read state for one configured printer."),
                (
                    "get_current_print",
                    "Read the active print job, progress, layer, remaining time, and material.",
                ),
                (
                    "get_printer_temperatures",
                    "Read observed nozzle, bed, and chamber temperatures.",
                ),
                (
                    "get_print_environment_summary",
                    "Read an observational SEN66 summary for the latest print session.",
                ),
                (
                    "get_printer_usage",
                    "Read local print hours/counts and clearly separated upstream usage provenance.",
                ),
                (
                    "get_printer_maintenance",
                    "Read local maintenance reminders and completion history.",
                ),
                (
                    "get_last_print",
                    "Read metadata and duration for the latest print-history item.",
                ),
            )
        ),
        ToolDefinition(
            "clarify_request",
            "Use when a sensor, filament box, metric, or request is ambiguous.",
            _object_schema(
                {
                    "topic": {
                        "type": "string",
                        "enum": [
                            "sensor",
                            "filament_box",
                            "printer",
                            "metric",
                            "request",
                        ],
                    }
                },
                ("topic",),
            ),
            executable=False,
        ),
        ToolDefinition(
            "unsupported_request",
            "Use for control, write, shell, unrelated, nonsensical, or unsafe requests.",
            _object_schema({}),
            executable=False,
        ),
    )


def entity_alias_summary(entities: EntityRegistry) -> tuple[str, ...]:
    """Compact non-secret entity hints for the routing prompt."""
    return tuple(
        f"{entity.entity_id}: {', '.join(entity.aliases)}"
        for entity in entities.entities
    )


def metric_alias_summary(metrics: MetricRegistry) -> tuple[str, ...]:
    return tuple(
        f"{metric.metric_id}: {', '.join(metric.aliases)}" for metric in metrics.metrics
    )


def _object_schema(
    properties: dict[str, object], required: tuple[str, ...] = ()
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _enum_string(values: list[str]) -> dict[str, object]:
    return {"type": "string", "enum": values}
