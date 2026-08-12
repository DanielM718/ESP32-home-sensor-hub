"""Offline diagnostic corpus evaluator over synthetic evidence snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from butters.config import subsystem_root
from butters.diagnostics.evidence import EvidenceBundle, EvidenceItem, EvidenceStatus
from butters.diagnostics.model import DiagnosticDomain, RequestComplexity
from butters.diagnostics.planner import DiagnosticPlan
from butters.diagnostics.playbooks import LocalDiagnosticRules


@dataclass(frozen=True, slots=True)
class DiagnosticEvaluation:
    cases: int
    local_success_rate: float
    unnecessary_cloud_escalation_rate: float
    missed_escalation_rate: float
    evidence_completeness_rate: float
    acceptable_diagnosis_rate: float
    unsupported_claim_rate: float
    tool_efficiency_rate: float
    safety_rate: float


def default_corpus_path() -> Path:
    return subsystem_root() / "benchmarks" / "diagnostics-corpus.json"


def evaluate_corpus(path: Path | None = None) -> DiagnosticEvaluation:
    payload = json.loads((path or default_corpus_path()).read_text(encoding="utf-8"))
    cases = payload["cases"]
    rules = LocalDiagnosticRules()
    local_expected = local_correct = unnecessary = missed = complete = acceptable = supported = efficient = safe = 0
    for case in cases:
        bundle = EvidenceBundle()
        for raw in case["evidence"]:
            bundle = bundle.add(
                EvidenceItem.create(
                    raw["id"], raw.get("kind", "fixture"), "fixture", raw["id"],
                    EvidenceStatus(raw["status"]), values=raw.get("values", {}),
                    text_excerpt=raw.get("text_excerpt"),
                )
            )
        plan = DiagnosticPlan(
            case["playbook"], DiagnosticDomain(case["domain"]), case.get("target"), (), RequestComplexity()
        )
        result = rules.assess(plan, bundle)
        actual_route = "cloud" if result.escalation_required else "local"
        truth = case["ground_truth"]
        expected_route = truth["route"]
        if expected_route == "local":
            local_expected += 1
            local_correct += actual_route == "local"
        unnecessary += expected_route == "local" and actual_route == "cloud"
        missed += expected_route == "cloud" and actual_route == "local"
        evidence_ids = {item.evidence_id for item in bundle.items}
        complete += set(truth["required_evidence"]) <= evidence_ids
        codes = {finding.code for finding in result.findings}
        acceptable += bool(codes & set(truth["acceptable_diagnosis_codes"]))
        supported += all(set(finding.evidence_ids) <= evidence_ids for finding in result.findings)
        efficient += len(bundle.items) <= int(truth["max_tool_calls"])
        safe += not bool(codes & set(truth["unacceptable_diagnosis_codes"]))
    count = len(cases)
    cloud_expected = sum(case["ground_truth"]["route"] == "cloud" for case in cases)
    return DiagnosticEvaluation(
        count,
        local_correct / local_expected if local_expected else 1.0,
        unnecessary / local_expected if local_expected else 0.0,
        missed / cloud_expected if cloud_expected else 0.0,
        complete / count,
        acceptable / count,
        1.0 - supported / count,
        efficient / count,
        safe / count,
    )
