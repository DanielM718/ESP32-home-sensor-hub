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
