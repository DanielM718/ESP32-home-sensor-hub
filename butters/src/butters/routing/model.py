"""Typed deterministic routing results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class RoutedIntent:
    status: Literal["matched", "clarification", "unsupported"]
    normalized_text: str
    skill: str | None = None
    arguments: dict[str, object] = field(default_factory=dict)
    confidence: float = 0.0
    message: str | None = None
    allow_fallback: bool = False
    missing_arguments: tuple[str, ...] = ()
    aggregate: bool = False
    ambiguity_candidates: tuple[str, ...] = ()
    action_plan: tuple[tuple[str, dict[str, object]], ...] = ()

    @property
    def matched(self) -> bool:
        return self.status == "matched" and self.skill is not None

    @property
    def incomplete(self) -> bool:
        """Whether a known skill is blocked only by required arguments."""

        return (
            self.status == "clarification"
            and self.skill is not None
            and bool(self.missing_arguments)
        )


@dataclass(frozen=True, slots=True)
class PendingClarification:
    """Structured, expiring state for one deterministic missing-slot turn."""

    original_text: str
    normalized_text: str
    skill: str | None
    arguments: dict[str, object]
    aggregate: bool
    missing_argument: str
    ambiguity_candidates: tuple[str, ...]
    created_monotonic: float
    expires_monotonic: float
