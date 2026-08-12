"""Bounded, expiring context for one diagnostic investigation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from butters.diagnostics.evidence import EvidenceBundle


@dataclass(slots=True)
class DiagnosticSession:
    goal: str
    created_monotonic: float = field(default_factory=time.monotonic)
    ttl_seconds: float = 900.0
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)
    completed_tools: list[tuple[str, str]] = field(default_factory=list)
    active_hypotheses: list[str] = field(default_factory=list)
    escalation_history: list[str] = field(default_factory=list)
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    stopping_reason: str | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_monotonic > self.ttl_seconds

    def require_active(self) -> None:
        if self.expired:
            raise RuntimeError("diagnostic session expired")

    def remember_tool(self, name: str, canonical_arguments: str) -> bool:
        """Return false for a repeated identical call and never add it twice."""

        self.require_active()
        key = (name, canonical_arguments)
        if key in self.completed_tools:
            return False
        self.completed_tools.append(key)
        return True
