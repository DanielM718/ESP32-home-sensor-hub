"""Ground-truth corpus loading and model-independent proposal scoring."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from butters.llm.model import ProposalKind, ToolProposal


@dataclass(frozen=True, slots=True)
class CorpusCase:
    case_id: str
    category: str
    text: str
    expected_path: Literal[
        "deterministic", "llm_fallback", "clarification", "unsupported"
    ]
    expected_outcome: Literal["tool", "clarification", "unsupported"]
    expected_skill: str | None = None
    expected_arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProposalScore:
    outcome_correct: bool
    skill_correct: bool
    arguments_correct: bool
    full_correct: bool


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    cases: int
    outcome_correct: int
    skill_correct: int
    arguments_correct: int
    full_correct: int
    clarifications: int
    clarification_correct: int
    unsupported: int
    unsupported_correct: int
    invalid_proposals: int
    hallucinated_skills: int
    invalid_entities: int
    invalid_metrics: int
    policy_denied: int
    category_counts: dict[str, int]


def load_corpus(path: Path) -> tuple[CorpusCase, ...]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load LLM corpus: {exc}") from exc
    if not isinstance(values, list) or not values:
        raise ValueError("LLM corpus must be a non-empty JSON array")
    cases = tuple(_case(value, index) for index, value in enumerate(values))
    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("LLM corpus case IDs must be unique")
    return cases


def score_proposal(case: CorpusCase, proposal: ToolProposal) -> ProposalScore:
    actual_outcome = {
        ProposalKind.TOOL: "tool",
        ProposalKind.CLARIFICATION: "clarification",
        ProposalKind.UNSUPPORTED: "unsupported",
        ProposalKind.INVALID: "invalid",
    }[proposal.kind]
    outcome = actual_outcome == case.expected_outcome
    if case.expected_outcome != "tool":
        return ProposalScore(outcome, outcome, outcome, outcome)
    skill = proposal.kind is ProposalKind.TOOL and proposal.skill == case.expected_skill
    arguments = (
        proposal.kind is ProposalKind.TOOL
        and proposal.arguments == case.expected_arguments
    )
    return ProposalScore(outcome, skill, arguments, outcome and skill and arguments)


class MetricsAccumulator:
    def __init__(self, *, entities: frozenset[str], metrics: frozenset[str], skills: frozenset[str]) -> None:
        self.entities = entities
        self.metrics = metrics
        self.skills = skills
        self.rows: list[tuple[CorpusCase, ToolProposal, ProposalScore, bool]] = []

    def add(
        self,
        case: CorpusCase,
        proposal: ToolProposal,
        *,
        policy_denied: bool = False,
    ) -> ProposalScore:
        score = score_proposal(case, proposal)
        self.rows.append((case, proposal, score, policy_denied))
        return score

    def finish(self) -> EvaluationMetrics:
        categories = Counter(case.category for case, *_rest in self.rows)
        clarifications = [row for row in self.rows if row[0].expected_outcome == "clarification"]
        unsupported = [row for row in self.rows if row[0].expected_outcome == "unsupported"]
        return EvaluationMetrics(
            cases=len(self.rows),
            outcome_correct=sum(row[2].outcome_correct for row in self.rows),
            skill_correct=sum(row[2].skill_correct for row in self.rows),
            arguments_correct=sum(row[2].arguments_correct for row in self.rows),
            full_correct=sum(row[2].full_correct for row in self.rows),
            clarifications=len(clarifications),
            clarification_correct=sum(row[2].full_correct for row in clarifications),
            unsupported=len(unsupported),
            unsupported_correct=sum(row[2].full_correct for row in unsupported),
            invalid_proposals=sum(
                proposal.kind is ProposalKind.INVALID
                for _case_value, proposal, _score, _denied in self.rows
            ),
            hallucinated_skills=sum(
                proposal.kind is ProposalKind.TOOL
                and proposal.skill not in self.skills
                for _case_value, proposal, _score, _denied in self.rows
            ),
            invalid_entities=sum(
                isinstance(proposal.arguments.get("entity"), str)
                and proposal.arguments["entity"] not in self.entities
                for _case_value, proposal, _score, _denied in self.rows
            ),
            invalid_metrics=sum(
                isinstance(proposal.arguments.get("metric"), str)
                and proposal.arguments["metric"] not in self.metrics
                for _case_value, proposal, _score, _denied in self.rows
            ),
            policy_denied=sum(denied for *_rest, denied in self.rows),
            category_counts=dict(sorted(categories.items())),
        )


def _case(value: Any, index: int) -> CorpusCase:
    if not isinstance(value, dict):
        raise ValueError(f"LLM corpus case {index} must be an object")
    required = {"id", "category", "text", "path", "outcome"}
    missing = required - set(value)
    unexpected = set(value) - required - {"skill", "arguments"}
    if missing or unexpected:
        raise ValueError(
            f"LLM corpus case {index} has missing={sorted(missing)} "
            f"unexpected={sorted(unexpected)}"
        )
    path = value["path"]
    outcome = value["outcome"]
    if path not in {"deterministic", "llm_fallback", "clarification", "unsupported"}:
        raise ValueError(f"LLM corpus case {index} has invalid path")
    if outcome not in {"tool", "clarification", "unsupported"}:
        raise ValueError(f"LLM corpus case {index} has invalid outcome")
    arguments = value.get("arguments", {})
    if not isinstance(arguments, dict) or not all(
        isinstance(key, str) for key in arguments
    ):
        raise ValueError(f"LLM corpus case {index} arguments must be an object")
    skill = value.get("skill")
    if outcome == "tool" and not isinstance(skill, str):
        raise ValueError(f"LLM corpus case {index} tool outcome requires skill")
    return CorpusCase(
        str(value["id"]),
        str(value["category"]),
        str(value["text"]),
        path,
        outcome,
        skill if isinstance(skill, str) else None,
        arguments,
    )
