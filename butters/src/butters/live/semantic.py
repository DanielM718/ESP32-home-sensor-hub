"""Router-aware endpoint decisions with no execution or model calls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from butters.routing.model import RoutedIntent


@dataclass(frozen=True, slots=True)
class SemanticEndpointAssessment:
    status: Literal["complete", "incomplete", "unrecognized"]
    route: RoutedIntent
    effective_text: str
    continued: bool = False


def incomplete_route_progressed(
    previous: RoutedIntent,
    candidate: RoutedIntent,
) -> bool:
    """Return true only when a continuation resolves or narrows known slots."""

    return candidate.incomplete and (
        candidate.arguments != previous.arguments
        or candidate.missing_arguments != previous.missing_arguments
        or candidate.message != previous.message
    )


class SemanticEndpointEvaluator:
    """Preview deterministic routing and, only for known pending requests, merge."""

    def __init__(self, preview_route: Callable[[str], RoutedIntent]) -> None:
        self.preview_route = preview_route

    def assess(
        self,
        text: str,
        *,
        pending_text: str | None = None,
    ) -> SemanticEndpointAssessment:
        current = text.strip()
        standalone = self.preview_route(current)
        if standalone.matched:
            return SemanticEndpointAssessment("complete", standalone, current)

        if pending_text:
            pending_route = self.preview_route(pending_text)
            merged = f"{pending_text.strip()} {current}".strip()
            merged_route = self.preview_route(merged)
            if merged_route.matched:
                return SemanticEndpointAssessment(
                    "complete", merged_route, merged, continued=True
                )
            if incomplete_route_progressed(pending_route, merged_route):
                return SemanticEndpointAssessment(
                    "incomplete", merged_route, merged, continued=True
                )

        if standalone.incomplete:
            return SemanticEndpointAssessment("incomplete", standalone, current)
        return SemanticEndpointAssessment("unrecognized", standalone, current)
