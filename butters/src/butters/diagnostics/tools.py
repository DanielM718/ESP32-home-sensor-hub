"""Allow-listed READ_ONLY diagnostic tools with strict typed arguments."""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeAlias, cast

from butters.assistant_config import AssistantSettings
from butters.diagnostics.evidence import (
    EvidenceItem,
    EvidenceStatus,
    Sensitivity,
)
from butters.diagnostics.sanitizer import sanitize_text
from butters.integrations.server_health import LocalServerHealthAdapter
from butters.routing.entities import Entity, EntityRegistry, MetricRegistry
from butters.skills.model import ActionClass, SkillError
from butters.skills.policy import PolicyValidator
from butters.skills.registry import required_string, strict_arguments


SERVICE_ALLOWLIST: dict[str, str] = {
    "mosquitto": "mosquitto.service",
    "influxdb": "influxdb.service",
    "grafana": "grafana-server.service",
    "bridge": "home-sensor-bridge.service",
    "dashboard": "home-sensor-dashboard.service",
    "export_worker": "home-sensor-export-worker.service",
    "docker": "docker.service",
    "containerd": "containerd.service",
    "tailscale": "tailscaled.service",
}
CONTAINER_ALLOWLIST = frozenset({"homeassistant", "home-sensor-ha-discovery"})
HOST_ALLOWLIST: dict[str, str] = {
    "localhost": "127.0.0.1",
    "butters": "127.0.0.1",
}
ENDPOINT_ALLOWLIST: dict[str, tuple[str, int]] = {
    "mqtt": ("127.0.0.1", 1883),
    "grafana": ("127.0.0.1", 3000),
    "dashboard": ("127.0.0.1", 8080),
    "influxdb": ("127.0.0.1", 8086),
    "home_assistant": ("127.0.0.1", 8123),
}
LISTENER_PORT_ALLOWLIST = frozenset(port for _host, port in ENDPOINT_ALLOWLIST.values())
KR260_UNAVAILABLE = (
    "No approved KR260 SSH, serial, agent, or API transport is configured on this Pi."
)


class DiagnosticToolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class NoArgs:
    pass


@dataclass(frozen=True, slots=True)
class ServiceArgs:
    service: str


@dataclass(frozen=True, slots=True)
class LogArgs:
    service: str
    minutes: int
    max_lines: int


@dataclass(frozen=True, slots=True)
class HostArgs:
    host: str


@dataclass(frozen=True, slots=True)
class EndpointArgs:
    endpoint: str


@dataclass(frozen=True, slots=True)
class SensorArgs:
    entity: str


@dataclass(frozen=True, slots=True)
class SensorValueArgs:
    entity: str
    metric: str


@dataclass(frozen=True, slots=True)
class SensorHistoryArgs:
    entity: str
    range_key: str


@dataclass(frozen=True, slots=True)
class TopicArgs:
    topic: str


@dataclass(frozen=True, slots=True)
class ContainerArgs:
    container: str


@dataclass(frozen=True, slots=True)
class ContainerLogArgs:
    container: str
    minutes: int
    max_lines: int


ToolArguments: TypeAlias = (
    NoArgs
    | ServiceArgs
    | LogArgs
    | HostArgs
    | EndpointArgs
    | SensorArgs
    | SensorValueArgs
    | SensorHistoryArgs
    | TopicArgs
    | ContainerArgs
    | ContainerLogArgs
)
ArgumentParser = Callable[[Mapping[str, object]], ToolArguments]
Authorizer = Callable[[ToolArguments], None]
Implementation = Callable[[ToolArguments], EvidenceItem]


@dataclass(frozen=True, slots=True)
class DiagnosticToolSpec:
    name: str
    description: str
    argument_type: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    action_class: ActionClass
    timeout_seconds: float
    max_output_bytes: int
    parse_arguments: ArgumentParser
    authorize: Authorizer
    implementation: Implementation
    error_behavior: str = "Return sanitized structured unavailable/error evidence."
    sensitivity_behavior: str = (
        "Internal evidence only; secret-like keys/text are redacted before return."
    )

    def as_model_tool(self) -> dict[str, object]:
        return {
            "type": "function",
            "name": self.name,
            "description": (
                f"{self.description} Returns {self.output_schema['description']} "
                f"Errors: {self.error_behavior}"
            ),
            "parameters": self.input_schema,
            "strict": True,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticToolExecution:
    tool: str
    action_class: ActionClass | None
    arguments: dict[str, object]
    elapsed_seconds: float
    evidence: EvidenceItem

    @property
    def ok(self) -> bool:
        return self.evidence.status not in {EvidenceStatus.ERROR}


class DiagnosticToolRegistry:
    """Validate policy locally before any diagnostic implementation executes."""

    def __init__(self, policy: PolicyValidator | None = None) -> None:
        self._policy = policy or PolicyValidator()
        self._specs: dict[str, DiagnosticToolSpec] = {}

    @property
    def tools(self) -> tuple[DiagnosticToolSpec, ...]:
        return tuple(self._specs.values())

    def get(self, name: str) -> DiagnosticToolSpec | None:
        return self._specs.get(name)

    def register(self, spec: DiagnosticToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate diagnostic tool: {spec.name}")
        if spec.action_class is not ActionClass.READ_ONLY:
            raise ValueError("diagnostic tools must remain READ_ONLY")
        if spec.timeout_seconds <= 0 or spec.max_output_bytes < 256:
            raise ValueError("diagnostic tool limits must be positive")
        self._specs[spec.name] = spec

    def execute(
        self, name: str, arguments: Mapping[str, object]
    ) -> DiagnosticToolExecution:
        started = time.perf_counter()
        spec = self._specs.get(name)
        if spec is None:
            evidence = _error_evidence(
                f"tool.{name}", name, "unknown", "unknown_tool", "tool is not registered"
            )
            return DiagnosticToolExecution(
                name, None, dict(arguments), time.perf_counter() - started, evidence
            )
        try:
            parsed = spec.parse_arguments(arguments)
            # Diagnostic arguments are a separate union, but the existing policy
            # validator is deliberately reused as the authoritative action gate.
            self._policy.authorize(  # type: ignore[arg-type]
                skill_name=spec.name,
                action_class=spec.action_class,
                arguments=parsed,
                authorizer=spec.authorize,  # type: ignore[arg-type]
            )
            evidence = spec.implementation(parsed)
            elapsed = time.perf_counter() - started
            if elapsed > spec.timeout_seconds:
                raise DiagnosticToolError("timeout", "diagnostic tool exceeded deadline")
            evidence = _bound_evidence(evidence, spec.max_output_bytes)
        except (SkillError, DiagnosticToolError) as exc:
            evidence = _error_evidence(
                f"tool.{name}", name, _target_from_arguments(arguments), exc.code, str(exc)
            )
        except Exception:  # noqa: BLE001
            evidence = _error_evidence(
                f"tool.{name}",
                name,
                _target_from_arguments(arguments),
                "internal_error",
                "the read-only diagnostic tool could not complete",
            )
        return DiagnosticToolExecution(
            name,
            spec.action_class,
            dict(arguments),
            time.perf_counter() - started,
            evidence,
        )

    def validate(self, name: str, arguments: Mapping[str, object]) -> str | None:
        spec = self._specs.get(name)
        if spec is None:
            return "unknown_tool"
        try:
            parsed = spec.parse_arguments(arguments)
            self._policy.authorize(  # type: ignore[arg-type]
                skill_name=spec.name,
                action_class=spec.action_class,
                arguments=parsed,
                authorizer=spec.authorize,  # type: ignore[arg-type]
            )
        except SkillError as exc:
            return exc.code
        except Exception:  # noqa: BLE001
            return "policy_denied"
        return None


class DashboardDiagnosticClient:
    """Bounded HTTP reader for fixed dashboard routes and typed query parameters."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._opener = opener

    def get(self, path: str) -> dict[str, Any]:
        if path not in {"/api/health", "/api/latest", "/api/nodes", "/api/status"} and not path.startswith("/api/readings?"):
            raise DiagnosticToolError("policy_denied", "dashboard path is not allow-listed")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json", "User-Agent": "Butters-Diagnostics/0.6"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(self.max_response_bytes + 1)
        except TimeoutError as exc:
            raise DiagnosticToolError("timeout", "dashboard request timed out") from exc
        except urllib.error.HTTPError as exc:
            raise DiagnosticToolError("upstream_status", f"dashboard returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise DiagnosticToolError("unavailable", "dashboard is unavailable") from exc
        if status != 200:
            raise DiagnosticToolError("upstream_status", f"dashboard returned HTTP {status}")
        if len(raw) > self.max_response_bytes:
            raise DiagnosticToolError("output_too_large", "dashboard response exceeded limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DiagnosticToolError("invalid_response", "dashboard returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise DiagnosticToolError("invalid_response", "dashboard response is not an object")
        return value


class DiagnosticImplementations:
    def __init__(
        self,
        settings: AssistantSettings,
        *,
        runner: Callable[..., Any] = subprocess.run,
        opener: Callable[..., Any] = urllib.request.urlopen,
        socket_connector: Callable[..., Any] = socket.create_connection,
    ) -> None:
        self.settings = settings
        self.entities = EntityRegistry(settings.entities)
        self.metrics = MetricRegistry()
        self.runner = runner
        self.opener = opener
        self.socket_connector = socket_connector
        self.dashboard = DashboardDiagnosticClient(
            settings.integration.dashboard_url,
            timeout_seconds=settings.integration.timeout_seconds,
            max_response_bytes=settings.integration.max_response_bytes,
            opener=opener,
        )

    # Host tools
    def get_server_health(self, _args: ToolArguments) -> EvidenceItem:
        snapshot = LocalServerHealthAdapter(runner=self.runner).snapshot()
        inactive = [service.unit for service in snapshot.services if not service.active]
        values = {
            "uptime_seconds": snapshot.uptime_seconds,
            "load": [snapshot.load_1m, snapshot.load_5m, snapshot.load_15m],
            "available_memory_bytes": snapshot.available_memory_bytes,
            "swap_used_bytes": snapshot.swap_used_bytes,
            "disk_free_bytes": snapshot.disk_free_bytes,
            "disk_total_bytes": snapshot.disk_total_bytes,
            "temperature_c": snapshot.temperature_c,
            "throttled": snapshot.throttled,
            "services": [
                {"name": service.name, "unit": service.unit, "state": service.state, "active": service.active}
                for service in snapshot.services
            ],
        }
        status = EvidenceStatus.DEGRADED if inactive else EvidenceStatus.OK
        return EvidenceItem.create(
            "server.health", "server_health", "local_system", "butters", status, values=values
        )

    def get_uptime(self, _args: ToolArguments) -> EvidenceItem:
        value = _read_float(Path("/proc/uptime"))
        return _metric_evidence("server.uptime", "uptime", "butters", value, "uptime_seconds")

    def get_load(self, _args: ToolArguments) -> EvidenceItem:
        values = os.getloadavg()
        status = EvidenceStatus.DEGRADED if values[0] >= max(4.0, (os.cpu_count() or 1) * 1.5) else EvidenceStatus.OK
        return EvidenceItem.create(
            "server.load", "load", "local_system", "butters", status,
            values={"load_1m": values[0], "load_5m": values[1], "load_15m": values[2], "cpu_count": os.cpu_count()},
        )

    def get_memory_status(self, _args: ToolArguments) -> EvidenceItem:
        memory = _meminfo()
        total = memory.get("MemTotal")
        available = memory.get("MemAvailable")
        percent = (available / total * 100) if total and available is not None else None
        status = EvidenceStatus.DEGRADED if percent is not None and percent < 10 else EvidenceStatus.OK
        return EvidenceItem.create(
            "server.memory", "memory_status", "procfs", "butters", status,
            values={"total_bytes": _kib(total), "available_bytes": _kib(available), "available_percent": percent},
        )

    def get_swap_status(self, _args: ToolArguments) -> EvidenceItem:
        memory = _meminfo()
        total = memory.get("SwapTotal") or 0
        free = memory.get("SwapFree") or 0
        used = max(0, total - free)
        vm = _vmstat()
        return EvidenceItem.create(
            "server.swap", "swap_status", "procfs", "butters", EvidenceStatus.OK,
            values={"total_bytes": _kib(total), "used_bytes": _kib(used), "pswpin_pages": vm.get("pswpin"), "pswpout_pages": vm.get("pswpout")},
        )

    def get_disk_status(self, _args: ToolArguments) -> EvidenceItem:
        disk = shutil.disk_usage(Path("/"))
        used_percent = (disk.used / disk.total * 100) if disk.total else 0.0
        status = EvidenceStatus.DEGRADED if used_percent >= 90 else EvidenceStatus.OK
        return EvidenceItem.create(
            "server.disk", "disk_status", "statvfs", "/", status,
            values={"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free, "used_percent": used_percent},
        )

    def get_temperature(self, _args: ToolArguments) -> EvidenceItem:
        raw = _read_text(Path("/sys/class/thermal/thermal_zone0/temp"), 64)
        value = float(raw) / 1000 if raw and raw.strip().isdigit() else None
        status = (
            EvidenceStatus.UNAVAILABLE
            if value is None
            else EvidenceStatus.DEGRADED
            if value >= 80
            else EvidenceStatus.OK
        )
        return EvidenceItem.create(
            "server.temperature", "temperature", "sysfs", "cpu", status,
            values={"temperature_c": value}, error=None if value is not None else "temperature unavailable",
        )

    def get_throttle_status(self, _args: ToolArguments) -> EvidenceItem:
        completed = self._run(["vcgencmd", "get_throttled"], timeout=2, max_bytes=256)
        output = completed[0].strip()
        value = output.partition("=")[2] if output.startswith("throttled=") else None
        status = EvidenceStatus.OK if value == "0x0" else EvidenceStatus.DEGRADED if value else EvidenceStatus.UNAVAILABLE
        return EvidenceItem.create(
            "server.throttle", "throttle_status", "vcgencmd", "cpu", status,
            values={"throttled": value}, error=completed[1],
        )

    def get_service_status(self, args: ToolArguments) -> EvidenceItem:
        service = cast(ServiceArgs, args).service
        return self._service_evidence(service)

    def get_service_summary(self, _args: ToolArguments) -> EvidenceItem:
        services = [self._service_values(name) for name in SERVICE_ALLOWLIST]
        inactive = [item["service"] for item in services if item["active_state"] != "active"]
        return EvidenceItem.create(
            "service.summary", "service_summary", "systemd", "allowlisted_services",
            EvidenceStatus.DEGRADED if inactive else EvidenceStatus.OK,
            values={"services": services, "inactive": inactive},
        )

    def read_service_logs(self, args: ToolArguments) -> EvidenceItem:
        values = cast(LogArgs, args)
        unit = SERVICE_ALLOWLIST[values.service]
        stdout, error, truncated = self._run(
            ["journalctl", "-u", unit, f"--since=-{values.minutes} min", "-n", str(values.max_lines), "--no-pager", "--output=short-iso"],
            timeout=5,
            max_bytes=8192,
            include_truncated=True,
        )
        return EvidenceItem.create(
            f"logs.service.{values.service}", "service_logs", "journalctl", unit,
            EvidenceStatus.OK if not error else EvidenceStatus.UNAVAILABLE,
            values={"minutes": values.minutes, "max_lines": values.max_lines},
            text_excerpt=stdout, error=error, truncated=truncated,
            sensitivity=Sensitivity.INTERNAL,
        )

    def get_failed_units(self, _args: ToolArguments) -> EvidenceItem:
        stdout, error, truncated = self._run(
            ["systemctl", "--failed", "--no-legend", "--plain"], timeout=3, max_bytes=4096, include_truncated=True
        )
        allowed_lines = [line for line in stdout.splitlines() if any(unit in line for unit in SERVICE_ALLOWLIST.values())]
        return EvidenceItem.create(
            "service.failed_units", "failed_units", "systemd", "allowlisted_services",
            EvidenceStatus.DEGRADED if allowed_lines else EvidenceStatus.OK,
            values={"failed": allowed_lines}, error=error, truncated=truncated,
        )

    # Network tools
    def get_network_interfaces(self, _args: ToolArguments) -> EvidenceItem:
        # sysfs enumeration keeps this observation inside the read-only
        # procfs/sysfs boundary. glibc's if_nameindex() needs an AF_NETLINK
        # socket, which the hardened unit deliberately does not grant.
        names = _list_interface_names()
        if names is None:
            return EvidenceItem.create(
                "network.interfaces", "network_interfaces", "kernel", "butters",
                EvidenceStatus.UNAVAILABLE,
                error="interface enumeration is unavailable in this sandbox",
            )
        interfaces: list[dict[str, object]] = []
        for name in names:
            state = _read_text(Path("/sys/class/net") / name / "operstate", 64)
            carrier = _read_text(Path("/sys/class/net") / name / "carrier", 8)
            interfaces.append({"name": name, "state": state.strip() if state else None, "carrier": carrier.strip() == "1" if carrier else None})
        return EvidenceItem.create(
            "network.interfaces", "network_interfaces", "kernel", "butters", EvidenceStatus.OK,
            values={"interfaces": interfaces},
        )

    def get_route_summary(self, _args: ToolArguments) -> EvidenceItem:
        routes: list[dict[str, object]] = []
        raw = _read_text(Path("/proc/net/route"), 16384) or ""
        for line in raw.splitlines()[1:33]:
            fields = line.split()
            if len(fields) >= 8:
                routes.append({"interface": fields[0], "destination": _route_hex(fields[1]), "gateway": _route_hex(fields[2]), "metric": _int_or_none(fields[6])})
        default = next((item for item in routes if item["destination"] == "0.0.0.0"), None)
        return EvidenceItem.create(
            "network.routes", "route_summary", "procfs", "butters",
            EvidenceStatus.OK if default else EvidenceStatus.DEGRADED,
            values={"default_route": default, "routes": routes},
        )

    def resolve_host(self, args: ToolArguments) -> EvidenceItem:
        alias = cast(HostArgs, args).host
        address = HOST_ALLOWLIST[alias]
        try:
            resolved = sorted({item[4][0] for item in socket.getaddrinfo(address, None)})
        except socket.gaierror as exc:
            return _error_evidence(f"network.dns.{alias}", "resolve_host", alias, "dns_failure", str(exc))
        return EvidenceItem.create(
            f"network.dns.{alias}", "host_resolution", "resolver", alias, EvidenceStatus.OK,
            values={"addresses": resolved[:8]},
        )

    def ping_allowlisted_host(self, args: ToolArguments) -> EvidenceItem:
        alias = cast(HostArgs, args).host
        stdout, error, truncated = self._run(
            ["ping", "-c", "1", "-W", "2", "--", HOST_ALLOWLIST[alias]], timeout=4, max_bytes=2048, include_truncated=True
        )
        return EvidenceItem.create(
            f"network.ping.{alias}", "ping", "ping", alias,
            EvidenceStatus.OK if not error else EvidenceStatus.DEGRADED,
            values={"reachable": not bool(error)}, text_excerpt=stdout, error=error, truncated=truncated,
        )

    def check_tcp_port(self, args: ToolArguments) -> EvidenceItem:
        endpoint = cast(EndpointArgs, args).endpoint
        host, port = ENDPOINT_ALLOWLIST[endpoint]
        started = time.perf_counter()
        try:
            connection = self.socket_connector((host, port), timeout=2.0)
            connection.close()
            reachable = True
            error = None
        except OSError as exc:
            reachable = False
            error = type(exc).__name__
        return EvidenceItem.create(
            f"network.tcp.{endpoint}", "tcp_port", "socket", endpoint,
            EvidenceStatus.OK if reachable else EvidenceStatus.DEGRADED,
            values={"reachable": reachable, "port": port, "elapsed_seconds": time.perf_counter() - started}, error=error,
        )

    def get_tailscale_status(self, _args: ToolArguments) -> EvidenceItem:
        stdout, error, truncated = self._run(["tailscale", "status", "--json"], timeout=4, max_bytes=16384, include_truncated=True)
        if error:
            return EvidenceItem.create("network.tailscale", "tailscale_status", "tailscale", "butters", EvidenceStatus.UNAVAILABLE, error=error, truncated=truncated)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return _error_evidence("network.tailscale", "tailscale", "butters", "invalid_response", "tailscale returned invalid JSON")
        peers = payload.get("Peer", {}) if isinstance(payload, dict) else {}
        values = {
            "backend_state": payload.get("BackendState") if isinstance(payload, dict) else None,
            "peer_count": len(peers) if isinstance(peers, dict) else 0,
            "online_peers": sum(bool(value.get("Online")) for value in peers.values() if isinstance(value, dict)) if isinstance(peers, dict) else 0,
        }
        return EvidenceItem.create("network.tailscale", "tailscale_status", "tailscale", "butters", EvidenceStatus.OK, values=values)

    def get_local_listeners(self, _args: ToolArguments) -> EvidenceItem:
        ports: set[int] = set()
        for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            raw = _read_text(path, 65536) or ""
            for line in raw.splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[3] == "0A":
                    try:
                        port = int(fields[1].rsplit(":", 1)[1], 16)
                    except (ValueError, IndexError):
                        continue
                    if port in LISTENER_PORT_ALLOWLIST:
                        ports.add(port)
        return EvidenceItem.create(
            "network.listeners", "local_listeners", "procfs", "allowlisted_ports", EvidenceStatus.OK,
            values={"ports": sorted(ports), "expected_ports": sorted(LISTENER_PORT_ALLOWLIST)},
        )

    # Sensor and stack tools
    def get_sensor_value(self, args: ToolArguments) -> EvidenceItem:
        values = cast(SensorValueArgs, args)
        entity = self.entities.require(values.entity)
        metric = self.metrics.require(values.metric)
        item, node = self._sensor_payload(entity)
        raw = item.get(metric.field) if item else None
        available = node.get("status") == "online" and isinstance(raw, (int, float)) and not isinstance(raw, bool)
        return EvidenceItem.create(
            f"sensor.{entity.entity_id}.value.{metric.metric_id}", "sensor_value", "dashboard_api", entity.entity_id,
            EvidenceStatus.OK if available else EvidenceStatus.DEGRADED,
            values={"metric": metric.metric_id, "value": raw if available else None, "status": node.get("status", "unknown"), "last_seen": node.get("last_seen"), "age_seconds": node.get("age_seconds")},
            age_seconds=_int_or_none(node.get("age_seconds")),
        )

    def get_sensor_status(self, args: ToolArguments) -> EvidenceItem:
        entity = self.entities.require(cast(SensorArgs, args).entity)
        _item, node = self._sensor_payload(entity)
        online = node.get("status") == "online"
        return EvidenceItem.create(
            f"sensor.{entity.entity_id}.status", "sensor_status", "dashboard_api", entity.entity_id,
            EvidenceStatus.OK if online else EvidenceStatus.DEGRADED,
            values={"status": node.get("status", "offline"), "last_seen": node.get("last_seen"), "age_seconds": node.get("age_seconds"), "stale_reason": node.get("stale_reason"), "topic": node.get("topic")},
            age_seconds=_int_or_none(node.get("age_seconds")),
        )

    def get_sensor_last_seen(self, args: ToolArguments) -> EvidenceItem:
        entity = self.entities.require(cast(SensorArgs, args).entity)
        _item, node = self._sensor_payload(entity)
        age = _int_or_none(node.get("age_seconds"))
        return EvidenceItem.create(
            f"sensor.{entity.entity_id}.last_seen", "sensor_last_seen", "dashboard_api", entity.entity_id,
            EvidenceStatus.OK if node.get("last_seen") else EvidenceStatus.UNAVAILABLE,
            values={"last_seen": node.get("last_seen"), "age_seconds": age, "status": node.get("status", "unknown")}, age_seconds=age,
        )

    def get_sensor_history_summary(self, args: ToolArguments) -> EvidenceItem:
        values = cast(SensorHistoryArgs, args)
        entity = self.entities.require(values.entity)
        query: dict[str, str] = {"range": values.range_key, "sensor_type": entity.sensor_type}
        if entity.sensor_type == "environment":
            query["node_id"] = entity.source_id
        else:
            query["location"] = entity.source_id
        payload = self.dashboard.get("/api/readings?" + urllib.parse.urlencode(query))
        timestamps = _collect_timestamps(payload)
        summaries: dict[str, dict[str, float | int | str]] = {}
        raw_series = payload.get("series")
        if isinstance(raw_series, list):
            for series in raw_series[:4]:
                if not isinstance(series, dict):
                    continue
                points = series.get("points")
                if not isinstance(points, list):
                    continue
                values_by_metric: dict[str, list[float]] = {}
                for point in points[:2048]:
                    if not isinstance(point, dict):
                        continue
                    for field, raw in point.items():
                        if field == "time" or isinstance(raw, bool) or not isinstance(raw, (int, float)):
                            continue
                        values_by_metric.setdefault(str(field), []).append(float(raw))
                for field, samples in list(values_by_metric.items())[:16]:
                    if not samples:
                        continue
                    difference = samples[-1] - samples[0]
                    summaries[field] = {
                        "count": len(samples),
                        "minimum": round(min(samples), 4),
                        "maximum": round(max(samples), 4),
                        "mean": round(sum(samples) / len(samples), 4),
                        "first": round(samples[0], 4),
                        "last": round(samples[-1], 4),
                        "difference": round(difference, 4),
                        "trend": "rising" if difference > 0 else "falling" if difference < 0 else "flat",
                    }
        return EvidenceItem.create(
            f"sensor.{entity.entity_id}.history.{values.range_key}", "sensor_history_summary", "dashboard_api", entity.entity_id,
            EvidenceStatus.OK if timestamps else EvidenceStatus.DEGRADED,
            values={
                "range": values.range_key,
                "point_timestamps_found": len(timestamps),
                "oldest": min(timestamps) if timestamps else None,
                "newest": max(timestamps) if timestamps else None,
                "metric_summaries": summaries,
                "calculation": "local deterministic min/max/mean/first/last difference",
            },
        )

    def get_air_quality(self, args: ToolArguments) -> EvidenceItem:
        entity = self.entities.require(cast(SensorArgs, args).entity)
        item, node = self._sensor_payload(entity)
        fields = {key: item.get(key) for key in ("co2", "pm25", "pm10", "voc_index", "nox_index", "temperature_c", "humidity") if item and key in item}
        fields.update({"status": node.get("status", "unknown"), "last_seen": node.get("last_seen"), "age_seconds": node.get("age_seconds")})
        return EvidenceItem.create(
            f"sensor.{entity.entity_id}.air_quality", "air_quality", "dashboard_api", entity.entity_id,
            EvidenceStatus.OK if node.get("status") == "online" else EvidenceStatus.DEGRADED,
            values=fields, age_seconds=_int_or_none(node.get("age_seconds")),
        )

    def get_mqtt_health(self, _args: ToolArguments) -> EvidenceItem:
        service = self._service_values("mosquitto")
        port = self.check_tcp_port(EndpointArgs("mqtt"))
        healthy = service.get("active_state") == "active" and port.values.get("reachable") is True
        observable = service.get("active_state") is not None or port.values.get("reachable") is True
        return EvidenceItem.create(
            "stack.mqtt", "mqtt_health", "local_system", "mosquitto",
            EvidenceStatus.OK if healthy else EvidenceStatus.DEGRADED if observable else EvidenceStatus.UNAVAILABLE,
            values={"service": service, "listener_reachable": port.values.get("reachable"), "publishing_test_performed": False},
            error=cast(str | None, service.get("inspection_error")) if not observable else None,
        )

    def inspect_allowlisted_mqtt_topic(self, args: ToolArguments) -> EvidenceItem:
        topic = cast(TopicArgs, args).topic
        payload = self.dashboard.get("/api/nodes")
        node = next((item for item in payload.get("nodes", []) if isinstance(item, dict) and item.get("topic") == topic), {})
        return EvidenceItem.create(
            "mqtt.topic." + topic.replace("/", "."), "mqtt_topic_evidence", "dashboard_api", topic,
            EvidenceStatus.OK if node.get("last_seen") else EvidenceStatus.UNAVAILABLE,
            values={"last_seen": node.get("last_seen"), "age_seconds": node.get("age_seconds"), "status": node.get("status"), "direct_broker_subscription": False, "provenance": "bridge-persisted/dashboard-observed"},
            age_seconds=_int_or_none(node.get("age_seconds")),
        )

    def get_bridge_health(self, _args: ToolArguments) -> EvidenceItem:
        return self._stack_service("bridge", "stack.bridge", "bridge_health")

    def get_dashboard_health(self, _args: ToolArguments) -> EvidenceItem:
        started = time.perf_counter()
        payload = self.dashboard.get("/api/health")
        service = self._service_values("dashboard")
        healthy = payload.get("status") == "ok" and service.get("active_state") == "active"
        return EvidenceItem.create(
            "stack.dashboard", "dashboard_health", "dashboard_api", "dashboard", EvidenceStatus.OK if healthy else EvidenceStatus.DEGRADED,
            values={"api_status": payload.get("status"), "service": service, "elapsed_seconds": time.perf_counter() - started},
        )

    def get_influx_health(self, _args: ToolArguments) -> EvidenceItem:
        return self._http_service_health("influxdb", "http://127.0.0.1:8086/health", {200})

    def get_grafana_health(self, _args: ToolArguments) -> EvidenceItem:
        return self._http_service_health("grafana", "http://127.0.0.1:3000/api/health", {200})

    def get_home_assistant_health(self, _args: ToolArguments) -> EvidenceItem:
        # An unauthenticated 401 is the expected safe liveness response.
        return self._http_service_health("home_assistant", "http://127.0.0.1:8123/api/", {200, 401}, service_name=None)

    # Docker tools (permission failure is a normal structured result on this host)
    def get_container_status(self, args: ToolArguments) -> EvidenceItem:
        name = cast(ContainerArgs, args).container
        stdout, error, truncated = self._run(
            ["docker", "inspect", "--format", "{{json .State}}", name], timeout=4, max_bytes=4096, include_truncated=True
        )
        state: object = None
        if not error:
            try:
                state = json.loads(stdout)
            except json.JSONDecodeError:
                error = "docker returned invalid JSON"
        running = isinstance(state, dict) and state.get("Running") is True
        return EvidenceItem.create(
            f"container.{name}.status", "container_status", "docker", name,
            EvidenceStatus.OK if running else EvidenceStatus.UNAVAILABLE if error else EvidenceStatus.DEGRADED,
            values={"state": state}, error=error, truncated=truncated,
        )

    def get_container_health(self, args: ToolArguments) -> EvidenceItem:
        return self.get_container_status(args)

    def read_container_logs(self, args: ToolArguments) -> EvidenceItem:
        values = cast(ContainerLogArgs, args)
        stdout, error, truncated = self._run(
            ["docker", "logs", "--since", f"{values.minutes}m", "--tail", str(values.max_lines), values.container],
            timeout=5, max_bytes=8192, include_truncated=True,
        )
        return EvidenceItem.create(
            f"container.{values.container}.logs", "container_logs", "docker", values.container,
            EvidenceStatus.OK if not error else EvidenceStatus.UNAVAILABLE,
            values={"minutes": values.minutes, "max_lines": values.max_lines}, text_excerpt=stdout, error=error, truncated=truncated,
        )

    # KR260 tools are honest placeholders until an approved transport exists.
    def kr260_unavailable(self, _args: ToolArguments) -> EvidenceItem:
        return EvidenceItem.create(
            "kr260.transport", "kr260_transport", "configuration", "kr260", EvidenceStatus.UNAVAILABLE,
            values={"transport_configured": False, "ssh": False, "serial": False, "api": False}, error=KR260_UNAVAILABLE,
        )

    # Shared internals
    def _service_values(self, service: str) -> dict[str, object]:
        unit = SERVICE_ALLOWLIST[service]
        stdout, error, _truncated = self._run(
            ["systemctl", "show", unit, "--no-pager", "--property=Id,LoadState,ActiveState,SubState,ActiveEnterTimestampUSec,NRestarts"],
            timeout=3, max_bytes=4096, include_truncated=True,
        )
        values: dict[str, object] = {"service": service, "unit": unit}
        for line in stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"Id", "LoadState", "ActiveState", "SubState", "ActiveEnterTimestampUSec", "NRestarts"}:
                values[_snake(key)] = _int_or_none(value) if key in {"ActiveEnterTimestampUSec", "NRestarts"} else value
        if error:
            values["inspection_error"] = error
        entered = values.get("active_enter_timestamp_usec")
        if isinstance(entered, int) and entered > 0:
            values["uptime_seconds"] = max(0, int(time.time() - entered / 1_000_000))
        return values

    def _service_evidence(self, service: str) -> EvidenceItem:
        values = self._service_values(service)
        state = values.get("active_state")
        return EvidenceItem.create(
            f"service.{service}", "service_status", "systemd", SERVICE_ALLOWLIST[service],
            EvidenceStatus.OK if state == "active" else EvidenceStatus.DEGRADED if state else EvidenceStatus.UNAVAILABLE,
            values=values, error=cast(str | None, values.get("inspection_error")),
        )

    def _stack_service(self, service: str, evidence_id: str, kind: str) -> EvidenceItem:
        values = self._service_values(service)
        state = values.get("active_state")
        return EvidenceItem.create(
            evidence_id, kind, "systemd", service,
            EvidenceStatus.OK if state == "active" else EvidenceStatus.DEGRADED if state else EvidenceStatus.UNAVAILABLE,
            values=values, error=cast(str | None, values.get("inspection_error")),
        )

    def _http_service_health(
        self,
        name: str,
        url: str,
        healthy_codes: set[int],
        *,
        service_name: str | None = "same",
    ) -> EvidenceItem:
        started = time.perf_counter()
        code: int | None = None
        error: str | None = None
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Butters-Diagnostics/0.6"}, method="GET")
            with self.opener(request, timeout=3.0) as response:
                code = int(getattr(response, "status", 200))
                response.read(4097)
        except urllib.error.HTTPError as exc:
            code = exc.code
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            error = type(exc).__name__
        service = None
        resolved_service = name if service_name == "same" else service_name
        if resolved_service in SERVICE_ALLOWLIST:
            service = self._service_values(cast(str, resolved_service))
        service_state = service.get("active_state") if service else None
        healthy = code in healthy_codes and service_state != "inactive" and service_state != "failed"
        return EvidenceItem.create(
            f"stack.{name}", f"{name}_health", "http", name,
            EvidenceStatus.OK if healthy else EvidenceStatus.DEGRADED if code is not None else EvidenceStatus.UNAVAILABLE,
            values={"http_status": code, "expected_statuses": sorted(healthy_codes), "elapsed_seconds": time.perf_counter() - started, "service": service}, error=error,
        )

    def _sensor_payload(self, entity: Entity) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = self.dashboard.get("/api/latest")
        items = payload.get("air_quality" if entity.sensor_type == "air_quality" else "environment", [])
        nodes = payload.get("nodes", [])
        item = next((value for value in items if isinstance(value, dict) and str(value.get("id")) == entity.source_id), {}) if isinstance(items, list) else {}
        node = next((value for value in nodes if isinstance(value, dict) and value.get("sensor_type") == entity.sensor_type and str(value.get("id")) == entity.source_id), {}) if isinstance(nodes, list) else {}
        return item, node

    def _run(
        self,
        command: list[str],
        *,
        timeout: float,
        max_bytes: int,
        include_truncated: bool = False,
    ) -> tuple[str, str | None] | tuple[str, str | None, bool]:
        try:
            completed = self.runner(command, capture_output=True, text=False, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            result: tuple[str, str | None, bool] = ("", type(exc).__name__, False)
            return result if include_truncated else result[:2]
        stdout = bytes(getattr(completed, "stdout", b"") or b"")
        stderr = bytes(getattr(completed, "stderr", b"") or b"")
        combined = stdout + (b"\n" + stderr if stderr else b"")
        truncated = len(combined) > max_bytes
        text = combined[:max_bytes].decode("utf-8", errors="replace")
        sanitized = sanitize_text(text, max_bytes=max_bytes)
        returncode = int(getattr(completed, "returncode", 0))
        error = None if returncode == 0 else (sanitized.text.strip()[:512] or f"command exited {returncode}")
        result = (sanitized.text, error, truncated or sanitized.truncated)
        return result if include_truncated else result[:2]


def build_diagnostic_registry(
    settings: AssistantSettings,
    *,
    runner: Callable[..., Any] = subprocess.run,
    opener: Callable[..., Any] = urllib.request.urlopen,
    socket_connector: Callable[..., Any] = socket.create_connection,
) -> DiagnosticToolRegistry:
    impl = DiagnosticImplementations(settings, runner=runner, opener=opener, socket_connector=socket_connector)
    registry = DiagnosticToolRegistry()
    entities = [entity.entity_id for entity in impl.entities.entities]
    air_entities = [entity.entity_id for entity in impl.entities.entities if entity.sensor_type == "air_quality"]
    topics = [
        f"home/air/{entity.source_id}" if entity.sensor_type == "air_quality" else f"home/sensors/{entity.source_id}"
        for entity in impl.entities.entities
    ]
    no_arg_tools: tuple[tuple[str, str, Implementation, float, int], ...] = (
        ("get_server_health", "Read a combined Raspberry Pi resource and allow-listed service snapshot.", impl.get_server_health, 5, 16384),
        ("get_uptime", "Read kernel uptime.", impl.get_uptime, 1, 1024),
        ("get_load", "Read one, five, and fifteen minute load averages.", impl.get_load, 1, 1024),
        ("get_memory_status", "Read total and available memory.", impl.get_memory_status, 1, 1024),
        ("get_swap_status", "Read zram/swap use and cumulative paging counters.", impl.get_swap_status, 1, 1024),
        ("get_disk_status", "Read root filesystem usage.", impl.get_disk_status, 1, 1024),
        ("get_temperature", "Read CPU thermal-zone temperature.", impl.get_temperature, 1, 1024),
        ("get_throttle_status", "Read Raspberry Pi firmware throttle flags.", impl.get_throttle_status, 3, 1024),
        ("get_service_summary", "Read status for every approved critical service.", impl.get_service_summary, 5, 16384),
        ("get_failed_units", "List failed units only when they are in the approved service set.", impl.get_failed_units, 4, 4096),
        ("get_network_interfaces", "Read bounded kernel interface/carrier state.", impl.get_network_interfaces, 2, 4096),
        ("get_route_summary", "Read a bounded kernel route summary.", impl.get_route_summary, 2, 4096),
        ("get_tailscale_status", "Read bounded Tailscale state without exposing peer addresses.", impl.get_tailscale_status, 5, 4096),
        ("get_local_listeners", "Read only approved local service listener ports.", impl.get_local_listeners, 2, 2048),
        ("get_mqtt_health", "Check Mosquitto service and TCP listener without publishing.", impl.get_mqtt_health, 6, 8192),
        ("get_bridge_health", "Check the MQTT-to-Influx bridge service.", impl.get_bridge_health, 4, 4096),
        ("get_dashboard_health", "Check dashboard service and read-only health API.", impl.get_dashboard_health, 6, 4096),
        ("get_influx_health", "Check InfluxDB service and health API.", impl.get_influx_health, 6, 4096),
        ("get_grafana_health", "Check Grafana service and health API.", impl.get_grafana_health, 6, 4096),
        ("get_home_assistant_health", "Check expected unauthenticated Home Assistant API liveness.", impl.get_home_assistant_health, 5, 4096),
    )
    for name, description, implementation, timeout, maximum in no_arg_tools:
        registry.register(_spec(name, description, "NoArgs", _empty_schema(), _parse_none, _allow, implementation, timeout, maximum))

    registry.register(_spec("get_service_status", "Read one approved systemd unit's state.", "ServiceArgs", _enum_schema("service", list(SERVICE_ALLOWLIST)), _parse_service, _allow_service, impl.get_service_status, 4, 4096))
    registry.register(_spec("read_service_logs", "Read sanitized bounded recent logs for one approved service.", "LogArgs", _log_schema("service", list(SERVICE_ALLOWLIST)), _parse_log, _allow_log, impl.read_service_logs, 7, 8192))
    registry.register(_spec("resolve_host", "Resolve one configured host alias.", "HostArgs", _enum_schema("host", list(HOST_ALLOWLIST)), _parse_host, _allow_host, impl.resolve_host, 4, 2048))
    registry.register(_spec("ping_allowlisted_host", "Send one bounded ICMP probe to one configured host alias.", "HostArgs", _enum_schema("host", list(HOST_ALLOWLIST)), _parse_host, _allow_host, impl.ping_allowlisted_host, 5, 4096))
    registry.register(_spec("check_tcp_port", "Attempt TCP connection to one fixed approved endpoint.", "EndpointArgs", _enum_schema("endpoint", list(ENDPOINT_ALLOWLIST)), _parse_endpoint, _allow_endpoint, impl.check_tcp_port, 4, 2048))

    registry.register(_spec("get_sensor_value", "Read one approved current entity metric through the dashboard API.", "SensorValueArgs", _two_enum_schema("entity", entities, "metric", [metric.metric_id for metric in impl.metrics.metrics]), _parse_sensor_value, lambda args: _allow_sensor_value(args, impl), impl.get_sensor_value, 7, 4096))
    for name, description, implementation, allowed in (
        ("get_sensor_status", "Read one configured sensor's current reporting status.", impl.get_sensor_status, entities),
        ("get_sensor_last_seen", "Read one configured sensor's latest persisted receive time.", impl.get_sensor_last_seen, entities),
        ("get_air_quality", "Read a bounded current air-quality station summary.", impl.get_air_quality, air_entities),
    ):
        registry.register(_spec(name, description, "SensorArgs", _enum_schema("entity", allowed), _parse_sensor, lambda args, allowed=frozenset(allowed): _allow_sensor(args, allowed), implementation, 7, 8192))
    registry.register(_spec("get_sensor_history_summary", "Summarize bounded existing sensor history without returning raw series.", "SensorHistoryArgs", _two_enum_schema("entity", entities, "range_key", ["1h", "24h", "7d"]), _parse_history, lambda args: _allow_history(args, frozenset(entities)), impl.get_sensor_history_summary, 10, 4096))
    registry.register(_spec("inspect_allowlisted_mqtt_topic", "Return indirect bridge-persisted activity for one approved topic; never subscribes or publishes.", "TopicArgs", _enum_schema("topic", topics), _parse_topic, lambda args: _allow_topic(args, frozenset(topics)), impl.inspect_allowlisted_mqtt_topic, 7, 4096))

    for name, description, implementation in (
        ("get_container_status", "Inspect state for one approved container.", impl.get_container_status),
        ("get_container_health", "Inspect health/state for one approved container.", impl.get_container_health),
    ):
        registry.register(_spec(name, description, "ContainerArgs", _enum_schema("container", sorted(CONTAINER_ALLOWLIST)), _parse_container, _allow_container, implementation, 6, 4096))
    registry.register(_spec("read_container_logs", "Read sanitized bounded recent logs for one approved container.", "ContainerLogArgs", _log_schema("container", sorted(CONTAINER_ALLOWLIST)), _parse_container_log, _allow_container_log, impl.read_container_logs, 7, 8192))

    for name in (
        "get_kr260_reachability", "get_kr260_network_status", "get_kr260_temperature",
        "get_kr260_storage", "get_kr260_fpga_status", "read_kr260_kernel_logs",
        "read_kr260_service_logs", "get_kr260_serial_status", "run_kr260_diagnostic",
    ):
        registry.register(_spec(name, "Report KR260 transport availability; no transport is currently configured.", "NoArgs", _empty_schema(), _parse_none, _allow, impl.kr260_unavailable, 1, 2048))
    return registry


def _spec(
    name: str,
    description: str,
    argument_type: str,
    schema: dict[str, object],
    parser: ArgumentParser,
    authorizer: Authorizer,
    implementation: Implementation,
    timeout: float,
    maximum: int,
) -> DiagnosticToolSpec:
    return DiagnosticToolSpec(
        name=name,
        description=description,
        argument_type=argument_type,
        input_schema=schema,
        output_schema={"type": "EvidenceItem", "description": "one bounded sanitized EvidenceItem."},
        action_class=ActionClass.READ_ONLY,
        timeout_seconds=timeout,
        max_output_bytes=maximum,
        parse_arguments=parser,
        authorize=authorizer,
        implementation=implementation,
    )


def _empty_schema() -> dict[str, object]:
    return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def _enum_schema(name: str, values: list[str]) -> dict[str, object]:
    return {"type": "object", "properties": {name: {"type": "string", "enum": values}}, "required": [name], "additionalProperties": False}


def _two_enum_schema(first: str, first_values: list[str], second: str, second_values: list[str]) -> dict[str, object]:
    return {"type": "object", "properties": {first: {"type": "string", "enum": first_values}, second: {"type": "string", "enum": second_values}}, "required": [first, second], "additionalProperties": False}


def _log_schema(target: str, values: list[str]) -> dict[str, object]:
    return {"type": "object", "properties": {target: {"type": "string", "enum": values}, "minutes": {"type": "integer", "minimum": 1, "maximum": 120}, "max_lines": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": [target, "minutes", "max_lines"], "additionalProperties": False}


def _parse_none(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values)
    return NoArgs()


def _parse_service(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"service"}))
    return ServiceArgs(required_string(values, "service"))


def _parse_log(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"service", "minutes", "max_lines"}))
    return LogArgs(required_string(values, "service"), _bounded_int(values, "minutes", 1, 120), _bounded_int(values, "max_lines", 1, 200))


def _parse_host(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"host"}))
    return HostArgs(required_string(values, "host"))


def _parse_endpoint(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"endpoint"}))
    return EndpointArgs(required_string(values, "endpoint"))


def _parse_sensor(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"entity"}))
    return SensorArgs(required_string(values, "entity"))


def _parse_sensor_value(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"entity", "metric"}))
    return SensorValueArgs(required_string(values, "entity"), required_string(values, "metric"))


def _parse_history(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"entity", "range_key"}))
    return SensorHistoryArgs(required_string(values, "entity"), required_string(values, "range_key"))


def _parse_topic(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"topic"}))
    return TopicArgs(required_string(values, "topic"))


def _parse_container(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"container"}))
    return ContainerArgs(required_string(values, "container"))


def _parse_container_log(values: Mapping[str, object]) -> ToolArguments:
    strict_arguments(values, required=frozenset({"container", "minutes", "max_lines"}))
    return ContainerLogArgs(required_string(values, "container"), _bounded_int(values, "minutes", 1, 120), _bounded_int(values, "max_lines", 1, 200))


def _bounded_int(values: Mapping[str, object], key: str, minimum: int, maximum: int) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise SkillError("invalid_arguments", f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _allow(_args: ToolArguments) -> None:
    return None


def _allow_service(args: ToolArguments) -> None:
    if cast(ServiceArgs, args).service not in SERVICE_ALLOWLIST:
        raise SkillError("policy_denied", "service target is not allow-listed")


def _allow_log(args: ToolArguments) -> None:
    if cast(LogArgs, args).service not in SERVICE_ALLOWLIST:
        raise SkillError("policy_denied", "service target is not allow-listed")


def _allow_host(args: ToolArguments) -> None:
    if cast(HostArgs, args).host not in HOST_ALLOWLIST:
        raise SkillError("policy_denied", "host target is not allow-listed")


def _allow_endpoint(args: ToolArguments) -> None:
    if cast(EndpointArgs, args).endpoint not in ENDPOINT_ALLOWLIST:
        raise SkillError("policy_denied", "endpoint target is not allow-listed")


def _allow_sensor(args: ToolArguments, allowed: frozenset[str]) -> None:
    if cast(SensorArgs, args).entity not in allowed:
        raise SkillError("policy_denied", "sensor entity is not allow-listed")


def _allow_sensor_value(args: ToolArguments, impl: DiagnosticImplementations) -> None:
    values = cast(SensorValueArgs, args)
    entity = impl.entities.get(values.entity)
    metric = impl.metrics.get(values.metric)
    if entity is None or metric is None or entity.sensor_type not in metric.sensor_types:
        raise SkillError("policy_denied", "sensor entity/metric is not allow-listed")


def _allow_history(args: ToolArguments, entities: frozenset[str]) -> None:
    values = cast(SensorHistoryArgs, args)
    if values.entity not in entities or values.range_key not in {"1h", "24h", "7d"}:
        raise SkillError("policy_denied", "history target/range is not allow-listed")


def _allow_topic(args: ToolArguments, topics: frozenset[str]) -> None:
    if cast(TopicArgs, args).topic not in topics:
        raise SkillError("policy_denied", "MQTT topic is not allow-listed")


def _allow_container(args: ToolArguments) -> None:
    if cast(ContainerArgs, args).container not in CONTAINER_ALLOWLIST:
        raise SkillError("policy_denied", "container is not allow-listed")


def _allow_container_log(args: ToolArguments) -> None:
    if cast(ContainerLogArgs, args).container not in CONTAINER_ALLOWLIST:
        raise SkillError("policy_denied", "container is not allow-listed")


def _bound_evidence(item: EvidenceItem, maximum: int) -> EvidenceItem:
    size = len(json.dumps(item.as_dict(), sort_keys=True, default=str).encode("utf-8"))
    if size <= maximum:
        return item
    excerpt = sanitize_text(item.text_excerpt or "", max_bytes=max(0, maximum // 2))
    return replace(
        item,
        values={"original_output_bytes": size, "output_omitted": True},
        text_excerpt=excerpt.text or None,
        error="structured output exceeded the tool limit",
        truncated=True,
    )


def _error_evidence(evidence_id: str, source: str, target: str, code: str, message: str) -> EvidenceItem:
    status = EvidenceStatus.UNAVAILABLE if code in {"timeout", "unavailable", "upstream_status"} else EvidenceStatus.ERROR
    return EvidenceItem.create(evidence_id, "tool_error", source, target, status, values={"error_code": code}, error=message)


def _target_from_arguments(values: Mapping[str, object]) -> str:
    for name in ("entity", "service", "container", "host", "endpoint", "topic"):
        value = values.get(name)
        if isinstance(value, str):
            return value[:128]
    return "none"


def _metric_evidence(evidence_id: str, kind: str, target: str, value: float | None, key: str) -> EvidenceItem:
    return EvidenceItem.create(evidence_id, kind, "procfs", target, EvidenceStatus.OK if value is not None else EvidenceStatus.UNAVAILABLE, values={key: value}, error=None if value is not None else f"{kind} unavailable")


def _list_interface_names(maximum: int = 32) -> list[str] | None:
    """Enumerate interfaces from sysfs, falling back to the netlink helper."""

    try:
        return sorted(item.name for item in Path("/sys/class/net").iterdir())[:maximum]
    except OSError:
        pass
    try:
        return [name for _index, name in socket.if_nameindex()[:maximum]]
    except OSError:
        return None


def _read_text(path: Path, maximum: int) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            return source.read(maximum)
    except OSError:
        return None


def _read_float(path: Path) -> float | None:
    raw = _read_text(path, 128)
    try:
        return float(raw.split()[0]) if raw else None
    except (ValueError, IndexError):
        return None


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    raw = _read_text(Path("/proc/meminfo"), 65536) or ""
    for line in raw.splitlines():
        key, separator, rest = line.partition(":")
        if separator and key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            try:
                values[key] = int(rest.split()[0])
            except (ValueError, IndexError):
                pass
    return values


def _vmstat() -> dict[str, int]:
    values: dict[str, int] = {}
    raw = _read_text(Path("/proc/vmstat"), 262144) or ""
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] in {"pswpin", "pswpout"}:
            values[fields[0]] = _int_or_none(fields[1]) or 0
    return values


def _kib(value: int | None) -> int | None:
    return value * 1024 if value is not None else None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _route_hex(value: str) -> str | None:
    try:
        return socket.inet_ntoa(struct.pack("<L", int(value, 16)))
    except (ValueError, OSError, struct.error):
        return None


def _snake(value: str) -> str:
    result = ""
    for index, character in enumerate(value):
        if character.isupper() and index:
            result += "_"
        result += character.lower()
    return result


def _collect_timestamps(value: object, *, limit: int = 10000) -> list[str]:
    result: list[str] = []

    def visit(item: object) -> None:
        if len(result) >= limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"time", "timestamp", "last_seen"} and isinstance(child, str):
                    result.append(child)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return result
