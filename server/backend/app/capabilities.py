"""Per-source measurement capability discovery and workflow validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.workflows import (
    AIR_QUALITY_FIELDS,
    ENVIRONMENT_FIELDS,
    Source,
    WorkflowValidationError,
)


def available_fields_for_entity(entity: Mapping[str, Any]) -> tuple[str, ...]:
    """Return fields actually observed for one latest-data entity."""

    supported = (
        ENVIRONMENT_FIELDS
        if entity.get("sensor_type") == "environment"
        else AIR_QUALITY_FIELDS
        if entity.get("sensor_type") == "air_quality"
        else ()
    )
    advertised = entity.get("available_fields")
    if isinstance(advertised, list):
        return tuple(field for field in supported if field in advertised)
    return tuple(field for field in supported if field in entity)


def capability_map(
    latest: Mapping[str, Any],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Index discovered fields by the same stable keys used by workflows."""

    result: dict[tuple[str, str], tuple[str, ...]] = {}
    for sensor_type, collection in (
        ("environment", latest.get("environment", [])),
        ("air_quality", latest.get("air_quality", [])),
    ):
        if not isinstance(collection, list):
            continue
        for entity in collection:
            if not isinstance(entity, Mapping):
                continue
            identity = (
                entity.get("node_id")
                if sensor_type == "environment"
                else entity.get("location")
            )
            if identity in (None, ""):
                continue
            result[(sensor_type, str(identity))] = available_fields_for_entity(entity)
    return result


def validate_source_capabilities(
    sources: Sequence[Source],
    fields: Sequence[str],
    latest: Mapping[str, Any],
) -> None:
    """Require every requested field to exist on at least one selected source."""

    discovered = capability_map(latest)
    missing_sources = [
        source.source_id for source in sources if source.key not in discovered
    ]
    if missing_sources:
        raise WorkflowValidationError(
            "selected source is not present in current capability data: "
            + ", ".join(missing_sources)
        )

    unavailable = [
        field
        for field in fields
        if not any(field in discovered.get(source.key, ()) for source in sources)
    ]
    if unavailable:
        raise WorkflowValidationError(
            "selected measurement is not available from the selected sources: "
            + ", ".join(unavailable)
        )
