"""Deterministic request classification and local playbook planning."""

from __future__ import annotations

from dataclasses import dataclass

from butters.diagnostics.model import (
    DiagnosticDomain,
    DiagnosticRequest,
    RequestComplexity,
    RequestDepth,
)
from butters.routing.entities import EntityRegistry
from butters.routing.normalization import normalize_request


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class DiagnosticPlan:
    playbook: str
    domain: DiagnosticDomain
    target: str | None
    tools: tuple[ToolInvocation, ...]
    complexity: RequestComplexity


class DiagnosticPlanner:
    def __init__(self, entities: EntityRegistry) -> None:
        self.entities = entities

    def request_from_text(
        self,
        text: str,
        *,
        local_only: bool = False,
        allow_cloud: bool = False,
        max_escalation: int = 3,
    ) -> DiagnosticRequest | None:
        normalized = normalize_request(text)
        # Diagnostics must not steal explicit control requests from the
        # default-deny command router. A later milestone may add separately
        # confirmed controls; this one remains strictly observational.
        if any(
            phrase in normalized
            for phrase in (
                "restart ",
                "start service",
                "stop ",
                "reboot",
                "shutdown",
                "publish ",
                "turn on",
                "turn off",
            )
        ):
            return None
        domain = self._domain(normalized)
        if domain is DiagnosticDomain.UNKNOWN:
            return None
        resolution = self.entities.resolve(normalized)
        target = resolution.entity.entity_id if resolution.entity is not None else None
        depth = (
            RequestDepth.EXHAUSTIVE
            if any(phrase in normalized for phrase in ("exhaustive", "deep dive", "maximum analysis"))
            else RequestDepth.DETAILED
            if any(phrase in normalized for phrase in ("detailed", "thorough", "analyze", "explain why"))
            else RequestDepth.NORMAL
        )
        complexity = self._complexity(normalized, depth)
        return DiagnosticRequest(
            text=text,
            domain=domain,
            target=target,
            depth=depth,
            local_only=local_only,
            allow_cloud=allow_cloud,
            max_escalation=max_escalation,
            complexity=complexity,
        )

    def plan(self, request: DiagnosticRequest) -> DiagnosticPlan:
        domain = request.domain
        target = request.target
        topic = self._topic(target) if target else None
        if domain is DiagnosticDomain.SENSOR:
            if target is None:
                return DiagnosticPlan("sensor_not_reporting", domain, None, (), request.complexity)
            tools = [
                ToolInvocation("get_sensor_status", {"entity": target}),
                ToolInvocation("get_mqtt_health", {}),
                ToolInvocation("get_bridge_health", {}),
            ]
            if topic:
                tools.append(ToolInvocation("inspect_allowlisted_mqtt_topic", {"topic": topic}))
            return DiagnosticPlan("sensor_not_reporting", domain, target, tuple(tools), request.complexity)
        if domain is DiagnosticDomain.SENSOR_PIPELINE:
            target = target or "printer_room"
            topic = self._topic(target)
            tools = [
                ToolInvocation("get_sensor_status", {"entity": target}),
                ToolInvocation("get_mqtt_health", {}),
                ToolInvocation("get_bridge_health", {}),
                ToolInvocation("get_influx_health", {}),
                ToolInvocation("get_dashboard_health", {}),
            ]
            if topic:
                tools.append(ToolInvocation("inspect_allowlisted_mqtt_topic", {"topic": topic}))
            return DiagnosticPlan("sensor_dashboard_pipeline", domain, target, tuple(tools), request.complexity)
        if domain is DiagnosticDomain.GRAFANA:
            target = target or "printer_room"
            return DiagnosticPlan(
                "grafana_current_data",
                domain,
                target,
                (
                    ToolInvocation("get_grafana_health", {}),
                    ToolInvocation("get_influx_health", {}),
                    ToolInvocation("get_sensor_status", {"entity": target}),
                    ToolInvocation("get_dashboard_health", {}),
                ),
                request.complexity,
            )
        if domain is DiagnosticDomain.HOME_ASSISTANT:
            target = target or "printer_room"
            return DiagnosticPlan(
                "home_assistant_sensor",
                domain,
                target,
                (
                    ToolInvocation("get_sensor_status", {"entity": target}),
                    ToolInvocation("get_home_assistant_health", {}),
                    ToolInvocation("get_container_status", {"container": "home-sensor-ha-discovery"}),
                    ToolInvocation("get_container_status", {"container": "homeassistant"}),
                ),
                request.complexity,
            )
        if domain is DiagnosticDomain.MQTT:
            return DiagnosticPlan(
                "mqtt_failure",
                domain,
                None,
                (
                    ToolInvocation("get_mqtt_health", {}),
                    ToolInvocation("get_bridge_health", {}),
                ),
                request.complexity,
            )
        if domain is DiagnosticDomain.INFLUXDB:
            return DiagnosticPlan(
                "influxdb_failure",
                domain,
                None,
                (
                    ToolInvocation("get_influx_health", {}),
                    ToolInvocation("get_bridge_health", {}),
                    ToolInvocation("get_dashboard_health", {}),
                ),
                request.complexity,
            )
        if domain is DiagnosticDomain.SERVER:
            return DiagnosticPlan(
                "server_health",
                domain,
                "butters",
                (
                    ToolInvocation("get_server_health", {}),
                    ToolInvocation("get_load", {}),
                    ToolInvocation("get_memory_status", {}),
                    ToolInvocation("get_swap_status", {}),
                    ToolInvocation("get_disk_status", {}),
                    ToolInvocation("get_temperature", {}),
                    ToolInvocation("get_throttle_status", {}),
                    ToolInvocation("get_failed_units", {}),
                ),
                request.complexity,
            )
        if domain is DiagnosticDomain.NETWORK:
            host = target if target in {"localhost", "butters"} else "butters"
            tools = [
                ToolInvocation("resolve_host", {"host": host}),
                ToolInvocation("get_route_summary", {}),
                ToolInvocation("ping_allowlisted_host", {"host": host}),
                ToolInvocation("get_network_interfaces", {}),
            ]
            endpoint = self._endpoint(request.text)
            if endpoint:
                tools.append(ToolInvocation("check_tcp_port", {"endpoint": endpoint}))
            return DiagnosticPlan(
                "network_host",
                domain,
                host,
                tuple(tools),
                request.complexity,
            )
        if domain is DiagnosticDomain.KR260:
            return DiagnosticPlan(
                "kr260_basic_health",
                domain,
                "kr260",
                (ToolInvocation("run_kr260_diagnostic", {}),),
                request.complexity,
            )
        if domain is DiagnosticDomain.MONITORING_STACK:
            return DiagnosticPlan(
                "monitoring_pipeline",
                domain,
                "critical_path",
                (
                    ToolInvocation("get_sensor_status", {"entity": "printer_room"}),
                    ToolInvocation("get_mqtt_health", {}),
                    ToolInvocation("get_bridge_health", {}),
                    ToolInvocation("get_influx_health", {}),
                    ToolInvocation("get_dashboard_health", {}),
                    ToolInvocation("get_grafana_health", {}),
                ),
                request.complexity,
            )
        return DiagnosticPlan("unknown", DiagnosticDomain.UNKNOWN, target, (), request.complexity)

    def _topic(self, entity_id: str | None) -> str | None:
        entity = self.entities.get(entity_id) if entity_id else None
        if entity is None:
            return None
        return (
            f"home/air/{entity.source_id}"
            if entity.sensor_type == "air_quality"
            else f"home/sensors/{entity.source_id}"
        )

    @staticmethod
    def _endpoint(text: str) -> str | None:
        normalized = normalize_request(text)
        for endpoint, aliases in {
            "mqtt": ("mqtt", "mosquitto", "1883"),
            "grafana": ("grafana", "3000"),
            "dashboard": ("dashboard", "8080"),
            "influxdb": ("influx", "8086"),
            "home_assistant": ("home assistant", "8123"),
        }.items():
            if any(alias in normalized for alias in aliases):
                return endpoint
        return None

    @staticmethod
    def _domain(text: str) -> DiagnosticDomain:
        if "kr260" in text or "kria" in text:
            return DiagnosticDomain.KR260
        if "grafana" in text:
            return DiagnosticDomain.GRAFANA
        if "home assistant" in text:
            return DiagnosticDomain.HOME_ASSISTANT
        if "mqtt" in text or "mosquitto" in text or "broker" in text:
            return DiagnosticDomain.MQTT
        if "influx" in text:
            return DiagnosticDomain.INFLUXDB
        if any(phrase in text for phrase in ("monitoring stack", "pipeline health", "monitoring pipeline", "stack working")):
            return DiagnosticDomain.MONITORING_STACK
        if "dashboard" in text and any(word in text for word in ("data", "updating", "update", "appearing", "current", "stale", "broken")):
            return DiagnosticDomain.SENSOR_PIPELINE
        if any(word in text.split() for word in ("sensor", "sen66", "sht41", "box", "container")) and any(
            phrase in text for phrase in ("not reporting", "offline", "stale", "missing", "has not reported", "isnt reporting", "isn t reporting")
        ):
            return DiagnosticDomain.SENSOR
        if any(phrase in text for phrase in ("server health", "server problem", "pi health", "server degraded", "high load", "low memory", "thermal throttling")):
            return DiagnosticDomain.SERVER
        if any(phrase in text for phrase in ("host unreachable", "network problem", "network unreachable", "port closed", "cannot reach")):
            return DiagnosticDomain.NETWORK
        return DiagnosticDomain.UNKNOWN

    @staticmethod
    def _complexity(text: str, depth: RequestDepth) -> RequestComplexity:
        systems = tuple(
            name
            for name, aliases in {
                "sensor": ("sensor", "sen66", "sht41"),
                "mqtt": ("mqtt", "mosquitto", "broker"),
                "bridge": ("bridge",),
                "influxdb": ("influx",),
                "grafana": ("grafana",),
                "dashboard": ("dashboard",),
                "home_assistant": ("home assistant",),
                "kr260": ("kr260", "kria"),
            }.items()
            if any(alias in text for alias in aliases)
        )
        return RequestComplexity(
            diagnostic_language=any(word in text for word in ("diagnose", "why", "problem", "broken", "failure")),
            historical_comparison=any(word in text for word in ("historical", "yesterday", "trend", "compared")),
            systems_referenced=systems,
            root_cause_requested=any(phrase in text for phrase in ("why", "root cause", "explain")),
            explicit_deep_analysis=depth is RequestDepth.EXHAUSTIVE,
        )
