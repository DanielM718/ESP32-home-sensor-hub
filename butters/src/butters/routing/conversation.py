"""Session-neutral structured deterministic clarification routing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from butters.routing.model import PendingClarification, RoutedIntent
from butters.routing.normalization import normalize_request
from butters.routing.router import IntentRouter


@dataclass(frozen=True, slots=True)
class ConversationalRoute:
    route: RoutedIntent
    pending: PendingClarification | None
    disposition: Literal[
        "standalone",
        "started",
        "resolved",
        "retry",
        "replaced",
        "expired",
    ]


def route_conversation_turn(
    router: IntentRouter,
    text: str,
    pending: PendingClarification | None,
    *,
    now: float,
    ttl_seconds: float,
    preview_route: Callable[[str], RoutedIntent] | None = None,
) -> ConversationalRoute:
    """Route one turn, giving complete standalone requests absolute priority."""

    if pending is not None and now >= pending.expires_monotonic:
        pending = None
        expired = True
    else:
        expired = False
    standalone = (preview_route or router.route)(text)
    if pending is None:
        created = pending_from_route(
            standalone, text, now=now, ttl_seconds=ttl_seconds
        )
        return ConversationalRoute(
            standalone,
            created,
            "started" if created is not None else "expired" if expired else "standalone",
        )

    if standalone.matched:
        return ConversationalRoute(standalone, None, "replaced")

    contextual = router.continue_clarification(pending, text)
    if contextual.matched or contextual.status == "unsupported":
        return ConversationalRoute(contextual, None, "resolved")

    normalized = normalize_request(text)
    if _clearly_new_incomplete(standalone, pending, normalized):
        replacement = pending_from_route(
            standalone, text, now=now, ttl_seconds=ttl_seconds
        )
        return ConversationalRoute(standalone, replacement, "replaced")
    if _looks_like_complete_request(normalized):
        return ConversationalRoute(standalone, None, "replaced")

    refreshed = PendingClarification(
        pending.original_text,
        pending.normalized_text,
        contextual.skill,
        dict(contextual.arguments),
        contextual.aggregate,
        contextual.missing_arguments[0],
        contextual.ambiguity_candidates,
        pending.created_monotonic,
        pending.expires_monotonic,
    )
    return ConversationalRoute(contextual, refreshed, "retry")


def pending_from_route(
    route: RoutedIntent,
    original_text: str,
    *,
    now: float,
    ttl_seconds: float,
) -> PendingClarification | None:
    if not route.incomplete or len(route.missing_arguments) != 1:
        return None
    return PendingClarification(
        original_text=original_text.strip(),
        normalized_text=route.normalized_text,
        skill=route.skill,
        arguments=dict(route.arguments),
        aggregate=route.aggregate,
        missing_argument=route.missing_arguments[0],
        ambiguity_candidates=route.ambiguity_candidates,
        created_monotonic=now,
        expires_monotonic=now + ttl_seconds,
    )


def _clearly_new_incomplete(
    standalone: RoutedIntent,
    pending: PendingClarification,
    normalized: str,
) -> bool:
    return (
        standalone.incomplete
        and len(normalized.split()) >= 3
        and (
            standalone.arguments != pending.arguments
            or standalone.missing_arguments != (pending.missing_argument,)
            or standalone.aggregate != pending.aggregate
        )
    )


def _looks_like_complete_request(normalized: str) -> bool:
    words = set(normalized.split())
    request_words = {
        "what",
        "which",
        "how",
        "why",
        "show",
        "give",
        "tell",
        "turn",
        "set",
        "start",
        "stop",
        "restart",
        "explain",
    }
    return len(words) >= 3 and bool(words & request_words)
