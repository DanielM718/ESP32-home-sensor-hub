"""Explicit entity and metric allow-lists with unambiguous alias resolution."""

from __future__ import annotations

from dataclasses import dataclass

from butters.assistant_config import EntitySettings
from butters.config import ConfigError
from butters.routing.fuzzy import (
    FUZZY_MARGIN,
    FuzzyMatch,
    best_by_key,
    fuzzy_matches,
    token_spans,
)
from butters.routing.normalization import (
    contains_phrase,
    normalize_request,
)


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
    confidence: float = 0.0
    fuzzy: bool = False


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
        exact_spans: list[tuple[int, int]] = []
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
        if candidates:
            # A generic one-token alias can be wholly contained in a strongly
            # matching longer registered phrase (``printer rom`` contains the
            # exact printer alias but is one edit from ``printer room``).  The
            # full phrase is more specific; this exception never overrides an
            # exact phrase of the same or greater token width.
            for entity in candidates:
                for alias in entity.aliases:
                    if len(normalize_request(alias).split()) == best:
                        exact_spans.extend(token_spans(normalized_text, alias))
            vocabulary = tuple(
                (entity.entity_id, entity.aliases, order)
                for order, entity in enumerate(self._entities)
            )
            longer = tuple(
                item
                for item in best_by_key(fuzzy_matches(normalized_text, vocabulary))
                if item.key not in {candidate.entity_id for candidate in candidates}
                and item.end - item.start > best
                and item.score >= 0.90
                and any(
                    item.start <= exact_start and item.end >= exact_end
                    for exact_start, exact_end in exact_spans
                )
            )
            if longer:
                top = longer[0]
                close = tuple(
                    item for item in longer if top.score - item.score < FUZZY_MARGIN
                )
                if len(close) > 1:
                    ambiguous = tuple(self._by_id[item.key] for item in close)
                    return EntityResolution(
                        None, ambiguous, confidence=top.score, fuzzy=True
                    )
                entity = self._by_id[top.key]
                return EntityResolution(entity, (entity,), top.score, True)
            return EntityResolution(
                entity=candidates[0] if len(candidates) == 1 else None,
                candidates=candidates,
                confidence=1.0,
            )

        vocabulary = tuple(
            (entity.entity_id, entity.aliases, order)
            for order, entity in enumerate(self._entities)
        )
        ranked = best_by_key(fuzzy_matches(normalized_text, vocabulary))
        if not ranked:
            return EntityResolution(None)
        top = ranked[0]
        close = tuple(item for item in ranked if top.score - item.score < FUZZY_MARGIN)
        if len(close) > 1:
            ambiguous = tuple(self._by_id[item.key] for item in close)
            return EntityResolution(
                None,
                ambiguous,
                confidence=top.score,
                fuzzy=True,
            )
        entity = self._by_id[top.key]
        return EntityResolution(entity, (entity,), top.score, True)


@dataclass(frozen=True, slots=True)
class Metric:
    metric_id: str
    display_name: str
    field: str
    unit: str
    sensor_types: frozenset[str]
    aliases: tuple[str, ...]
    scale: float = 1.0
    aggregate: bool = True


@dataclass(frozen=True, slots=True)
class MetricResolution:
    metrics: tuple[Metric, ...]
    candidates: tuple[Metric, ...] = ()
    confidence: float = 0.0
    fuzzy: bool = False


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

    def supported_for(self, sensor_type: str) -> tuple[Metric, ...]:
        """Return capability metadata in stable registry order."""

        return tuple(
            metric
            for metric in self._metrics
            if sensor_type in metric.sensor_types and metric.aggregate
        )

    def resolve(self, normalized_text: str) -> tuple[Metric, ...]:
        """Return every distinct requested metric, in the order it was asked for.

        A request may name more than one compatible measurement, so this reports
        the full requested set rather than a single winner. Ordering follows the
        earliest alias match, with the registry order breaking ties, so a reply
        can be phrased in the order the caller used.
        """

        return self.resolve_details(normalized_text).metrics

    def resolve_details(self, normalized_text: str) -> MetricResolution:
        """Resolve exact and bounded-fuzzy metric aliases with tie reporting."""

        matched: list[tuple[int, int, Metric]] = []
        occupied: list[tuple[int, int]] = []
        for order, metric in enumerate(self._metrics):
            positions = [
                span[0]
                for alias in metric.aliases
                for span in token_spans(normalized_text, alias)
            ]
            if positions:
                matched.append((min(positions), order, metric))
                for alias in metric.aliases:
                    occupied.extend(token_spans(normalized_text, alias))

        exact_metrics = tuple(metric for _position, _order, metric in sorted(matched))
        exact_ids = frozenset(metric.metric_id for metric in exact_metrics)
        vocabulary = tuple(
            (metric.metric_id, metric.aliases, order)
            for order, metric in enumerate(self._metrics)
        )
        fuzzy = fuzzy_matches(
            normalized_text,
            vocabulary,
            excluded_keys=exact_ids,
            occupied_spans=tuple(occupied),
        )
        by_span: dict[tuple[int, int], list[FuzzyMatch]] = {}
        for item in fuzzy:
            by_span.setdefault((item.start, item.end), []).append(item)

        accepted = []
        ambiguous_ids: set[str] = set()
        for span in sorted(by_span):
            ranked = best_by_key(tuple(by_span[span]))
            if not ranked:
                continue
            if len(ranked) > 1 and ranked[0].score - ranked[1].score < FUZZY_MARGIN:
                ambiguous_ids.update(
                    item.key
                    for item in ranked
                    if ranked[0].score - item.score < FUZZY_MARGIN
                )
                continue
            accepted.append(ranked[0])

        earliest: dict[str, FuzzyMatch] = {}
        for item in accepted:
            previous = earliest.get(item.key)
            if previous is None or (item.start, -item.score, item.order) < (
                previous.start,
                -previous.score,
                previous.order,
            ):
                earliest[item.key] = item
        fuzzy_metrics = tuple(
            self._by_id[item.key]
            for item in sorted(
                earliest.values(), key=lambda value: (value.start, value.order)
            )
        )
        combined = tuple(dict.fromkeys((*exact_metrics, *fuzzy_metrics)))
        # Preserve phrase order across exact and fuzzy matches.
        positions: dict[str, tuple[int, int]] = {
            metric.metric_id: (position, order) for position, order, metric in matched
        }
        for item in earliest.values():
            positions[item.key] = (item.start, item.order)
        combined = tuple(
            sorted(combined, key=lambda metric: positions[metric.metric_id])
        )
        candidates = tuple(
            metric for metric in self._metrics if metric.metric_id in ambiguous_ids
        )
        confidences = [item.score for item in earliest.values()]
        return MetricResolution(
            combined,
            candidates,
            min(confidences, default=1.0 if exact_metrics else 0.0),
            bool(earliest),
        )


DEFAULT_METRICS = (
    Metric(
        "temperature",
        "temperature",
        "temperature_c",
        "°C",
        frozenset({"environment", "air_quality"}),
        ("temperature", "temperatures", "temp", "how warm", "how hot", "degrees"),
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
        "pm1",
        "PM1",
        "pm1",
        "µg/m³",
        frozenset({"air_quality"}),
        ("pm1", "pm 1"),
        aggregate=False,
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
        "pm4",
        "PM4",
        "pm4",
        "µg/m³",
        frozenset({"air_quality"}),
        ("pm4", "pm 4"),
        aggregate=False,
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
