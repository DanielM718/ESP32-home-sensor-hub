"""Truthful, independent state facets for passive observers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StateFacet:
    name: str
    value: str
    confidence: str
    observed_at: float
    age_seconds: float | None

    def safe_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    facets: tuple[StateFacet, ...]
    assembled_at: float
    incomplete: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, object]:
        return {
            "facets": [facet.safe_dict() for facet in self.facets],
            "assembled_at": self.assembled_at,
            "incomplete": list(self.incomplete),
        }
