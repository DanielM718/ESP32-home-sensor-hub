"""First-class normal skills backed by reviewed diagnostic and Git adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from butters.diagnostics.tools import DiagnosticToolRegistry
from butters.integrations.project import ProjectInspectionAdapter
from butters.skills.model import (
    ActionClass,
    HostObservationArgs,
    NetworkObservationArgs,
    ProjectStatusArgs,
    ReadOnlyObservationResult,
    SensorHistorySummaryArgs,
    SkillArguments,
    SkillError,
    SkillResult,
    StackObservationArgs,
)
from butters.skills.registry import SkillRegistry, SkillSpec, required_string, strict_arguments


HOST_TOOLS = {
    "uptime": "get_uptime",
    "load": "get_load",
    "memory": "get_memory_status",
    "swap": "get_swap_status",
    "disk": "get_disk_status",
    "temperature": "get_temperature",
    "throttle": "get_throttle_status",
    "failed_units": "get_failed_units",
}
STACK_TOOLS = {
    "mqtt": "get_mqtt_health",
    "bridge": "get_bridge_health",
    "dashboard": "get_dashboard_health",
    "influxdb": "get_influx_health",
    "grafana": "get_grafana_health",
    "home_assistant": "get_home_assistant_health",
    "services": "get_service_summary",
}
NETWORK_TOOLS = {
    "interfaces": "get_network_interfaces",
    "routes": "get_route_summary",
    "tailscale": "get_tailscale_status",
    "listeners": "get_local_listeners",
}
PROJECT_VIEWS = frozenset(ProjectInspectionAdapter.COMMANDS)


class PromotedReadOnlySkills:
    def __init__(
        self,
        tools: DiagnosticToolRegistry,
        project: ProjectInspectionAdapter,
    ) -> None:
        self.tools = tools
        self.project = project

    def authorize_host(self, arguments: SkillArguments) -> None:
        if cast(HostObservationArgs, arguments).metric not in HOST_TOOLS:
            raise SkillError("policy_denied", "host metric is not allow-listed")

    def authorize_stack(self, arguments: SkillArguments) -> None:
        if cast(StackObservationArgs, arguments).component not in STACK_TOOLS:
            raise SkillError("policy_denied", "stack component is not allow-listed")

    def authorize_network(self, arguments: SkillArguments) -> None:
        if cast(NetworkObservationArgs, arguments).view not in NETWORK_TOOLS:
            raise SkillError("policy_denied", "network view is not allow-listed")

    def authorize_history(self, arguments: SkillArguments) -> None:
        args = cast(SensorHistorySummaryArgs, arguments)
        failure = self.tools.validate(
            "get_sensor_history_summary",
            {"entity": args.entity, "range_key": args.range_key},
        )
        if failure is not None:
            raise SkillError("policy_denied", "sensor history request is not allow-listed")

    def authorize_project(self, arguments: SkillArguments) -> None:
        if cast(ProjectStatusArgs, arguments).view not in PROJECT_VIEWS:
            raise SkillError("policy_denied", "project view is not allow-listed")

    def host(self, arguments: SkillArguments) -> SkillResult:
        metric = cast(HostObservationArgs, arguments).metric
        return self._tool(HOST_TOOLS[metric], {})

    def stack(self, arguments: SkillArguments) -> SkillResult:
        component = cast(StackObservationArgs, arguments).component
        return self._tool(STACK_TOOLS[component], {})

    def network(self, arguments: SkillArguments) -> SkillResult:
        view = cast(NetworkObservationArgs, arguments).view
        return self._tool(NETWORK_TOOLS[view], {})

    def history(self, arguments: SkillArguments) -> SkillResult:
        args = cast(SensorHistorySummaryArgs, arguments)
        return self._tool(
            "get_sensor_history_summary",
            {"entity": args.entity, "range_key": args.range_key},
        )

    def project_status(self, arguments: SkillArguments) -> SkillResult:
        view = cast(ProjectStatusArgs, arguments).view
        values = self.project.inspect(view)
        return ReadOnlyObservationResult("project_status", "ok", values)

    def _tool(self, name: str, arguments: dict[str, object]) -> ReadOnlyObservationResult:
        execution = self.tools.execute(name, arguments)
        evidence = execution.evidence
        return ReadOnlyObservationResult(
            name,
            evidence.status.value,
            evidence.values,
            evidence.text_excerpt,
            None if execution.ok else (evidence.error or "unavailable"),
            evidence.truncated,
        )


def register_promoted_skills(
    registry: SkillRegistry,
    tools: DiagnosticToolRegistry,
    project: ProjectInspectionAdapter,
) -> None:
    impl = PromotedReadOnlySkills(tools, project)
    registry.register(
        SkillSpec(
            "get_host_observation",
            "Read one bounded Raspberry Pi resource observation.",
            ActionClass.READ_ONLY,
            _parse_host,
            impl.authorize_host,
            impl.host,
            7.0,
            category="system",
            input_schema={"metric": sorted(HOST_TOOLS)},
            result_description="Sanitized bounded host observation.",
            permission_summary=("procfs_read", "fixed_system_commands"),
            positive_examples=("what is the Pi uptime", "is the server throttled"),
            negative_examples=("restart the Pi", "run uname"),
            source_reference="butters.skills.promoted",
        )
    )
    registry.register(
        SkillSpec(
            "get_stack_observation",
            "Read health for one allow-listed monitoring component.",
            ActionClass.READ_ONLY,
            _parse_stack,
            impl.authorize_stack,
            impl.stack,
            8.0,
            category="monitoring",
            input_schema={"component": sorted(STACK_TOOLS)},
            result_description="Sanitized bounded component health evidence.",
            permission_summary=("allowlisted_service_read", "fixed_loopback_health_check"),
            positive_examples=("is Grafana healthy", "check MQTT health"),
            negative_examples=("restart Grafana", "publish MQTT"),
            source_reference="butters.skills.promoted",
        )
    )
    registry.register(
        SkillSpec(
            "get_network_observation",
            "Read one bounded allow-listed local network view.",
            ActionClass.READ_ONLY,
            _parse_network,
            impl.authorize_network,
            impl.network,
            7.0,
            category="network",
            input_schema={"view": sorted(NETWORK_TOOLS)},
            result_description="Sanitized interfaces, routes, Tailscale, or listener summary.",
            permission_summary=("procfs_read", "fixed_network_commands", "no_scanning"),
            positive_examples=("what is the Tailscale status",),
            negative_examples=("scan my network", "probe an arbitrary host"),
            source_reference="butters.skills.promoted",
        )
    )
    registry.register(
        SkillSpec(
            "get_sensor_history_summary",
            "Compute a bounded historical sensor summary for a reviewed range.",
            ActionClass.READ_ONLY,
            _parse_history,
            impl.authorize_history,
            impl.history,
            12.0,
            category="sensors",
            input_schema={"entity": "configured entity", "range_key": ["1h", "24h", "7d"]},
            result_description="Local min/max/mean/trend summary without raw time-series export.",
            permission_summary=("dashboard_api_read", "bounded_history", "local_computation"),
            positive_examples=("humidity trend for box three over 24 hours",),
            negative_examples=("query arbitrary InfluxDB",),
            source_reference="butters.skills.promoted",
        )
    )
    registry.register(
        SkillSpec(
            "get_project_status",
            "Inspect this repository using one fixed read-only Git view.",
            ActionClass.READ_ONLY,
            _parse_project,
            impl.authorize_project,
            impl.project_status,
            7.0,
            category="project",
            input_schema={"view": sorted(PROJECT_VIEWS)},
            result_description="Bounded sanitized Git status, branch, commit, log, or diff summary.",
            permission_summary=("fixed_git_read", "configured_repository_only"),
            positive_examples=("what commit is Butters on", "is the repo dirty"),
            negative_examples=("git reset", "read another path"),
            source_reference="butters.skills.promoted",
        )
    )


def _parse_host(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"metric"}))
    return HostObservationArgs(required_string(values, "metric"))


def _parse_stack(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"component"}))
    return StackObservationArgs(required_string(values, "component"))


def _parse_network(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"view"}))
    return NetworkObservationArgs(required_string(values, "view"))


def _parse_history(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"entity", "range_key"}))
    return SensorHistorySummaryArgs(
        required_string(values, "entity"),
        required_string(values, "range_key"),
    )


def _parse_project(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"view"}))
    return ProjectStatusArgs(required_string(values, "view"))
