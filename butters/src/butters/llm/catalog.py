"""Model-visible tool catalog, derived from the authoritative SkillRegistry.

The registry is the only source of truth for what Butters can actually do.
Model visibility is a separate, narrower question, so it is decided here by
explicit policy rather than by a hand-maintained list that silently drifts
away from the registry.

Three rules apply, in order:

1. A capability is categorically ineligible unless its action class is one of
   the non-mutating classes below. ACTION, and anything requiring
   authentication, declaring side effects, or demanding explicit intent or
   confirmation, can never become model-visible by any later edit here.
2. An administrator-audience capability is never model-visible, even though
   it is read-only, because audience is an authorization boundary rather than
   a mutation boundary.
3. Whatever survives must still present a strict, enum-bound schema. Older
   skills carry prose argument hints rather than JSON Schema, so those are
   given an explicit schema shape here; skills that already declare a strict
   schema reuse it unchanged.

Anything eligible that is nonetheless withheld must appear in
DOCUMENTED_EXCLUSIONS with a reason. A parity test fails if a registered safe
skill is neither exposed nor documented, so a new capability cannot quietly
vanish from the model surface.
"""

from __future__ import annotations

from collections.abc import Callable

from butters.llm.model import ToolDefinition
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.skills.model import ActionClass, AuthenticationLevel, SkillAudience
from butters.skills.registry import SkillRegistry, SkillSpec

# Non-mutating classes are the only ones a model may ever see.
MODEL_SAFE_ACTION_CLASSES = frozenset({ActionClass.READ_ONLY, ActionClass.ANALYTICAL})

# A hard ceiling on how much surface a model is offered in one request.
MAX_MODEL_TOOLS = 32

# Eligible under the categorical policy, yet deliberately withheld.
DOCUMENTED_EXCLUSIONS: dict[str, str] = {
    "get_host_observation": (
        "deployment-internal host detail; the conversational surface has no "
        "use for it and it should not be summarised by a model"
    ),
    "get_stack_observation": (
        "deployment-internal service-stack detail; same reasoning as "
        "get_host_observation"
    ),
    "get_sensor_history_summary": (
        "superseded for model use by get_sensor_history and "
        "summarize_sensor_window, which both declare strict window arguments"
    ),
}


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
                    "Read canonical Tracked Print Time, interval completeness, and rolling usage tier.",
                ),
                (
                    "get_printer_maintenance",
                    "Read manufacturer maintenance state, baselines, advisories, and completion history.",
                ),
                (
                    "get_printer_maintenance_events",
                    "Read up to twenty recent maintenance and usage-tier transition events.",
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


def _shape_builders(
    entities: EntityRegistry, metrics: MetricRegistry
) -> dict[str, Callable[[], dict[str, object]]]:
    """Strict schemas for skills that predate JSON Schema in the registry."""

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
    return {
        "none": lambda: _object_schema({}),
        "entity": lambda: _object_schema(
            {"entity": _enum_string(entity_ids)}, ("entity",)
        ),
        "entity_metric": lambda: _object_schema(
            {
                "entity": _enum_string(entity_ids),
                "metric": _enum_string(metric_ids),
            },
            ("entity", "metric"),
        ),
        # The same-entity multi-metric read. Exposing this is what lets one
        # request for several measurements from one sensor stay a single call
        # instead of being split into one call per metric.
        "entity_metrics": lambda: _object_schema(
            {
                "entity": _enum_string(entity_ids),
                "metrics": {
                    "type": "array",
                    "items": _enum_string(metric_ids),
                    "minItems": 1,
                    "maxItems": len(metric_ids) or 1,
                },
            },
            ("entity", "metrics"),
        ),
        "entity_optional": lambda: _object_schema(
            {"entity": {"type": ["string", "null"], "enum": [*entity_ids, None]}}
        ),
        "air_entity": lambda: _object_schema(
            {"entity": _enum_string(air_entities)}, ("entity",)
        ),
        "printer_entity": lambda: _object_schema(
            {"entity": _enum_string(printer_entities)}, ("entity",)
        ),
        "filament_humidity_comparison": lambda: _object_schema(
            {
                "group": {"type": "string", "enum": ["filament_boxes"]},
                "metric": {"type": "string", "enum": ["humidity"]},
                "operation": {"type": "string", "enum": ["max"]},
            },
            ("group", "metric", "operation"),
        ),
    }


# Explicit schema shape for each eligible skill that does not already declare a
# strict JSON Schema. A skill absent from both this map and DOCUMENTED_EXCLUSIONS
# fails the parity test rather than silently disappearing.
MODEL_TOOL_SHAPES: dict[str, str] = {
    "get_sensor_value": "entity_metric",
    "get_sensor_values": "entity_metrics",
    "get_sensor_status": "entity_optional",
    "get_sensor_last_seen": "entity",
    "compare_sensor_metric": "filament_humidity_comparison",
    "get_room_air_quality": "air_entity",
    "get_server_health": "none",
    "get_printer_status": "printer_entity",
    "get_current_print": "printer_entity",
    "get_printer_temperatures": "printer_entity",
    "get_print_environment_summary": "printer_entity",
    "get_printer_usage": "printer_entity",
    "get_printer_maintenance": "printer_entity",
    "get_printer_maintenance_events": "printer_entity",
    "get_last_print": "printer_entity",
}


def model_eligible(spec: SkillSpec) -> tuple[bool, str]:
    """Apply the categorical policy to one registered capability.

    Returns whether the capability may ever be shown to a model, and the
    reason code when it may not. Every rule here is a property the registry
    already declares, so a capability cannot become visible by omission.
    """

    if spec.action_class not in MODEL_SAFE_ACTION_CLASSES:
        return False, "mutating_action_class"
    if spec.audience is not SkillAudience.NORMAL:
        return False, "administrator_audience"
    if spec.authentication is not AuthenticationLevel.NONE:
        return False, "requires_authentication"
    if spec.side_effects != "none":
        return False, "declares_side_effects"
    if spec.explicit_intent_required or spec.confirmation_required:
        return False, "requires_explicit_user_intent"
    if not spec.available or not spec.configured:
        return False, "capability_unavailable"
    return True, ""


def _strict_schema(spec: SkillSpec) -> dict[str, object] | None:
    schema = spec.input_schema
    if (
        isinstance(schema, dict)
        and schema.get("type") == "object"
        and isinstance(schema.get("properties"), dict)
    ):
        strict = dict(schema)
        strict["additionalProperties"] = False
        return strict
    return None


def model_visibility_report(
    registry: SkillRegistry,
    entities: EntityRegistry,
    metrics: MetricRegistry,
) -> dict[str, dict[str, str]]:
    """Explain, per registered skill, why it is or is not model-visible."""

    shapes = _shape_builders(entities, metrics)
    report: dict[str, dict[str, str]] = {}
    for spec in registry.skills:
        eligible, reason = model_eligible(spec)
        if not eligible:
            report[spec.name] = {"visible": "no", "reason": reason}
        elif spec.name in DOCUMENTED_EXCLUSIONS:
            report[spec.name] = {
                "visible": "no",
                "reason": "documented_exclusion",
                "detail": DOCUMENTED_EXCLUSIONS[spec.name],
            }
        elif spec.name in MODEL_TOOL_SHAPES and MODEL_TOOL_SHAPES[spec.name] in shapes:
            report[spec.name] = {"visible": "yes", "reason": "declared_shape"}
        elif _strict_schema(spec) is not None:
            report[spec.name] = {"visible": "yes", "reason": "registry_schema"}
        else:
            # Neither exposed nor documented: the parity test treats this as a
            # failure so a new safe skill cannot silently go missing.
            report[spec.name] = {"visible": "no", "reason": "undeclared"}
    return report


def derive_safe_tool_catalog(
    registry: SkillRegistry,
    entities: EntityRegistry,
    metrics: MetricRegistry,
) -> tuple[ToolDefinition, ...]:
    """Build the model-visible catalog from the registry under the policy above."""

    shapes = _shape_builders(entities, metrics)
    tools: list[ToolDefinition] = []
    for spec in sorted(registry.skills, key=lambda item: item.name):
        eligible, _reason = model_eligible(spec)
        if not eligible or spec.name in DOCUMENTED_EXCLUSIONS:
            continue
        if not registry.is_enabled(spec.name):
            continue
        shape = MODEL_TOOL_SHAPES.get(spec.name)
        if shape is not None and shape in shapes:
            parameters = shapes[shape]()
        else:
            parameters = _strict_schema(spec)
            if parameters is None:
                continue
        tools.append(ToolDefinition(spec.name, spec.description, parameters))
    if len(tools) > MAX_MODEL_TOOLS:
        raise ValueError("model tool catalog exceeded its bound")
    # The two control outcomes are never executed; they let a model say that a
    # request is ambiguous or unsupported instead of inventing a capability.
    return (*tools, *_control_tools())


def _control_tools() -> tuple[ToolDefinition, ...]:
    return (
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
