"""Concept-based deterministic router for supported read-only requests."""

from __future__ import annotations

import re

from butters.routing.entities import Entity, EntityRegistry, Metric, MetricRegistry
from butters.routing.model import RoutedIntent
from butters.routing.normalization import contains_phrase, normalize_request

CONTROL_START = re.compile(
    r"^(?:please )?(?:turn|switch|set|start|stop|pause|resume|cancel|restart|"
    r"reboot|shutdown|shut down|wake|enable|disable|open|close|move|home|heat|"
    r"cool|load|unload|upload|delete|print|extrude)\b"
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
                normalized,
                f"Which sensor did you mean: {names}?",
                missing_arguments=("entity",),
            )
        entity = resolution.entity

        if self._print_environment_request(normalized):
            printer_or_result = self._require_printer(normalized, entity)
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_print_environment_summary",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        if self._printer_maintenance_request(normalized):
            printer_or_result = self._require_printer(normalized, entity)
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_printer_maintenance",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        if self._last_print_request(normalized):
            printer_or_result = self._require_printer(normalized, entity)
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_last_print",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        if self._printer_usage_request(normalized):
            printer_or_result = self._require_printer(normalized, entity)
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_printer_usage",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        if entity is not None and entity.sensor_type == "printer":
            if self._printer_temperature_request(normalized):
                skill = "get_printer_temperatures"
            elif self._current_print_request(normalized):
                skill = "get_current_print"
            elif self._printer_status_request(normalized):
                skill = "get_printer_status"
            else:
                return self._unsupported(
                    normalized,
                    "That printer question is not supported by the read-only router.",
                    allow_fallback=True,
                )
            return self._matched(
                normalized,
                skill,
                {"entity": entity.entity_id},
                0.98,
            )

        if self._current_print_request(normalized) or self._printer_temperature_request(
            normalized
        ):
            printer_or_result = self._require_printer(normalized, entity)
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            skill = (
                "get_printer_temperatures"
                if self._printer_temperature_request(normalized)
                else "get_current_print"
            )
            return self._matched(
                normalized,
                skill,
                {"entity": printer_or_result.entity_id},
                0.95,
            )

        if self._last_seen_request(normalized):
            entity_or_result = self._require_entity(
                normalized, entity, skill="get_sensor_last_seen"
            )
            if isinstance(entity_or_result, RoutedIntent):
                return entity_or_result
            return self._matched(
                normalized,
                "get_sensor_last_seen",
                {"entity": entity_or_result.entity_id},
                0.98,
            )

        if self._entity_status_request(normalized):
            entity_or_result = self._require_entity(
                normalized, entity, skill="get_sensor_status"
            )
            if isinstance(entity_or_result, RoutedIntent):
                return entity_or_result
            return self._matched(
                normalized,
                "get_sensor_status",
                {"entity": entity_or_result.entity_id},
                0.96,
            )

        if self._air_quality_request(normalized):
            air_entity = entity
            if air_entity is None or air_entity.sensor_type != "air_quality":
                return self._clarification(
                    normalized,
                    "Which room did you mean?",
                    skill="get_room_air_quality",
                    missing_arguments=("entity",),
                )
            return self._matched(
                normalized,
                "get_room_air_quality",
                {"entity": air_entity.entity_id},
                0.97 if entity else 0.91,
            )

        matched_metrics = self.metrics.resolve(normalized)
        if not matched_metrics:
            if len(normalized.split()) <= 4:
                return self._unsupported(
                    normalized,
                    "I couldn't understand that. Please repeat the full request.",
                )
            return self._unsupported(
                normalized,
                "That request is not supported by the read-only deterministic router.",
                allow_fallback=True,
            )
        metric = self._choose_metric(matched_metrics)
        if metric is None:
            names = ", ".join(item.display_name for item in matched_metrics)
            return self._clarification(
                normalized,
                f"Which measurement did you mean: {names}?",
                skill="get_sensor_value",
                arguments=({"entity": entity.entity_id} if entity is not None else {}),
                missing_arguments=("metric",),
            )

        if entity is None:
            if self._mentions_unnumbered_box(normalized):
                return self._clarification(
                    normalized,
                    "Which filament box did you mean?",
                    skill="get_sensor_value",
                    arguments={"metric": metric.metric_id},
                    missing_arguments=("entity",),
                )
            return self._clarification(
                normalized,
                "Which sensor did you mean?",
                skill="get_sensor_value",
                arguments={"metric": metric.metric_id},
                missing_arguments=("entity",),
            )
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
    def _print_environment_request(text: str) -> bool:
        print_context = any(
            contains_phrase(text, phrase)
            for phrase in (
                "last print",
                "during the print",
                "after the print",
                "print emissions",
            )
        )
        environment = any(
            phrase in text
            for phrase in (
                "pm1",
                "pm2.5",
                "pm 2.5",
                "pm4",
                "pm10",
                "voc",
                "nox",
                "co2",
                "air quality",
                "emission",
                "environment",
                "baseline",
                "recover",
                "temperature",
                "humidity",
            )
        )
        return print_context and environment

    @staticmethod
    def _printer_temperature_request(text: str) -> bool:
        component = any(
            word in text.split()
            for word in ("nozzle", "nozzles", "bed", "chamber", "toolhead")
        )
        return component and any(
            word in text.split()
            for word in ("temperature", "temperatures", "temp", "hot")
        )

    @staticmethod
    def _printer_usage_request(text: str) -> bool:
        printer_context = any(
            word in text.split() for word in ("printer", "x2d", "bambu")
        )
        usage = any(
            contains_phrase(text, phrase)
            for phrase in (
                "how many hours",
                "usage hours",
                "printer run",
                "how many prints",
                "print count",
            )
        )
        return printer_context and usage

    @staticmethod
    def _printer_maintenance_request(text: str) -> bool:
        return any(
            word in text.split()
            for word in ("maintenance", "overdue", "service", "serviced", "lubricate")
        ) and any(
            word in text.split() for word in ("printer", "x2d", "bambu", "maintenance")
        )

    @staticmethod
    def _last_print_request(text: str) -> bool:
        return any(
            contains_phrase(text, phrase)
            for phrase in (
                "last print",
                "previous print",
                "most recent print",
                "how long was the print",
            )
        )

    @staticmethod
    def _current_print_request(text: str) -> bool:
        return any(
            contains_phrase(text, phrase)
            for phrase in (
                "current print",
                "printing now",
                "what is the printer printing",
                "what is x2d printing",
                "what is the x2d printing",
                "how much longer",
                "remaining time",
                "time remaining",
                "what layer",
                "which layer",
                "print progress",
                "current material",
                "what material",
                "which material",
                "current filament",
                "print job",
            )
        )

    @staticmethod
    def _printer_status_request(text: str) -> bool:
        words = set(text.split())
        return bool(
            words
            & {
                "running",
                "doing",
                "state",
                "status",
                "online",
                "offline",
                "printing",
                "idle",
                "paused",
            }
        )

    @staticmethod
    def _mentions_unnumbered_box(text: str) -> bool:
        words = set(text.split())
        return bool(words & {"box", "container"}) and not bool(
            words & {str(value) for value in range(1, 10)}
        )

    @staticmethod
    def _choose_metric(metrics: tuple[Metric, ...]) -> Metric | None:
        unique = {metric.metric_id: metric for metric in metrics}
        return next(iter(unique.values())) if len(unique) == 1 else None

    def _require_entity(
        self,
        normalized: str,
        entity: Entity | None,
        *,
        skill: str,
    ) -> Entity | RoutedIntent:
        if entity is not None:
            return entity
        if self._mentions_unnumbered_box(normalized):
            return self._clarification(
                normalized,
                "Which filament box did you mean?",
                skill=skill,
                missing_arguments=("entity",),
            )
        return self._clarification(
            normalized,
            "Which sensor did you mean?",
            skill=skill,
            missing_arguments=("entity",),
        )

    def _require_printer(
        self, normalized: str, entity: Entity | None
    ) -> Entity | RoutedIntent:
        if entity is not None:
            if entity.sensor_type == "printer":
                return entity
            return self._unsupported(
                normalized, f"{entity.display_name} is not a printer."
            )
        printers = tuple(
            item for item in self.entities.entities if item.sensor_type == "printer"
        )
        if len(printers) == 1:
            return printers[0]
        return self._clarification(
            normalized,
            "Which printer did you mean?",
            missing_arguments=("entity",),
        )

    @staticmethod
    def _matched(
        text: str,
        skill: str,
        arguments: dict[str, object],
        confidence: float,
    ) -> RoutedIntent:
        return RoutedIntent("matched", text, skill, arguments, confidence)

    @staticmethod
    def _clarification(
        text: str,
        message: str,
        *,
        skill: str | None = None,
        arguments: dict[str, object] | None = None,
        missing_arguments: tuple[str, ...] = (),
    ) -> RoutedIntent:
        return RoutedIntent(
            "clarification",
            text,
            skill=skill,
            arguments=arguments or {},
            message=message,
            missing_arguments=missing_arguments,
        )

    @staticmethod
    def _unsupported(
        text: str, message: str, *, allow_fallback: bool = False
    ) -> RoutedIntent:
        return RoutedIntent(
            "unsupported", text, message=message, allow_fallback=allow_fallback
        )
