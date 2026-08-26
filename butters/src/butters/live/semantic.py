"""Router-aware endpoint decisions with no execution or model calls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from butters.routing.conversation import route_conversation_turn
from butters.routing.model import PendingClarification, RoutedIntent
from butters.routing.router import IntentRouter


@dataclass(frozen=True, slots=True)
class SemanticEndpointAssessment:
    status: Literal["complete", "incomplete", "unrecognized"]
    route: RoutedIntent
    effective_text: str
    continued: bool = False
    pending: PendingClarification | None = None


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
    """Preview routing and fill only a known structured pending slot."""

    def __init__(
        self,
        preview_route: Callable[[str], RoutedIntent],
        *,
        router: IntentRouter | None = None,
    ) -> None:
        self.preview_route = preview_route
        owner = getattr(preview_route, "__self__", None)
        inferred = owner if isinstance(owner, IntentRouter) else getattr(owner, "router", None)
        self.router = router or inferred
        if not isinstance(self.router, IntentRouter):
            raise TypeError("structured semantic continuation requires an IntentRouter")

    def assess(
        self,
        text: str,
        *,
        pending: PendingClarification | None = None,
    ) -> SemanticEndpointAssessment:
        current = text.strip()
        outcome = route_conversation_turn(
            self.router,
            current,
            pending,
            now=0.0,
            ttl_seconds=float("inf"),
            preview_route=self.preview_route,
        )
        status: Literal["complete", "incomplete", "unrecognized"]
        if outcome.route.matched:
            status = "complete"
        elif outcome.route.incomplete:
            status = "incomplete"
        else:
            status = "unrecognized"
        return SemanticEndpointAssessment(
            status,
            outcome.route,
            current,
            continued=outcome.disposition in {"resolved", "retry"},
            pending=outcome.pending,
        )
