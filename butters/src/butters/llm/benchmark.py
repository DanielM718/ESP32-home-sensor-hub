"""Evaluate a manually started localhost llama.cpp worker against the corpus."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict
from pathlib import Path

from butters.assistant_config import load_assistant_settings
from butters.config import subsystem_root
from butters.llm.catalog import (
    build_tool_catalog,
    entity_alias_summary,
    metric_alias_summary,
)
from butters.llm.evaluation import MetricsAccumulator, load_corpus
from butters.llm.llama_server import LlamaCppServerLanguageModel
from butters.llm.model import LanguageModelError, ProposalKind, ToolProposal
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.routing.router import IntentRouter
from butters.skills.implementations import build_read_only_registry


class _NoSensorAccess:
    def snapshot(self):  # pragma: no cover - a benchmark invariant
        raise AssertionError("proposal validation must not query sensor data")


class _NoServerAccess:
    def snapshot(self):  # pragma: no cover - a benchmark invariant
        raise AssertionError("proposal validation must not query server data")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score a loopback llama.cpp worker without executing skills"
    )
    parser.add_argument("--server", default="http://127.0.0.1:18080")
    parser.add_argument("--model", default="butters-router")
    parser.add_argument("--profile", choices=("generic", "lfm2", "qwen3"), default="lfm2")
    parser.add_argument(
        "--output-mode",
        choices=("native_tools", "json_schema"),
        default="native_tools",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=subsystem_root() / "benchmarks" / "llm-corpus.json",
    )
    parser.add_argument("--assistant-config", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--show-failures", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_assistant_settings(args.assistant_config)
    entities = EntityRegistry(settings.entities)
    metrics = MetricRegistry()
    router = IntentRouter(entities, metrics)
    tools = build_tool_catalog(entities, metrics)
    context = (
        *(f"entity {item}" for item in entity_alias_summary(entities)),
        *(f"metric {item}" for item in metric_alias_summary(metrics)),
    )
    registry = build_read_only_registry(
        _NoSensorAccess(),  # type: ignore[arg-type]
        _NoServerAccess(),  # type: ignore[arg-type]
        entities,
        metrics,
    )
    model = LlamaCppServerLanguageModel(
        args.server,
        args.model,
        profile=args.profile,
        output_mode=args.output_mode,
        timeout_seconds=args.timeout,
        context_hints=context,
    )
    cases = load_corpus(args.corpus)
    if args.limit:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        cases = cases[: args.limit]
    accumulator = MetricsAccumulator(
        entities=frozenset(entity.entity_id for entity in entities.entities),
        metrics=frozenset(metric.metric_id for metric in metrics.metrics),
        skills=frozenset(spec.name for spec in registry.skills),
    )
    latencies: list[float] = []
    prompt_rates: list[float] = []
    generation_rates: list[float] = []
    path_correct = 0
    deterministic_cases = 0
    deterministic_bypasses = 0
    model_invocations = 0
    model_failures = 0
    false_confident_actions = 0
    rows: list[dict[str, object]] = []
    started = time.perf_counter()
    for case in cases:
        route = router.route(case.text)
        if route.matched and route.skill:
            actual_path = "deterministic"
            proposal = ToolProposal(ProposalKind.TOOL, route.skill, route.arguments)
            deterministic_cases += case.expected_path == "deterministic"
            deterministic_bypasses += case.expected_path == "deterministic"
        elif route.status == "clarification" and not route.allow_fallback:
            actual_path = "clarification"
            proposal = ToolProposal(ProposalKind.CLARIFICATION)
        elif not route.allow_fallback:
            actual_path = "unsupported"
            proposal = ToolProposal(ProposalKind.UNSUPPORTED)
        else:
            actual_path = "llm_fallback"
            model_invocations += 1
            try:
                result = model.propose_tools(case.text, tools)
                proposal = result.proposal
                latencies.append(result.elapsed_seconds)
                if result.prompt_tokens_per_second is not None:
                    prompt_rates.append(result.prompt_tokens_per_second)
                if result.generated_tokens_per_second is not None:
                    generation_rates.append(result.generated_tokens_per_second)
            except LanguageModelError as exc:
                proposal = ToolProposal(ProposalKind.INVALID, error=type(exc).__name__)
                model_failures += 1
        path_ok = actual_path == case.expected_path
        path_correct += path_ok
        failure = None
        if proposal.kind is ProposalKind.TOOL and proposal.skill is not None:
            failure = registry.validate_proposal(proposal.skill, proposal.arguments)
        score = accumulator.add(case, proposal, policy_denied=failure is not None)
        if case.expected_outcome in {"clarification", "unsupported"} and proposal.kind is ProposalKind.TOOL:
            false_confident_actions += 1
        row = {
            "id": case.case_id,
            "category": case.category,
            "expected_path": case.expected_path,
            "actual_path": actual_path,
            "expected_outcome": case.expected_outcome,
            "proposal": asdict(proposal),
            "path_correct": path_ok,
            "full_correct": score.full_correct and path_ok,
            "policy_failure": asdict(failure) if failure else None,
        }
        rows.append(row)
        if args.show_failures and not row["full_correct"]:
            print(json.dumps(row, sort_keys=True), flush=True)

    metrics = accumulator.finish()
    report = {
        "model": args.model,
        "profile": args.profile,
        "output_mode": args.output_mode,
        "corpus_cases": len(cases),
        "model_invocations": model_invocations,
        "model_failures": model_failures,
        "path_correct": path_correct,
        "deterministic_cases": deterministic_cases,
        "deterministic_bypasses": deterministic_bypasses,
        "false_confident_actions": false_confident_actions,
        "mean_proposal_seconds": statistics.fmean(latencies) if latencies else None,
        "p95_proposal_seconds": _percentile(latencies, 0.95),
        "mean_prompt_tokens_per_second": statistics.fmean(prompt_rates) if prompt_rates else None,
        "mean_generation_tokens_per_second": statistics.fmean(generation_rates) if generation_rates else None,
        "wall_seconds": time.perf_counter() - started,
        "metrics": asdict(metrics),
        "rows": rows,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
