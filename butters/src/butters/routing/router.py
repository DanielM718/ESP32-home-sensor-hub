"""Concept-based deterministic router for supported read-only requests."""

from __future__ import annotations

import re

from butters.routing.entities import Entity, EntityRegistry, Metric, MetricRegistry
from butters.routing.model import PendingClarification, RoutedIntent
from butters.routing.normalization import contains_phrase, normalize_request

CONTROL_START = re.compile(
    r"^(?:please )?(?:turn|switch|set|start|stop|pause|resume|cancel|restart|"
    r"reboot|shutdown|shut down|wake|enable|disable|open|close|move|home|heat|"
    r"cool|load|unload|upload|delete|print|extrude)\b"
)
CONTROL_TOGGLE = re.compile(r"\bturn\b.+\b(?:on|off)\b")


def _distinct_metrics(metrics: tuple[Metric, ...]) -> tuple[Metric, ...]:
    unique: dict[str, Metric] = {}
    for metric in metrics:
        unique.setdefault(metric.metric_id, metric)
    return tuple(unique.values())


def _sensor_value_call(
    entity_id: str | None, metrics: tuple[Metric, ...]
) -> tuple[str, dict[str, object]]:
    """Route one measurement through the single-value skill and several through the set skill."""

    arguments: dict[str, object] = {} if entity_id is None else {"entity": entity_id}
    if len(metrics) == 1:
        return "get_sensor_value", {**arguments, "metric": metrics[0].metric_id}
    return "get_sensor_values", {
        **arguments,
        "metrics": [metric.metric_id for metric in metrics],
    }


def _joined_names(names: tuple[str, ...]) -> str:
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} or {names[-1]}"


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
        environment_action = self._environment_action(normalized)
        if self._desktop_remote_action(normalized) and environment_action is not None:
            environment_skill, environment_arguments = environment_action
            return RoutedIntent(
                "matched",
                normalized,
                "start_remote_desktop_session",
                {"machine": "desktop"},
                confidence=1.0,
                action_plan=(
                    ("start_remote_desktop_session", {"machine": "desktop"}),
                    (environment_skill, environment_arguments),
                ),
            )
        if self._desktop_remote_action(normalized):
            return self._matched(
                normalized,
                "start_remote_desktop_session",
                {"machine": "desktop"},
                1.0,
            )
        desktop_action = self._desktop_action(normalized)
        if desktop_action is not None:
            # "Wake my desktop and tell me when it is reachable" is one request,
            # not two. The wake stays the same authenticated action with the
            # same pending-action and passkey flow; the readiness question is
            # answered by appending the existing read-only observation to the
            # frozen plan, so it runs only after the action the user authorized
            # actually succeeded. No new privilege is composed here: a mutating
            # step is never added by this branch.
            if desktop_action == "wake_desktop" and self._desktop_readiness_follow_up(
                normalized
            ):
                return RoutedIntent(
                    "matched",
                    normalized,
                    "wake_desktop",
                    {"machine": "desktop"},
                    confidence=1.0,
                    action_plan=(
                        ("wake_desktop", {"machine": "desktop"}),
                        ("get_desktop_status", {"machine": "desktop"}),
                    ),
                )
            return self._matched(
                normalized,
                desktop_action,
                {"machine": "desktop"},
                1.0,
            )
        if self._desktop_status_request(normalized):
            return self._matched(
                normalized, "get_desktop_status", {"machine": "desktop"}, 0.99
            )
        if environment_action is not None:
            skill, arguments = environment_action
            return self._matched(normalized, skill, arguments, 1.0)
        fixed_action = self._fixed_action(normalized)
        if fixed_action is not None:
            return self._matched(normalized, fixed_action, {}, 1.0)
        status_skill = self._capability_status_request(normalized)
        if status_skill is not None:
            return self._matched(normalized, status_skill, {}, 0.99)
        print_analysis = self._print_analysis_request(normalized)
        if print_analysis is not None:
            return self._matched(
                normalized, "analyze_print_environment", print_analysis, 0.98
            )
        control = self._control_rejection(normalized)
        if control is not None:
            return control

        if self._server_health_request(normalized):
            return self._matched(normalized, "get_server_health", {}, 0.99)

        host_metric = self._host_observation(normalized)
        if host_metric is not None:
            return self._matched(
                normalized,
                "get_host_observation",
                {"metric": host_metric},
                0.96,
            )

        stack_component = self._stack_observation(normalized)
        if stack_component is not None:
            return self._matched(
                normalized,
                "get_stack_observation",
                {"component": stack_component},
                0.96,
            )

        network_view = self._network_observation(normalized)
        if network_view is not None:
            return self._matched(
                normalized,
                "get_network_observation",
                {"view": network_view},
                0.96,
            )

        project_view = self._project_observation(normalized)
        if project_view is not None:
            return self._matched(
                normalized,
                "get_project_status",
                {"view": project_view},
                0.96,
            )

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
        entity = resolution.entity
        entity_candidates = (
            resolution.candidates if len(resolution.candidates) > 1 else ()
        )

        # "Baseline" is also a sensor-history term, so preserve the explicit
        # maintenance phrase before evaluating history windows.
        if "maintenance" in normalized.split() and contains_phrase(
            normalized, "baseline required"
        ):
            printer_or_result = self._require_printer(
                normalized,
                entity,
                skill="get_printer_maintenance",
                candidates=entity_candidates,
            )
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_printer_maintenance",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        history_range = self._history_range(normalized)
        if history_range is not None:
            entity_or_result = self._require_entity(
                normalized,
                entity,
                skill="get_sensor_history_summary",
                arguments={"range_key": history_range},
                candidates=entity_candidates,
            )
            if isinstance(entity_or_result, RoutedIntent):
                return entity_or_result
            if entity_or_result.sensor_type == "printer":
                return self._unsupported(
                    normalized,
                    "I can report printer status and print details, but not that sensor-history view.",
                )
            return self._matched(
                normalized,
                "get_sensor_history_summary",
                {"entity": entity_or_result.entity_id, "range_key": history_range},
                0.94,
            )

        if self._aggregate_measurement_request(
            normalized, has_registered_entity=entity is not None
        ):
            if entity is None:
                return self._clarification(
                    normalized,
                    self._entity_question(normalized, entity_candidates),
                    skill="get_sensor_values",
                    missing_arguments=("entity",),
                    aggregate=True,
                    ambiguity_candidates=tuple(
                        item.entity_id for item in entity_candidates
                    ),
                )
            if entity.sensor_type == "printer":
                return self._unsupported(
                    normalized,
                    "I can report printer status and temperatures, but not a generic sensor-reading set for the printer.",
                )
            supported = self.metrics.supported_for(entity.sensor_type)
            skill, arguments = _sensor_value_call(entity.entity_id, supported)
            return self._matched(
                normalized,
                skill,
                arguments,
                min(0.98, resolution.confidence or 0.98),
                aggregate=True,
            )

        if self._print_environment_request(normalized):
            printer_or_result = self._require_printer(
                normalized,
                entity,
                skill="get_print_environment_summary",
                candidates=entity_candidates,
            )
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_print_environment_summary",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        if self._printer_maintenance_events_request(normalized):
            printer_or_result = self._require_printer(
                normalized,
                entity,
                skill="get_printer_maintenance_events",
                candidates=entity_candidates,
            )
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_printer_maintenance_events",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        if self._printer_maintenance_request(normalized):
            printer_or_result = self._require_printer(
                normalized,
                entity,
                skill="get_printer_maintenance",
                candidates=entity_candidates,
            )
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_printer_maintenance",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        if self._last_print_request(normalized):
            printer_or_result = self._require_printer(
                normalized,
                entity,
                skill="get_last_print",
                candidates=entity_candidates,
            )
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                "get_last_print",
                {"entity": printer_or_result.entity_id},
                0.98,
            )

        if self._printer_usage_request(normalized):
            printer_or_result = self._require_printer(
                normalized,
                entity,
                skill="get_printer_usage",
                candidates=entity_candidates,
            )
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
                    "I can help with printer status, temperatures, current or recent prints, usage, and maintenance.",
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
            requested_skill = (
                "get_printer_temperatures"
                if self._printer_temperature_request(normalized)
                else "get_current_print"
            )
            printer_or_result = self._require_printer(
                normalized,
                entity,
                skill=requested_skill,
                candidates=entity_candidates,
            )
            if isinstance(printer_or_result, RoutedIntent):
                return printer_or_result
            return self._matched(
                normalized,
                requested_skill,
                {"entity": printer_or_result.entity_id},
                0.95,
            )

        if self._last_seen_request(normalized):
            entity_or_result = self._require_entity(
                normalized,
                entity,
                skill="get_sensor_last_seen",
                candidates=entity_candidates,
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
                normalized,
                entity,
                skill="get_sensor_status",
                candidates=entity_candidates,
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
                    ambiguity_candidates=tuple(
                        item.entity_id for item in entity_candidates
                    ),
                )
            return self._matched(
                normalized,
                "get_room_air_quality",
                {"entity": air_entity.entity_id},
                0.97 if entity else 0.91,
            )

        metric_resolution = self.metrics.resolve_details(normalized)
        if metric_resolution.candidates:
            names = _joined_names(
                tuple(item.display_name for item in metric_resolution.candidates)
            )
            arguments = {} if entity is None else {"entity": entity.entity_id}
            return self._clarification(
                normalized,
                f"Which measurement did you mean: {names}?",
                skill="get_sensor_value",
                arguments=arguments,
                missing_arguments=("metric",),
                ambiguity_candidates=tuple(
                    item.metric_id for item in metric_resolution.candidates
                ),
            )
        matched_metrics = metric_resolution.metrics
        if not matched_metrics:
            if entity is not None and self._measurement_without_metric_request(
                normalized
            ):
                return self._clarification(
                    normalized,
                    "Which measurement did you mean?",
                    skill="get_sensor_value",
                    arguments={"entity": entity.entity_id},
                    missing_arguments=("metric",),
                )
            if len(normalized.split()) <= 4:
                return self._unsupported(
                    normalized,
                    "I didn't understand that request. Try asking about a sensor reading or status.",
                )
            return self._unsupported(
                normalized,
                "I can't answer that type of question with the local skills currently enabled.",
                allow_fallback=True,
            )
        if (
            metric_resolution.fuzzy
            and entity is None
            and not self._sensor_query_context(normalized)
        ):
            return self._unsupported(
                normalized,
                "I can't answer that type of question with the local skills currently enabled.",
                allow_fallback=True,
            )
        # Naming several compatible measurements is a complete request, not an
        # ambiguity: the caller already said which ones they want.
        requested = _distinct_metrics(matched_metrics)

        if entity is None:
            skill, arguments = _sensor_value_call(None, requested)
            return self._clarification(
                normalized,
                self._entity_question(normalized, entity_candidates),
                skill=skill,
                arguments=arguments,
                missing_arguments=("entity",),
                ambiguity_candidates=tuple(
                    item.entity_id for item in entity_candidates
                ),
            )

        unsupported = tuple(
            metric
            for metric in requested
            if entity.sensor_type not in metric.sensor_types
        )
        if unsupported:
            names = _joined_names(tuple(metric.display_name for metric in unsupported))
            return self._unsupported(
                normalized,
                f"{entity.display_name} does not provide {names}.",
            )
        skill, arguments = _sensor_value_call(entity.entity_id, requested)
        confidence = min(
            0.98,
            resolution.confidence or 0.98,
            metric_resolution.confidence or 0.98,
        )
        return self._matched(normalized, skill, arguments, confidence)

    def continue_clarification(
        self,
        pending: PendingClarification,
        reply: str,
    ) -> RoutedIntent:
        """Fill exactly one known slot without joining user utterance strings."""

        normalized = normalize_request(reply)
        # A clarification reply is still a request. Reporting the read-only
        # boundary must not depend on whether a slot happens to be open, or a
        # control phrase would be answered with an unrelated sensor reading.
        control = self._control_rejection(normalized)
        if control is not None:
            return control
        if pending.missing_argument == "entity":
            resolution = self.entities.resolve(normalized)
            entity = resolution.entity
            if entity is None:
                candidates = resolution.candidates
                return self._clarification(
                    normalized,
                    self._entity_question(normalized, candidates),
                    skill=pending.skill,
                    arguments=dict(pending.arguments),
                    missing_arguments=("entity",),
                    aggregate=pending.aggregate,
                    ambiguity_candidates=tuple(item.entity_id for item in candidates)
                    or pending.ambiguity_candidates,
                )
            if (
                pending.ambiguity_candidates
                and entity.entity_id not in pending.ambiguity_candidates
            ):
                candidates = tuple(
                    item
                    for entity_id in pending.ambiguity_candidates
                    if (item := self.entities.get(entity_id)) is not None
                )
                return self._clarification(
                    normalized,
                    self._entity_question(normalized, candidates),
                    skill=pending.skill,
                    arguments=dict(pending.arguments),
                    missing_arguments=("entity",),
                    aggregate=pending.aggregate,
                    ambiguity_candidates=pending.ambiguity_candidates,
                )
            if pending.aggregate:
                if entity.sensor_type == "printer":
                    return self._unsupported(
                        normalized,
                        "I can report printer status and temperatures, but not a generic sensor-reading set for the printer.",
                    )
                metrics = self.metrics.supported_for(entity.sensor_type)
                skill, arguments = _sensor_value_call(entity.entity_id, metrics)
                return self._matched(
                    normalized,
                    skill,
                    arguments,
                    min(0.98, resolution.confidence or 0.98),
                    aggregate=True,
                )

            skill = pending.skill
            arguments = {**pending.arguments, "entity": entity.entity_id}
            incompatibility = self._entity_skill_incompatibility(
                entity, skill, arguments
            )
            if incompatibility is not None:
                return self._unsupported(normalized, incompatibility)
            if skill is None:
                return self._unsupported(
                    normalized,
                    "I understood the sensor name, but the original request did not identify a supported reading.",
                )
            return self._matched(
                normalized,
                skill,
                arguments,
                min(0.98, resolution.confidence or 0.98),
            )

        if pending.missing_argument == "metric":
            resolution = self.metrics.resolve_details(normalized)
            if resolution.candidates:
                names = _joined_names(
                    tuple(item.display_name for item in resolution.candidates)
                )
                return self._clarification(
                    normalized,
                    f"Which measurement did you mean: {names}?",
                    skill=pending.skill,
                    arguments=dict(pending.arguments),
                    missing_arguments=("metric",),
                    ambiguity_candidates=tuple(
                        item.metric_id for item in resolution.candidates
                    ),
                )
            requested = _distinct_metrics(resolution.metrics)
            if not requested:
                return self._clarification(
                    normalized,
                    "Which measurement did you mean?",
                    skill=pending.skill,
                    arguments=dict(pending.arguments),
                    missing_arguments=("metric",),
                    ambiguity_candidates=pending.ambiguity_candidates,
                )
            if pending.ambiguity_candidates and any(
                item.metric_id not in pending.ambiguity_candidates for item in requested
            ):
                candidates = tuple(
                    self.metrics.require(metric_id)
                    for metric_id in pending.ambiguity_candidates
                )
                return self._clarification(
                    normalized,
                    f"Which measurement did you mean: {_joined_names(tuple(item.display_name for item in candidates))}?",
                    skill=pending.skill,
                    arguments=dict(pending.arguments),
                    missing_arguments=("metric",),
                    ambiguity_candidates=pending.ambiguity_candidates,
                )
            entity_id = pending.arguments.get("entity")
            entity = (
                self.entities.get(entity_id) if isinstance(entity_id, str) else None
            )
            if entity is None:
                return self._clarification(
                    normalized,
                    "Which sensor did you mean?",
                    skill="get_sensor_values"
                    if len(requested) > 1
                    else "get_sensor_value",
                    arguments=(
                        {"metrics": [item.metric_id for item in requested]}
                        if len(requested) > 1
                        else {"metric": requested[0].metric_id}
                    ),
                    missing_arguments=("entity",),
                )
            unsupported = tuple(
                item
                for item in requested
                if entity.sensor_type not in item.sensor_types
            )
            if unsupported:
                return self._unsupported(
                    normalized,
                    f"{entity.display_name} does not provide {_joined_names(tuple(item.display_name for item in unsupported))}.",
                )
            skill, arguments = _sensor_value_call(entity.entity_id, requested)
            return self._matched(
                normalized,
                skill,
                arguments,
                min(0.98, resolution.confidence or 0.98),
            )

        return self._unsupported(
            normalized,
            "I couldn't safely apply that clarification.",
        )

    @classmethod
    def _control_rejection(cls, normalized: str) -> RoutedIntent | None:
        if CONTROL_START.search(normalized) or CONTROL_TOGGLE.search(normalized):
            return cls._unsupported(
                normalized,
                "Control requests are disabled; Butters currently supports read-only questions.",
            )
        return None

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
    def _host_observation(text: str) -> str | None:
        mappings = (
            ("uptime", ("uptime", "how long has the pi", "how long has the server")),
            ("load", ("load average", "server load", "pi load", "cpu load")),
            (
                "memory",
                ("memory usage", "available memory", "server memory", "pi memory"),
            ),
            ("swap", ("swap usage", "swap status", "zram")),
            ("disk", ("disk usage", "disk space", "free disk", "root filesystem")),
            (
                "temperature",
                ("cpu temperature", "pi temperature", "server temperature"),
            ),
            ("throttle", ("throttle status", "throttled", "undervoltage")),
            ("failed_units", ("failed units", "failed services", "services failed")),
        )
        for value, phrases in mappings:
            if any(phrase in text for phrase in phrases):
                return value
        return None

    @staticmethod
    def _stack_observation(text: str) -> str | None:
        # Word-boundary matching keeps "support", "update", and "upstairs" from
        # standing in for the health question word "up".
        if not any(
            contains_phrase(text, word)
            for word in (
                "health",
                "healthy",
                "status",
                "running",
                "reachable",
                "working",
                "up",
            )
        ):
            return None
        mappings = (
            ("home_assistant", ("home assistant",)),
            ("influxdb", ("influxdb", "influx")),
            ("grafana", ("grafana",)),
            ("dashboard", ("dashboard",)),
            ("bridge", ("sensor bridge", "mqtt bridge", "bridge")),
            ("mqtt", ("mqtt", "mosquitto", "broker")),
            ("services", ("all services", "critical services")),
        )
        for value, phrases in mappings:
            if any(contains_phrase(text, phrase) for phrase in phrases):
                return value
        return None

    @staticmethod
    def _network_observation(text: str) -> str | None:
        mappings = (
            ("tailscale", ("tailscale status", "tailnet status")),
            ("interfaces", ("network interfaces", "interface status")),
            ("routes", ("route summary", "network routes", "routing table")),
            ("listeners", ("local listeners", "listening ports", "service ports")),
        )
        for value, phrases in mappings:
            if any(phrase in text for phrase in phrases):
                return value
        return None

    @staticmethod
    def _project_observation(text: str) -> str | None:
        if any(
            phrase in text
            for phrase in (
                "repo dirty",
                "repository dirty",
                "git status",
                "repo status",
            )
        ):
            return "status"
        if any(phrase in text for phrase in ("current branch", "git branch")):
            return "branch"
        if any(
            phrase in text
            for phrase in ("base commit", "current commit", "git commit are we on")
        ):
            return "base_commit"
        if any(phrase in text for phrase in ("recent commits", "git history")):
            return "recent_commits"
        if any(phrase in text for phrase in ("diff summary", "changed files")):
            return "diff_summary"
        return None

    @staticmethod
    def _history_range(text: str) -> str | None:
        historical = any(
            phrase in text
            for phrase in (
                "history",
                "historical",
                "trend",
                "average",
                "mean",
                "baseline",
                "over the last",
                "past hour",
                "past day",
                "past week",
            )
        )
        if not historical:
            return None
        if any(
            phrase in text
            for phrase in ("7 day", "seven day", "past week", "last week")
        ):
            return "7d"
        if any(
            phrase in text
            for phrase in ("1 hour", "one hour", "past hour", "last hour")
        ):
            return "1h"
        return "24h"

    @staticmethod
    def _all_sensor_status_request(text: str) -> bool:
        has_sensor = "sensor" in text
        has_all = any(word in text.split() for word in ("all", "every"))
        has_status = any(
            word in text.split() for word in ("reporting", "alive", "online", "status")
        )
        return has_sensor and has_all and has_status

    def _aggregate_measurement_request(
        self, text: str, *, has_registered_entity: bool
    ) -> bool:
        # Explicit metric slots always win over aggregate wording.  For example,
        # ``temperature and humidity readings`` is still a two-metric request.
        if self.metrics.resolve(text):
            return False
        words = set(text.split())
        aggregate = bool(words & {"reading", "readings"})
        aggregate = (
            aggregate
            or contains_phrase(text, "sensor data")
            or contains_phrase(text, "sensor values")
        )
        if words & {"everything"} and words & {
            "measuring",
            "measured",
            "reading",
            "readings",
            "sensor",
            "sensors",
        }:
            aggregate = True
        if words & {"all", "every"} and words & {
            "measurement",
            "measurements",
            "reading",
            "readings",
            "values",
        }:
            aggregate = True
        if "measurements" in words and not self.metrics.resolve(text):
            aggregate = True
        aggregate = aggregate or (
            "sensors" in words
            and "looking" in words
            and any(word in words for word in ("how", "what"))
        )
        if not aggregate:
            return False
        if has_registered_entity or words & {"sensor", "sensors", "station"}:
            return True
        # A missing-entity aggregate question may legitimately ask for a sensor
        # clarification, but incidental prose containing "readings" must not.
        return bool(words & {"what", "which", "how", "show", "give", "tell"})

    @staticmethod
    def _measurement_without_metric_request(text: str) -> bool:
        return any(
            contains_phrase(text, phrase)
            for phrase in (
                "what should i check",
                "what can i check",
                "which measurement",
                "which value",
                "what is it measuring",
            )
        )

    @staticmethod
    def _sensor_query_context(text: str) -> bool:
        words = set(text.split())
        return bool(
            words
            & {
                "what",
                "which",
                "how",
                "show",
                "give",
                "check",
                "reading",
                "readings",
                "measurement",
                "measurements",
                "level",
                "value",
                "values",
                "sensor",
                "sensors",
            }
        )

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

    def _print_analysis_request(self, text: str) -> dict[str, object] | None:
        print_context = any(
            phrase in text
            for phrase in (
                "this print",
                "current print",
                "print started",
                "during the print",
                "printer caused",
                "printer affected",
                "printer room air quality changed",
                "environmental impact",
            )
        )
        analytical = any(
            phrase in text
            for phrase in (
                "changed",
                "change",
                "compare",
                "affected",
                "impact",
                "causing",
                "caused",
                "increase",
                "most",
                "since",
                "heat",
                "heated",
                "warmer",
            )
        )
        if not (print_context and analytical):
            return None
        requested = tuple(
            metric.metric_id
            for metric in self.metrics.resolve(text)
            if metric.metric_id
            in {
                "temperature",
                "humidity",
                "co2",
                "pm1",
                "pm25",
                "pm4",
                "pm10",
                "voc_index",
                "nox_index",
            }
        )
        if not requested or any(
            phrase in text
            for phrase in ("air quality", "environmental impact", "which measurement")
        ):
            requested = (
                "temperature",
                "humidity",
                "co2",
                "pm1",
                "pm25",
                "pm4",
                "pm10",
                "voc_index",
                "nox_index",
            )
        if any(word in text for word in ("heat", "heated", "warmer")):
            requested = ("temperature",)
        selector = (
            "current_vs_previous"
            if "previous print" in text
            and any(word in text for word in ("compare", "larger", "impact"))
            else "previous"
            if "previous print" in text or "last print" in text
            else "current"
        )
        return {
            "printer": "x2d",
            "environment": "printer_room",
            "metrics": list(requested),
            "print_selector": selector,
            "baseline_minutes": 60 if "hour before" in text else None,
        }

    @staticmethod
    def _desktop_readiness_follow_up(text: str) -> bool:
        """Whether a wake request also asks to be told when the machine is up."""

        return any(
            phrase in text
            for phrase in (
                "reachable",
                "when it is up",
                "when it's up",
                "when it comes up",
                "when it is ready",
                "when it's ready",
                "when it is online",
                "when it's online",
                "tell me when",
                "let me know when",
            )
        )

    @staticmethod
    def _desktop_remote_action(text: str) -> bool:
        if "prepare my computer" in text:
            return True
        wake = any(
            phrase in text
            for phrase in (
                "turn on my computer",
                "wake my computer",
                "wake the desktop",
            )
        )
        return wake and any(word in text for word in ("parsec", "remote"))

    @staticmethod
    def _desktop_status_request(text: str) -> bool:
        return any(word in text for word in ("computer", "desktop")) and any(
            phrase in text
            for phrase in (
                "ready for parsec",
                "parsec ready",
                "computer status",
                "desktop status",
                "computer online",
                "desktop online",
            )
        )

    @staticmethod
    def _desktop_action(text: str) -> str | None:
        mappings = (
            (
                (
                    "turn off my monitors",
                    "turn desktop monitors off",
                    "headless mode",
                    "remote mode",
                    "enter headless mode",
                ),
                "monitors_off",
            ),
            (
                (
                    "turn on my monitors",
                    "turn desktop monitors on",
                    "local mode",
                    "restore local mode",
                    "restore local desktop",
                    "restore local display",
                    "exit remote mode",
                    "return to local mode",
                ),
                "monitors_on",
            ),
            (
                ("wake my desktop", "wake the desktop", "turn on my computer"),
                "wake_desktop",
            ),
            (("lock my desktop", "lock the desktop"), "lock_desktop"),
            (("sleep my desktop", "sleep the desktop"), "sleep_desktop"),
            (("restart my desktop", "restart the desktop"), "restart_desktop"),
            (
                (
                    "shut down my desktop",
                    "shutdown my desktop",
                    "shut down the desktop",
                ),
                "shutdown_desktop",
            ),
        )
        for phrases, skill in mappings:
            if any(phrase in text for phrase in phrases):
                return skill
        return None

    @staticmethod
    def _environment_action(text: str) -> tuple[str, dict[str, object]] | None:
        device = next(
            (
                name
                for name in ("heater", "dehumidifier", "ventilation")
                if name in text
            ),
            None,
        )
        if device is None:
            return None
        if not any(
            phrase in text
            for phrase in ("turn on", "switch on", "turn off", "switch off")
        ):
            return None
        state = (
            "off"
            if any(phrase in text for phrase in ("turn off", "switch off"))
            else "on"
        )
        match = re.search(r"\bfor\s+(\d{1,4})\s+(?:minute|minutes|min)\b", text)
        duration = int(match.group(1)) if match else None
        return f"set_{device}", {"state": state, "duration_minutes": duration}

    @staticmethod
    def _fixed_action(text: str) -> str | None:
        mappings = (
            (("wake my nas", "wake the nas", "turn on my nas"), "wake_nas"),
            (("restart butters", "restart butters service"), "restart_butters_service"),
            (("reboot butters host", "reboot the pi"), "reboot_butters_host"),
            (
                ("shut down butters host", "shutdown butters host", "shut down the pi"),
                "shutdown_butters_host",
            ),
        )
        for phrases, skill in mappings:
            if any(phrase in text for phrase in phrases):
                return skill
        return None

    @staticmethod
    def _capability_status_request(text: str) -> str | None:
        if "environment control" in text or any(
            phrase in text
            for phrase in ("heater status", "dehumidifier status", "ventilation status")
        ):
            return "get_environment_control_status"
        if any(phrase in text for phrase in ("nas status", "is my nas online")):
            return "get_nas_status"
        if "action broker" in text:
            return "get_action_broker_status"
        if any(
            phrase in text
            for phrase in ("butters service status", "is butters running")
        ):
            return "get_butters_service_status"
        if any(phrase in text for phrase in ("storage status", "disk space")):
            return "get_storage_status"
        if any(
            phrase in text for phrase in ("dependency health", "network service health")
        ):
            return "get_network_service_health"
        if any(phrase in text for phrase in ("butters host status", "pi status")):
            return "get_butters_host_status"
        return None

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
                "how much has my printer printed",
                "how heavily have i been using the printer",
                "printer usage",
            )
        )
        return printer_context and usage

    @staticmethod
    def _printer_maintenance_events_request(text: str) -> bool:
        return any(
            contains_phrase(text, phrase)
            for phrase in (
                "maintenance events",
                "maintenance notifications",
                "maintenance alerts",
            )
        )

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

    def _require_entity(
        self,
        normalized: str,
        entity: Entity | None,
        *,
        skill: str,
        arguments: dict[str, object] | None = None,
        candidates: tuple[Entity, ...] = (),
    ) -> Entity | RoutedIntent:
        if entity is not None:
            return entity
        return self._clarification(
            normalized,
            self._entity_question(normalized, candidates),
            skill=skill,
            arguments=arguments,
            missing_arguments=("entity",),
            ambiguity_candidates=tuple(item.entity_id for item in candidates),
        )

    def _require_printer(
        self,
        normalized: str,
        entity: Entity | None,
        *,
        skill: str,
        candidates: tuple[Entity, ...] = (),
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
            skill=skill,
            missing_arguments=("entity",),
            ambiguity_candidates=tuple(
                item.entity_id for item in candidates if item.sensor_type == "printer"
            ),
        )

    def _entity_question(self, normalized: str, candidates: tuple[Entity, ...]) -> str:
        if candidates:
            names = _joined_names(tuple(item.display_name for item in candidates))
            return f"Which sensor did you mean: {names}?"
        if self._mentions_unnumbered_box(normalized):
            return "Which filament box did you mean?"
        return "Which sensor did you mean?"

    def _entity_skill_incompatibility(
        self,
        entity: Entity,
        skill: str | None,
        arguments: dict[str, object],
    ) -> str | None:
        if (
            skill
            in {
                "get_sensor_value",
                "get_sensor_values",
                "get_sensor_history_summary",
                "get_sensor_last_seen",
                "get_sensor_status",
            }
            and entity.sensor_type == "printer"
        ):
            return f"{entity.display_name} needs a printer-specific question."
        if skill == "get_room_air_quality" and entity.sensor_type != "air_quality":
            return f"{entity.display_name} is not an air-quality station."
        if (
            skill
            in {
                "get_printer_status",
                "get_printer_temperatures",
                "get_printer_usage",
                "get_printer_maintenance",
                "get_printer_maintenance_events",
                "get_current_print",
                "get_last_print",
                "get_print_environment_summary",
            }
            and entity.sensor_type != "printer"
        ):
            return f"{entity.display_name} is not a printer."
        metric_ids: tuple[str, ...] = ()
        metric = arguments.get("metric")
        metrics = arguments.get("metrics")
        if isinstance(metric, str):
            metric_ids = (metric,)
        elif isinstance(metrics, list) and all(
            isinstance(item, str) for item in metrics
        ):
            metric_ids = tuple(metrics)
        unsupported = tuple(
            self.metrics.require(metric_id)
            for metric_id in metric_ids
            if entity.sensor_type not in self.metrics.require(metric_id).sensor_types
        )
        if unsupported:
            return (
                f"{entity.display_name} does not provide "
                f"{_joined_names(tuple(item.display_name for item in unsupported))}."
            )
        return None

    @staticmethod
    def _matched(
        text: str,
        skill: str,
        arguments: dict[str, object],
        confidence: float,
        *,
        aggregate: bool = False,
    ) -> RoutedIntent:
        return RoutedIntent(
            "matched",
            text,
            skill,
            arguments,
            confidence,
            aggregate=aggregate,
        )

    @staticmethod
    def _clarification(
        text: str,
        message: str,
        *,
        skill: str | None = None,
        arguments: dict[str, object] | None = None,
        missing_arguments: tuple[str, ...] = (),
        aggregate: bool = False,
        ambiguity_candidates: tuple[str, ...] = (),
    ) -> RoutedIntent:
        return RoutedIntent(
            "clarification",
            text,
            skill=skill,
            arguments=arguments or {},
            message=message,
            missing_arguments=missing_arguments,
            aggregate=aggregate,
            ambiguity_candidates=ambiguity_candidates,
        )

    @staticmethod
    def _unsupported(
        text: str, message: str, *, allow_fallback: bool = False
    ) -> RoutedIntent:
        return RoutedIntent(
            "unsupported", text, message=message, allow_fallback=allow_fallback
        )
