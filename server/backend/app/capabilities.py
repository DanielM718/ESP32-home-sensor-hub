"""Per-source measurement capability discovery and workflow validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.workflows import (
    FIELDS_BY_SENSOR_TYPE,
    Source,
    WorkflowValidationError,
)


def available_fields_for_entity(entity: Mapping[str, Any]) -> tuple[str, ...]:
    """Return fields actually observed for one latest-data entity."""

    supported = FIELDS_BY_SENSOR_TYPE.get(str(entity.get("sensor_type")), ())
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
        ("printer", latest.get("printer", [])),
        ("ams", latest.get("ams", [])),
    ):
        if not isinstance(collection, list):
            continue
        for entity in collection:
            if not isinstance(entity, Mapping):
                continue
            identity = {
                "environment": entity.get("node_id"),
                "air_quality": entity.get("location"),
                "printer": entity.get("printer_id") or entity.get("id"),
                "ams": entity.get("source_id") or entity.get("id"),
            }[sensor_type]
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
            "selected source is not present in known capability data: "
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
