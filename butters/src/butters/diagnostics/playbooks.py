"""Deterministic local diagnostic rules over typed evidence."""

from __future__ import annotations

from butters.diagnostics.evidence import EvidenceBundle, EvidenceItem, EvidenceStatus
from butters.diagnostics.model import (
    Confidence,
    DiagnosticAssessment,
    DiagnosticDomain,
    DiagnosticFinding,
    DiagnosticStatus,
    FindingSeverity,
    ObservationComplexity,
)
from butters.diagnostics.planner import DiagnosticPlan


class LocalDiagnosticRules:
    def assess(self, plan: DiagnosticPlan, evidence: EvidenceBundle) -> DiagnosticAssessment:
        method = getattr(self, f"_{plan.playbook}", self._unknown)
        return method(plan, evidence)

    def _sensor_not_reporting(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        if plan.target is None:
            return self._insufficient(plan, bundle, "A specific configured sensor is required.")
        sensor = _id(bundle, f"sensor.{plan.target}.status")
        mqtt = _id(bundle, "stack.mqtt")
        bridge = _id(bundle, "stack.bridge")
        if _bad(mqtt):
            return _failure(plan, bundle, "mqtt_unavailable", "MQTT broker or listener is unavailable", (mqtt,), Confidence.CONFIRMED)
        if _bad(bridge):
            return _failure(plan, bundle, "bridge_service_inactive", "home-sensor-bridge is not active", (bridge,), Confidence.CONFIRMED)
        if sensor and sensor.values.get("status") == "online":
            return _healthy(plan, bundle, "sensor_reporting", f"{plan.target} is currently reporting", (sensor,), Confidence.HIGH)
        if sensor and sensor.values.get("status") in {"stale", "offline"} and _ok(mqtt) and _ok(bridge):
            return DiagnosticAssessment(
                plan.domain,
                DiagnosticStatus.DEGRADED,
                Confidence.MODERATE,
                (_finding("sensor_upstream_stale", FindingSeverity.WARNING, "The sensor is stale while MQTT and the bridge are healthy", sensor, mqtt, bridge),),
                bundle,
                root_cause="The failure is upstream of the broker, but power/radio state is not remotely observable.",
                hypotheses=("The sensor node lost power.", "The sensor's radio link or gateway delivery failed."),
                unresolved_questions=("Whether the physical sensor has power and a working radio link.",),
                recommended_next_steps=("When physically available, inspect the node power and ESP-NOW/gateway path.",),
                escalation_required=True,
                escalation_reason="multiple upstream physical/radio causes remain and cannot be observed locally",
                observation_complexity=ObservationComplexity(True, unresolved_causes=2, missing_or_stale_evidence=True, systems_implicated=("sensor", "mqtt", "bridge")),
            )
        return self._insufficient(plan, bundle, "Sensor status could not be established from current evidence.")

    def _sensor_dashboard_pipeline(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        ordered = (
            ("stack.mqtt", "mqtt_unavailable", "MQTT is unavailable"),
            ("stack.bridge", "bridge_unavailable", "The sensor bridge is unavailable"),
            ("stack.influxdb", "influx_unavailable", "InfluxDB is unavailable"),
            ("stack.dashboard", "dashboard_unavailable", "The dashboard API is unavailable"),
        )
        for evidence_id, code, cause in ordered:
            item = _id(bundle, evidence_id)
            if _bad(item):
                return _failure(plan, bundle, code, cause, (item,), Confidence.CONFIRMED)
        sensor = _id(bundle, f"sensor.{plan.target}.status")
        if sensor and sensor.status is EvidenceStatus.DEGRADED and sensor.values.get("status") in {"stale", "offline"}:
            return _failure(plan, bundle, "sensor_stale_upstream", "The source sensor is stale before the dashboard stage", (sensor,), Confidence.HIGH)
        if sensor and all(_ok(_id(bundle, key)) for key, _code, _cause in ordered):
            return DiagnosticAssessment(
                plan.domain, DiagnosticStatus.UNKNOWN, Confidence.INSUFFICIENT,
                (_finding("reported_problem_not_reproduced", FindingSeverity.WARNING, "The current sensor pipeline observations are healthy despite the reported symptom", sensor, *(_id(bundle, key) for key, _code, _cause in ordered)),),
                bundle,
                hypotheses=("The problem is intermittent.", "The browser view may be stale while the backend is current."),
                unresolved_questions=("Whether the dashboard symptom persists at the client.",),
                recommended_next_steps=("Compare the client-visible timestamp with the dashboard API timestamp.",),
                escalation_required=True,
                escalation_reason="current evidence contradicts the reported dashboard symptom",
                observation_complexity=ObservationComplexity(False, contradictory_evidence=True, unresolved_causes=2, systems_implicated=("sensor", "mqtt", "bridge", "influxdb", "dashboard")),
            )
        return self._insufficient(plan, bundle, "The pipeline evidence is incomplete.")

    def _grafana_current_data(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        grafana = _id(bundle, "stack.grafana")
        influx = _id(bundle, "stack.influxdb")
        sensor = _id(bundle, f"sensor.{plan.target}.status")
        dashboard = _id(bundle, "stack.dashboard")
        if _bad(grafana):
            return _failure(plan, bundle, "grafana_unavailable", "Grafana is unavailable", (grafana,), Confidence.CONFIRMED)
        if _bad(influx):
            return _failure(plan, bundle, "influx_unavailable", "InfluxDB is unavailable, so Grafana cannot query current data", (influx,), Confidence.CONFIRMED)
        if sensor and sensor.status is EvidenceStatus.DEGRADED and sensor.values.get("status") in {"stale", "offline"}:
            return _failure(plan, bundle, "sensor_stale", "The source sensor is stale before data reaches Grafana", (sensor,), Confidence.HIGH)
        if _bad(dashboard):
            return _failure(plan, bundle, "current_data_layer_unavailable", "The shared current-data layer is unavailable", (dashboard,), Confidence.HIGH)
        if all(_ok(item) for item in (grafana, influx, sensor, dashboard)):
            return DiagnosticAssessment(
                plan.domain, DiagnosticStatus.UNKNOWN, Confidence.INSUFFICIENT,
                (_finding("grafana_symptom_unexplained", FindingSeverity.WARNING, "Grafana, InfluxDB, the source sensor, and dashboard are currently healthy", grafana, influx, sensor, dashboard),),
                bundle,
                hypotheses=("A Grafana panel query/time-range issue may be present.", "The problem may have been transient."),
                unresolved_questions=("Whether the affected panel datasource/query differs from the provisioned datasource.",),
                recommended_next_steps=("Inspect the affected panel query and time range without modifying Grafana.",),
                escalation_required=True,
                escalation_reason="healthy current stages contradict the reported Grafana symptom",
                observation_complexity=ObservationComplexity(False, contradictory_evidence=True, unresolved_causes=2, systems_implicated=("sensor", "influxdb", "grafana", "dashboard")),
            )
        return self._insufficient(plan, bundle, "Grafana pipeline evidence is incomplete.")

    def _home_assistant_sensor(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        sensor = _id(bundle, f"sensor.{plan.target}.status")
        ha = _id(bundle, "stack.home_assistant")
        discovery = _id(bundle, "container.home-sensor-ha-discovery.status")
        container = _id(bundle, "container.homeassistant.status")
        if sensor and sensor.status is EvidenceStatus.DEGRADED and sensor.values.get("status") in {"stale", "offline"}:
            return _failure(plan, bundle, "underlying_sensor_stale", "The underlying sensor is stale before Home Assistant", (sensor,), Confidence.HIGH)
        if _bad(ha):
            return _failure(plan, bundle, "home_assistant_unavailable", "Home Assistant is unavailable", (ha,), Confidence.HIGH)
        if discovery and discovery.status is EvidenceStatus.DEGRADED:
            return _failure(plan, bundle, "discovery_container_inactive", "The Home Assistant MQTT discovery companion is inactive", (discovery,), Confidence.HIGH)
        if _ok(sensor) and _ok(ha) and (_ok(discovery) or _ok(container)):
            return _healthy(plan, bundle, "home_assistant_path_healthy", "The underlying sensor and Home Assistant path are healthy", tuple(item for item in (sensor, ha, discovery, container) if item), Confidence.HIGH)
        return self._insufficient(plan, bundle, "Container inspection is unavailable or the Home Assistant integration state is not remotely observable.")

    def _mqtt_failure(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        mqtt = _id(bundle, "stack.mqtt")
        bridge = _id(bundle, "stack.bridge")
        if _bad(mqtt):
            return _failure(plan, bundle, "mqtt_unavailable", "Mosquitto or its listener is unavailable", (mqtt,), Confidence.CONFIRMED)
        if _bad(bridge):
            return _failure(plan, bundle, "mqtt_consumer_unavailable", "MQTT is healthy but the sensor bridge consumer is unavailable", (mqtt, bridge), Confidence.HIGH)
        if _ok(mqtt) and _ok(bridge):
            return _healthy(plan, bundle, "mqtt_path_healthy", "MQTT and the bridge consumer are healthy", (mqtt, bridge), Confidence.HIGH)
        return self._insufficient(plan, bundle, "MQTT health could not be established.")

    def _influxdb_failure(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        influx = _id(bundle, "stack.influxdb")
        bridge = _id(bundle, "stack.bridge")
        dashboard = _id(bundle, "stack.dashboard")
        if _bad(influx):
            return _failure(plan, bundle, "influx_unavailable", "InfluxDB service or API is unavailable", (influx,), Confidence.CONFIRMED)
        if _ok(influx) and _bad(bridge):
            return _failure(plan, bundle, "influx_writer_unavailable", "InfluxDB is healthy but the bridge writer is unavailable", (influx, bridge), Confidence.HIGH)
        if _ok(influx) and _ok(bridge) and _ok(dashboard):
            return _healthy(plan, bundle, "influx_path_healthy", "InfluxDB, its writer, and the dashboard reader are healthy", (influx, bridge, dashboard), Confidence.HIGH)
        return self._insufficient(plan, bundle, "InfluxDB path evidence is incomplete.")

    def _server_health(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        health = _id(bundle, "server.health")
        if health is None or health.status in {EvidenceStatus.ERROR, EvidenceStatus.UNAVAILABLE}:
            return self._insufficient(plan, bundle, "Server health data is unavailable.")
        values = health.values
        findings: list[DiagnosticFinding] = []
        load = values.get("load")
        if isinstance(load, list) and load and isinstance(load[0], (int, float)) and load[0] >= 4.0:
            findings.append(_finding("high_load", FindingSeverity.WARNING, "One-minute load is high for this four-core Pi", health))
        available = values.get("available_memory_bytes")
        if isinstance(available, int) and available < 256 * 1024 * 1024:
            findings.append(_finding("low_available_memory", FindingSeverity.ERROR, "Available RAM is below 256 MiB", health))
        swap_used = values.get("swap_used_bytes")
        if (
            isinstance(swap_used, int)
            and swap_used >= 512 * 1024 * 1024
            and isinstance(available, int)
            and available < 512 * 1024 * 1024
        ):
            findings.append(_finding("high_swap_use", FindingSeverity.WARNING, "High swap use coincides with low available RAM", health))
        disk_free = values.get("disk_free_bytes")
        disk_total = values.get("disk_total_bytes")
        if isinstance(disk_free, int) and isinstance(disk_total, int) and disk_total and disk_free / disk_total < 0.1:
            findings.append(_finding("disk_nearly_full", FindingSeverity.ERROR, "Root filesystem has less than ten percent free", health))
        temperature = values.get("temperature_c")
        if isinstance(temperature, (int, float)) and temperature >= 80:
            findings.append(_finding("high_temperature", FindingSeverity.ERROR, "CPU temperature is at least 80 C", health))
        throttled = values.get("throttled")
        try:
            throttle_flags = int(throttled, 16) if isinstance(throttled, str) else 0
        except ValueError:
            throttle_flags = 0
        if throttle_flags & 0xF:
            findings.append(_finding("thermal_throttling", FindingSeverity.ERROR, "Firmware reports a current power or thermal throttle condition", health))
        elif throttle_flags & 0xF0000:
            findings.append(_finding("historical_throttling", FindingSeverity.WARNING, "Firmware records a past power or thermal throttle condition", health))
        services = values.get("services")
        inactive = [item.get("unit") for item in services if isinstance(item, dict) and item.get("active") is False] if isinstance(services, list) else []
        if inactive:
            findings.append(_finding("allowlisted_service_inactive", FindingSeverity.ERROR, "One or more critical services are inactive", health))
        if findings:
            root = findings[0].summary
            return DiagnosticAssessment(plan.domain, DiagnosticStatus.DEGRADED, Confidence.HIGH, tuple(findings), bundle, root_cause=root, recommended_next_steps=("Inspect the cited resource or service before considering any change.",), observation_complexity=ObservationComplexity(True, systems_implicated=("server",)))
        return _healthy(plan, bundle, "server_healthy", "No configured server-health threshold is currently violated", (health,), Confidence.HIGH)

    def _network_host(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        dns = _id(bundle, f"network.dns.{plan.target}")
        ping = _id(bundle, f"network.ping.{plan.target}")
        route = _id(bundle, "network.routes")
        tcp = next((item for item in bundle.items if item.kind == "tcp_port"), None)
        if _bad(dns):
            return _failure(plan, bundle, "dns_failure", "The configured host did not resolve", (dns,), Confidence.CONFIRMED)
        if _bad(route):
            return _failure(plan, bundle, "routing_failure", "No usable default route was observed", (route,), Confidence.HIGH)
        if _bad(ping):
            return _failure(plan, bundle, "host_unreachable", "The configured host did not answer one bounded ping", (ping,), Confidence.MODERATE)
        if _ok(ping) and _bad(tcp):
            return _failure(plan, bundle, "port_unavailable", "The configured host is reachable but the approved service port is closed", (ping, tcp), Confidence.HIGH)
        if _ok(dns) and _ok(ping):
            return _healthy(plan, bundle, "host_reachable", "The configured host resolves and is reachable", (dns, ping), Confidence.HIGH)
        return self._insufficient(plan, bundle, "Host reachability evidence is incomplete.")

    def _kr260_basic_health(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        transport = _id(bundle, "kr260.transport")
        return DiagnosticAssessment(
            plan.domain, DiagnosticStatus.UNKNOWN, Confidence.INSUFFICIENT,
            (_finding("kr260_transport_unavailable", FindingSeverity.WARNING, "No approved KR260 observation transport exists", transport),),
            bundle,
            unresolved_questions=("The KR260's current reachability and health cannot be observed from this Pi.",),
            recommended_next_steps=("Define and review a least-privilege KR260 SSH, serial, or API transport before enabling remote diagnostics.",),
            escalation_required=False,
            escalation_reason="missing transport is a local evidence precondition, not a cloud reasoning problem",
            observation_complexity=ObservationComplexity(False, missing_or_stale_evidence=True, systems_implicated=("kr260",)),
        )

    def _monitoring_pipeline(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        ordered = (
            ("sensor.printer_room.status", "sensor_stale", "The SEN66 source is stale"),
            ("stack.mqtt", "mqtt_unavailable", "MQTT is unavailable"),
            ("stack.bridge", "bridge_unavailable", "The MQTT-to-Influx bridge is unavailable"),
            ("stack.influxdb", "influx_unavailable", "InfluxDB is unavailable"),
            ("stack.dashboard", "dashboard_unavailable", "The dashboard is unavailable"),
            ("stack.grafana", "grafana_unavailable", "Grafana is unavailable"),
        )
        observed: list[EvidenceItem] = []
        for evidence_id, code, cause in ordered:
            item = _id(bundle, evidence_id)
            if item:
                observed.append(item)
            if _bad(item) or (
                evidence_id.endswith(".status")
                and item
                and item.status is EvidenceStatus.DEGRADED
                and item.values.get("status") in {"stale", "offline"}
            ):
                return _failure(plan, bundle, code, cause, tuple(observed), Confidence.HIGH)
        if len(observed) == len(ordered) and all(_ok(item) for item in observed):
            return _healthy(plan, bundle, "monitoring_pipeline_healthy", "Every observed stage in the critical monitoring path is healthy", tuple(observed), Confidence.HIGH)
        return self._insufficient(plan, bundle, "One or more monitoring stages could not be observed.")

    def _unknown(self, plan: DiagnosticPlan, bundle: EvidenceBundle) -> DiagnosticAssessment:
        return self._insufficient(plan, bundle, "No deterministic diagnostic playbook matched.")

    def _insufficient(self, plan: DiagnosticPlan, bundle: EvidenceBundle, reason: str) -> DiagnosticAssessment:
        evidence = tuple(bundle.items[:3])
        return DiagnosticAssessment(
            plan.domain, DiagnosticStatus.UNKNOWN, Confidence.INSUFFICIENT,
            (_finding("insufficient_evidence", FindingSeverity.WARNING, reason, *evidence),),
            bundle,
            unresolved_questions=(reason,),
            recommended_next_steps=("Collect the missing approved evidence or escalate for analysis when cloud access is explicitly enabled.",),
            escalation_required=plan.domain not in {DiagnosticDomain.KR260, DiagnosticDomain.UNKNOWN},
            escalation_reason=reason,
            observation_complexity=ObservationComplexity(False, missing_or_stale_evidence=True, systems_implicated=tuple(plan.complexity.systems_referenced)),
        )


def _id(bundle: EvidenceBundle, evidence_id: str) -> EvidenceItem | None:
    return bundle.get(evidence_id)


def _ok(item: EvidenceItem | None) -> bool:
    return item is not None and item.status is EvidenceStatus.OK


def _bad(item: EvidenceItem | None) -> bool:
    # UNAVAILABLE means the observation itself could not be collected; it is
    # not evidence that the target failed. Internal tool errors also cannot
    # support a causal diagnosis.
    return item is not None and (
        item.status is EvidenceStatus.DEGRADED
        or (item.status is EvidenceStatus.ERROR and item.kind != "tool_error")
    )


def _finding(code: str, severity: FindingSeverity, summary: str, *items: EvidenceItem | None) -> DiagnosticFinding:
    return DiagnosticFinding(code, severity, summary, tuple(item.evidence_id for item in items if item is not None))


def _failure(plan: DiagnosticPlan, bundle: EvidenceBundle, code: str, cause: str, items: tuple[EvidenceItem | None, ...], confidence: Confidence) -> DiagnosticAssessment:
    present = tuple(item for item in items if item is not None)
    return DiagnosticAssessment(
        plan.domain,
        DiagnosticStatus.FAILED if confidence is Confidence.CONFIRMED else DiagnosticStatus.DEGRADED,
        confidence,
        (_finding(code, FindingSeverity.ERROR, cause, *present),),
        bundle,
        root_cause=cause,
        recommended_next_steps=(f"Inspect {cause.lower()} and apply any change only after explicit authorization.",),
        observation_complexity=ObservationComplexity(True, systems_implicated=tuple(plan.complexity.systems_referenced)),
    )


def _healthy(plan: DiagnosticPlan, bundle: EvidenceBundle, code: str, conclusion: str, items: tuple[EvidenceItem | None, ...], confidence: Confidence) -> DiagnosticAssessment:
    present = tuple(item for item in items if item is not None)
    return DiagnosticAssessment(
        plan.domain, DiagnosticStatus.HEALTHY, confidence,
        (_finding(code, FindingSeverity.INFO, conclusion, *present),), bundle,
        root_cause=None,
        recommended_next_steps=("No write or control action is indicated by the current evidence.",),
        observation_complexity=ObservationComplexity(True, systems_implicated=tuple(plan.complexity.systems_referenced)),
    )
