"""Concept-based deterministic router for supported read-only requests."""

from __future__ import annotations

import re

from butters.routing.entities import Entity, EntityRegistry, Metric, MetricRegistry
from butters.routing.model import RoutedIntent
from butters.routing.normalization import contains_phrase, normalize_request

CONTROL_START = re.compile(
    r"^(?:please )?(?:turn|switch|set|start|stop|restart|reboot|shutdown|"
    r"shut down|wake|enable|disable|open|close)\b"
)


class IntentRouter:
    def __init__(
        self,
        entities: EntityRegistry,
        metrics: MetricRegistry,
    ) -> None:
        self.entities = entities
        self.metrics = metrics

    def route(self, text: str) -> RoutedIntent:
        normalized = normalize_request(text)
        if not normalized:
            return self._unsupported(normalized, "I didn't hear a request.")
        if CONTROL_START.search(normalized) or re.search(
            r"\bturn\b.+\b(?:on|off)\b", normalized
        ):
            return self._unsupported(
                normalized,
                "Control requests are disabled; Butters currently supports read-only questions.",
            )

        if self._server_health_request(normalized):
            return self._matched(normalized, "get_server_health", {}, 0.99)

        if self._all_sensor_status_request(normalized):
            return self._matched(
                normalized,
                "get_sensor_status",
                {"entity": None},
                0.98,
            )

        if self._comparison_request(normalized):
            return self._matched(
                normalized,
                "compare_sensor_metric",
                {"group": "filament_boxes", "metric": "humidity", "operation": "max"},
                0.98,
            )

        resolution = self.entities.resolve(normalized)
        if len(resolution.candidates) > 1:
            names = ", ".join(entity.display_name for entity in resolution.candidates)
            return self._clarification(
                normalized, f"Which sensor did you mean: {names}?"
            )
        entity = resolution.entity

        if self._last_seen_request(normalized):
            entity_or_result = self._require_entity(normalized, entity)
            if isinstance(entity_or_result, RoutedIntent):
                return entity_or_result
            return self._matched(
                normalized,
                "get_sensor_last_seen",
                {"entity": entity_or_result.entity_id},
                0.98,
            )

        if self._entity_status_request(normalized):
            entity_or_result = self._require_entity(normalized, entity)
            if isinstance(entity_or_result, RoutedIntent):
                return entity_or_result
            return self._matched(
                normalized,
                "get_sensor_status",
                {"entity": entity_or_result.entity_id},
                0.96,
            )

        if self._air_quality_request(normalized):
            air_entity = entity or self._single_entity_for_type("air_quality")
            if air_entity is None or air_entity.sensor_type != "air_quality":
                return self._clarification(normalized, "Which room did you mean?")
            return self._matched(
                normalized,
                "get_room_air_quality",
                {"entity": air_entity.entity_id},
                0.97 if entity else 0.91,
            )

        matched_metrics = self.metrics.resolve(normalized)
        if not matched_metrics:
            return self._unsupported(
                normalized,
                "That request is not supported by the read-only deterministic router.",
            )
        metric = self._choose_metric(matched_metrics)
        if metric is None:
            names = ", ".join(item.display_name for item in matched_metrics)
            return self._clarification(
                normalized, f"Which measurement did you mean: {names}?"
            )

        if entity is None:
            compatible = tuple(
                candidate
                for candidate in self.entities.entities
                if candidate.sensor_type in metric.sensor_types
            )
            if len(compatible) == 1:
                entity = compatible[0]
                confidence = 0.91
            elif self._mentions_unnumbered_box(normalized):
                return self._clarification(
                    normalized, "Which filament box did you mean?"
                )
            else:
                return self._clarification(normalized, "Which sensor did you mean?")
        else:
            confidence = 0.98

        if entity.sensor_type not in metric.sensor_types:
            return self._unsupported(
                normalized,
                f"{entity.display_name} does not provide {metric.display_name}.",
            )
        return self._matched(
            normalized,
            "get_sensor_value",
            {"entity": entity.entity_id, "metric": metric.metric_id},
            confidence,
        )

    @staticmethod
    def _server_health_request(text: str) -> bool:
        return any(
            contains_phrase(text, phrase)
            for phrase in (
                "server status",
                "server health",
                "how is the server",
                "pi status",
            )
        )

    @staticmethod
    def _all_sensor_status_request(text: str) -> bool:
        has_sensor = "sensor" in text
        has_all = any(word in text.split() for word in ("all", "every"))
        has_status = any(
            word in text.split() for word in ("reporting", "alive", "online", "status")
        )
        return has_sensor and has_all and has_status

    @staticmethod
    def _comparison_request(text: str) -> bool:
        comparison = any(
            contains_phrase(text, phrase)
            for phrase in (
                "highest humidity",
                "most humid",
                "highest relative humidity",
            )
        )
        group = any(
            word in text.split()
            for word in ("box", "boxes", "container", "containers", "filament")
        )
        return comparison and group

    @staticmethod
    def _last_seen_request(text: str) -> bool:
        return any(
            contains_phrase(text, phrase)
            for phrase in ("last seen", "last report", "last reported", "when was")
        ) and any(word in text.split() for word in ("seen", "report", "reported"))

    @staticmethod
    def _entity_status_request(text: str) -> bool:
        return any(
            word in text.split() for word in ("reporting", "alive", "online", "status")
        )

    @staticmethod
    def _air_quality_request(text: str) -> bool:
        return contains_phrase(text, "air quality")

    @staticmethod
    def _mentions_unnumbered_box(text: str) -> bool:
        words = set(text.split())
        return bool(words & {"box", "container"}) and not bool(
            words & {str(value) for value in range(1, 10)}
        )

    def _single_entity_for_type(self, sensor_type: str) -> Entity | None:
        candidates = tuple(
            entity
            for entity in self.entities.entities
            if entity.sensor_type == sensor_type
        )
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _choose_metric(metrics: tuple[Metric, ...]) -> Metric | None:
        unique = {metric.metric_id: metric for metric in metrics}
        return next(iter(unique.values())) if len(unique) == 1 else None

    def _require_entity(
        self, normalized: str, entity: Entity | None
    ) -> Entity | RoutedIntent:
        if entity is not None:
            return entity
        if self._mentions_unnumbered_box(normalized):
            return self._clarification(normalized, "Which filament box did you mean?")
        return self._clarification(normalized, "Which sensor did you mean?")

    @staticmethod
    def _matched(
        text: str,
        skill: str,
        arguments: dict[str, object],
        confidence: float,
    ) -> RoutedIntent:
        return RoutedIntent("matched", text, skill, arguments, confidence)

    @staticmethod
    def _clarification(text: str, message: str) -> RoutedIntent:
        return RoutedIntent("clarification", text, message=message)

    @staticmethod
    def _unsupported(text: str, message: str) -> RoutedIntent:
        return RoutedIntent("unsupported", text, message=message)
