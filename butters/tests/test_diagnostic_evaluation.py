from __future__ import annotations

import json

from butters.diagnostics.evaluation import default_corpus_path, evaluate_corpus


def test_diagnostic_corpus_has_all_required_scenario_categories_and_ground_truth() -> None:
    payload = json.loads(default_corpus_path().read_text(encoding="utf-8"))
    categories = {case["category"] for case in payload["cases"]}

    assert len(payload["cases"]) == 17
    assert {
        "obvious_single_known_failure", "sensor_stale", "mqtt_unavailable", "bridge_failed",
        "influx_unavailable", "grafana_unavailable", "home_assistant_integration",
        "memory_swap_pressure", "thermal_throttling", "host_unreachable", "port_unavailable",
        "contradictory_evidence", "unknown_log_message", "multiple_simultaneous_failures",
        "incomplete_evidence", "adversarial_log_content", "kr260_connectivity",
    } == categories
    for case in payload["cases"]:
        truth = case["ground_truth"]
        assert truth["route"] in {"local", "cloud"}
        assert truth["required_evidence"]
        assert truth["acceptable_diagnosis_codes"]
        assert truth["unacceptable_diagnosis_codes"]


def test_offline_corpus_routes_and_diagnoses_all_fixtures_as_expected() -> None:
    result = evaluate_corpus()

    assert result.cases == 17
    assert result.local_success_rate == 1.0
    assert result.unnecessary_cloud_escalation_rate == 0.0
    assert result.missed_escalation_rate == 0.0
    assert result.evidence_completeness_rate == 1.0
    assert result.acceptable_diagnosis_rate == 1.0
    assert result.unsupported_claim_rate == 0.0
    assert result.tool_efficiency_rate == 1.0
    assert result.safety_rate == 1.0
