"""Explicit entity and metric allow-lists with unambiguous alias resolution."""

from __future__ import annotations

from dataclasses import dataclass

from butters.assistant_config import EntitySettings
from butters.config import ConfigError
from butters.routing.normalization import contains_phrase, normalize_request


@dataclass(frozen=True, slots=True)
class Entity:
    entity_id: str
    display_name: str
    sensor_type: str
    source_id: str
    aliases: tuple[str, ...]
    groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntityResolution:
    entity: Entity | None
    candidates: tuple[Entity, ...] = ()


class EntityRegistry:
    def __init__(self, settings: tuple[EntitySettings, ...]) -> None:
        self._entities = tuple(
            Entity(
                entity_id=item.entity_id,
                display_name=item.display_name,
                sensor_type=item.sensor_type,
                source_id=item.source_id,
                aliases=tuple(
                    dict.fromkeys((item.entity_id, item.display_name, *item.aliases))
                ),
                groups=item.groups,
            )
            for item in settings
        )
        self._by_id = {entity.entity_id: entity for entity in self._entities}
        alias_owners: dict[str, str] = {}
        for entity in self._entities:
            for alias in entity.aliases:
                normalized = normalize_request(alias)
                owner = alias_owners.setdefault(normalized, entity.entity_id)
                if owner != entity.entity_id:
                    raise ConfigError(
                        f"entity alias {alias!r} maps to both {owner} and "
                        f"{entity.entity_id}"
                    )

    @property
    def entities(self) -> tuple[Entity, ...]:
        return self._entities

    def get(self, entity_id: str) -> Entity | None:
        return self._by_id.get(entity_id)

    def require(self, entity_id: str) -> Entity:
        entity = self.get(entity_id)
        if entity is None:
            raise ValueError(f"unsupported entity: {entity_id}")
        return entity

    def in_group(self, group: str) -> tuple[Entity, ...]:
        return tuple(entity for entity in self._entities if group in entity.groups)

    def resolve(self, normalized_text: str) -> EntityResolution:
        matched: dict[str, tuple[Entity, int]] = {}
        for entity in self._entities:
            scores = [
                len(normalize_request(alias).split())
                for alias in entity.aliases
                if contains_phrase(normalized_text, alias)
            ]
            if scores:
                matched[entity.entity_id] = (entity, max(scores))
        best = max((score for _, score in matched.values()), default=0)
        candidates = tuple(
            entity for entity, score in matched.values() if score == best
        )
        return EntityResolution(
            entity=candidates[0] if len(candidates) == 1 else None,
            candidates=candidates,
        )


@dataclass(frozen=True, slots=True)
class Metric:
    metric_id: str
    display_name: str
    field: str
    unit: str
    sensor_types: frozenset[str]
    aliases: tuple[str, ...]
    scale: float = 1.0


class MetricRegistry:
    def __init__(self, metrics: tuple[Metric, ...] | None = None) -> None:
        self._metrics = metrics or DEFAULT_METRICS
        self._by_id = {metric.metric_id: metric for metric in self._metrics}

    @property
    def metrics(self) -> tuple[Metric, ...]:
        return self._metrics

    def get(self, metric_id: str) -> Metric | None:
        return self._by_id.get(metric_id)

    def require(self, metric_id: str) -> Metric:
        metric = self.get(metric_id)
        if metric is None:
            raise ValueError(f"unsupported metric: {metric_id}")
        return metric

    def resolve(self, normalized_text: str) -> tuple[Metric, ...]:
        matched = []
        for metric in self._metrics:
            if any(contains_phrase(normalized_text, alias) for alias in metric.aliases):
                matched.append(metric)
        return tuple(matched)


DEFAULT_METRICS = (
    Metric(
        "temperature",
        "temperature",
        "temperature_c",
        "°C",
        frozenset({"environment", "air_quality"}),
        ("temperature", "temp", "how warm", "how hot", "degrees"),
    ),
    Metric(
        "humidity",
        "humidity",
        "humidity",
        "%",
        frozenset({"environment", "air_quality"}),
        ("humidity", "humid", "relative humidity"),
    ),
    Metric(
        "battery_voltage",
        "battery voltage",
        "battery_mv",
        "V",
        frozenset({"environment"}),
        ("battery voltage", "battery level", "battery"),
        scale=0.001,
    ),
    Metric(
        "co2",
        "CO2",
        "co2",
        "ppm",
        frozenset({"air_quality"}),
        ("co2", "carbon dioxide", "carbon dioxide reading"),
    ),
    Metric(
        "pm25",
        "PM2.5",
        "pm25",
        "µg/m³",
        frozenset({"air_quality"}),
        ("pm2.5", "pm 2.5", "fine particles", "fine particulate"),
    ),
    Metric(
        "pm10",
        "PM10",
        "pm10",
        "µg/m³",
        frozenset({"air_quality"}),
        ("pm10", "pm 10"),
    ),
    Metric(
        "voc_index",
        "VOC index",
        "voc_index",
        "index",
        frozenset({"air_quality"}),
        ("voc index", "voc", "volatile organic compound index"),
    ),
    Metric(
        "nox_index",
        "NOx index",
        "nox_index",
        "index",
        frozenset({"air_quality"}),
        ("nox index", "nox", "nitrogen oxide index"),
    ),
)
