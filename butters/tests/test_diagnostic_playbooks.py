from __future__ import annotations

from types import SimpleNamespace

from butters.assistant_config import load_assistant_settings
from butters.diagnostics.engine import DiagnosticEngine
from butters.diagnostics.evidence import EvidenceBundle, EvidenceItem, EvidenceStatus
from butters.diagnostics.model import (
    Confidence,
    DiagnosticDomain,
    DiagnosticRequest,
    DiagnosticStatus,
    RequestComplexity,
)
from butters.diagnostics.planner import DiagnosticPlan, DiagnosticPlanner
from butters.diagnostics.playbooks import LocalDiagnosticRules
from butters.routing.entities import EntityRegistry


def _item(
    evidence_id: str,
    status: EvidenceStatus,
    *,
    values: dict[str, object] | None = None,
    kind: str = "fixture",
    text: str | None = None,
) -> EvidenceItem:
    return EvidenceItem.create(
        evidence_id, kind, "fixture", evidence_id, status,
        values=values or {}, text_excerpt=text,
    )


def _bundle(*items: EvidenceItem) -> EvidenceBundle:
    return EvidenceBundle().extend(items)


def _plan(playbook: str, domain: DiagnosticDomain, target: str | None = None) -> DiagnosticPlan:
    return DiagnosticPlan(playbook, domain, target, (), RequestComplexity())


def test_sensor_bridge_failure_is_solved_locally() -> None:
    assessment = LocalDiagnosticRules().assess(
        _plan("sensor_not_reporting", DiagnosticDomain.SENSOR, "filament_box_3"),
        _bundle(
            _item("sensor.filament_box_3.status", EvidenceStatus.DEGRADED, values={"status": "stale"}),
            _item("stack.mqtt", EvidenceStatus.OK),
            _item("stack.bridge", EvidenceStatus.DEGRADED, values={"active_state": "inactive"}),
        ),
    )

    assert assessment.status is DiagnosticStatus.FAILED
    assert assessment.confidence is Confidence.CONFIRMED
    assert assessment.root_cause == "home-sensor-bridge is not active"
    assert not assessment.escalation_required


def test_stale_sensor_with_healthy_pipeline_remains_honestly_incomplete() -> None:
    assessment = LocalDiagnosticRules().assess(
        _plan("sensor_not_reporting", DiagnosticDomain.SENSOR, "filament_box_3"),
        _bundle(
            _item("sensor.filament_box_3.status", EvidenceStatus.DEGRADED, values={"status": "stale"}),
            _item("stack.mqtt", EvidenceStatus.OK),
            _item("stack.bridge", EvidenceStatus.OK),
        ),
    )

    assert assessment.confidence is Confidence.MODERATE
    assert assessment.escalation_required
    assert len(assessment.hypotheses) == 2
    assert "power/radio" in (assessment.root_cause or "")


def test_dashboard_pipeline_identifies_first_known_failed_stage() -> None:
    assessment = LocalDiagnosticRules().assess(
        _plan("sensor_dashboard_pipeline", DiagnosticDomain.SENSOR_PIPELINE, "printer_room"),
        _bundle(
            _item("sensor.printer_room.status", EvidenceStatus.OK, values={"status": "online"}),
            _item("stack.mqtt", EvidenceStatus.OK),
            _item("stack.bridge", EvidenceStatus.DEGRADED),
            _item("stack.influxdb", EvidenceStatus.DEGRADED),
            _item("stack.dashboard", EvidenceStatus.DEGRADED),
        ),
    )

    assert assessment.root_cause == "The sensor bridge is unavailable"
    assert assessment.findings[0].evidence_ids == ("stack.bridge",)


def test_grafana_and_influx_known_failures_are_local() -> None:
    rules = LocalDiagnosticRules()
    grafana = rules.assess(
        _plan("grafana_current_data", DiagnosticDomain.GRAFANA, "printer_room"),
        _bundle(_item("stack.grafana", EvidenceStatus.DEGRADED)),
    )
    influx = rules.assess(
        _plan("influxdb_failure", DiagnosticDomain.INFLUXDB),
        _bundle(_item("stack.influxdb", EvidenceStatus.DEGRADED)),
    )

    assert grafana.findings[0].code == "grafana_unavailable"
    assert influx.findings[0].code == "influx_unavailable"
    assert not grafana.escalation_required and not influx.escalation_required


def test_healthy_current_grafana_evidence_contradicts_report_and_escalates() -> None:
    assessment = LocalDiagnosticRules().assess(
        _plan("grafana_current_data", DiagnosticDomain.GRAFANA, "printer_room"),
        _bundle(
            _item("stack.grafana", EvidenceStatus.OK),
            _item("stack.influxdb", EvidenceStatus.OK),
            _item("sensor.printer_room.status", EvidenceStatus.OK, values={"status": "online"}),
            _item("stack.dashboard", EvidenceStatus.OK),
        ),
    )

    assert assessment.confidence is Confidence.INSUFFICIENT
    assert assessment.escalation_required
    assert assessment.observation_complexity and assessment.observation_complexity.contradictory_evidence


def test_home_assistant_separates_source_and_integration_failure() -> None:
    rules = LocalDiagnosticRules()
    source = rules.assess(
        _plan("home_assistant_sensor", DiagnosticDomain.HOME_ASSISTANT, "filament_box_1"),
        _bundle(_item("sensor.filament_box_1.status", EvidenceStatus.DEGRADED, values={"status": "stale"})),
    )
    integration = rules.assess(
        _plan("home_assistant_sensor", DiagnosticDomain.HOME_ASSISTANT, "filament_box_1"),
        _bundle(
            _item("sensor.filament_box_1.status", EvidenceStatus.OK, values={"status": "online"}),
            _item("stack.home_assistant", EvidenceStatus.OK),
            _item("container.home-sensor-ha-discovery.status", EvidenceStatus.DEGRADED),
        ),
    )

    assert source.findings[0].code == "underlying_sensor_stale"
    assert integration.findings[0].code == "discovery_container_inactive"


def test_server_health_rules_cover_resource_and_service_thresholds() -> None:
    assessment = LocalDiagnosticRules().assess(
        _plan("server_health", DiagnosticDomain.SERVER, "butters"),
        _bundle(
            _item(
                "server.health",
                EvidenceStatus.DEGRADED,
                values={
                    "load": [8.0, 6.0, 4.0],
                    "available_memory_bytes": 100_000_000,
                    "disk_free_bytes": 1,
                    "disk_total_bytes": 100,
                    "temperature_c": 84.0,
                    "throttled": "0x50005",
                    "services": [{"unit": "home-sensor-dashboard.service", "active": False}],
                },
            )
        ),
    )

    codes = {finding.code for finding in assessment.findings}
    assert {"high_load", "low_available_memory", "disk_nearly_full", "high_temperature", "thermal_throttling", "allowlisted_service_inactive"} <= codes
    assert assessment.confidence is Confidence.HIGH


def test_server_health_decodes_historical_soft_temperature_flag_exactly() -> None:
    assessment = LocalDiagnosticRules().assess(
        _plan("server_health", DiagnosticDomain.SERVER, "butters"),
        _bundle(
            _item(
                "server.health",
                EvidenceStatus.DEGRADED,
                values={
                    "load": [0.5, 0.5, 0.5],
                    "available_memory_bytes": 1_000_000_000,
                    "swap_used_bytes": 0,
                    "disk_free_bytes": 90,
                    "disk_total_bytes": 100,
                    "temperature_c": 69.0,
                    "throttled": "0x80000",
                    "services": [],
                },
            )
        ),
    )

    finding = next(item for item in assessment.findings if item.code == "historical_throttling")
    assert "soft-temperature limit" in finding.summary
    assert "no current condition bits set" in finding.summary
    assert "undervoltage" not in finding.summary


def test_network_rules_separate_dns_routing_and_host_failures() -> None:
    rules = LocalDiagnosticRules()
    dns = rules.assess(
        _plan("network_host", DiagnosticDomain.NETWORK, "butters"),
        _bundle(_item("network.dns.butters", EvidenceStatus.ERROR, kind="host_resolution")),
    )
    route = rules.assess(
        _plan("network_host", DiagnosticDomain.NETWORK, "butters"),
        _bundle(
            _item("network.dns.butters", EvidenceStatus.OK),
            _item("network.routes", EvidenceStatus.DEGRADED),
        ),
    )
    host = rules.assess(
        _plan("network_host", DiagnosticDomain.NETWORK, "butters"),
        _bundle(
            _item("network.dns.butters", EvidenceStatus.OK),
            _item("network.routes", EvidenceStatus.OK),
            _item("network.ping.butters", EvidenceStatus.DEGRADED),
        ),
    )

    assert dns.findings[0].code == "dns_failure"
    assert route.findings[0].code == "routing_failure"
    assert host.findings[0].code == "host_unreachable"


def test_unobservable_tool_result_is_not_misdiagnosed_as_target_failure() -> None:
    assessment = LocalDiagnosticRules().assess(
        _plan("mqtt_failure", DiagnosticDomain.MQTT),
        _bundle(
            _item("stack.mqtt", EvidenceStatus.UNAVAILABLE, values={"inspection_error": "permission denied"}),
            _item("stack.bridge", EvidenceStatus.UNAVAILABLE),
        ),
    )

    assert assessment.status is DiagnosticStatus.UNKNOWN
    assert assessment.confidence is Confidence.INSUFFICIENT
    assert assessment.root_cause is None
    assert assessment.escalation_required


def test_kr260_missing_transport_is_not_pointlessly_sent_to_cloud() -> None:
    assessment = LocalDiagnosticRules().assess(
        _plan("kr260_basic_health", DiagnosticDomain.KR260, "kr260"),
        _bundle(_item("kr260.transport", EvidenceStatus.UNAVAILABLE, values={"transport_configured": False})),
    )

    assert assessment.confidence is Confidence.INSUFFICIENT
    assert not assessment.escalation_required
    assert "transport" in assessment.findings[0].code


class FixtureTools:
    def __init__(self, evidence: dict[str, EvidenceItem]) -> None:
        self.evidence = evidence

    def execute(self, name: str, _arguments: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(evidence=self.evidence[name])


def test_engine_cloud_absence_returns_best_local_evidence() -> None:
    settings = load_assistant_settings()
    planner = DiagnosticPlanner(EntityRegistry(settings.entities))
    request = DiagnosticRequest(
        "why is grafana not showing current SEN66 data",
        DiagnosticDomain.GRAFANA,
        "printer_room",
        allow_cloud=True,
    )
    tools = FixtureTools(
        {
            "get_grafana_health": _item("stack.grafana", EvidenceStatus.OK),
            "get_influx_health": _item("stack.influxdb", EvidenceStatus.OK),
            "get_sensor_status": _item("sensor.printer_room.status", EvidenceStatus.OK, values={"status": "online"}),
            "get_dashboard_health": _item("stack.dashboard", EvidenceStatus.OK),
        }
    )

    answer = DiagnosticEngine(planner, tools).diagnose(request)  # type: ignore[arg-type]

    assert answer.route == "cloud_escalation_pending"
    assert not answer.cloud_used
    assert answer.stopping_reason == "cloud_disabled_best_local_result"
    assert "OBSERVED" in answer.detailed_text
    assert "CONCLUDED" in answer.detailed_text
    assert "POSSIBLE" in answer.detailed_text
    assert "RECOMMENDED NEXT STEP" in answer.detailed_text
