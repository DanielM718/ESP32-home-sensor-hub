"""Bounded incomplete-request state for voice clarification turns."""

from __future__ import annotations

import time
from collections.abc import Callable

from butters.assistant import AssistantResponse, DeterministicAssistant
from butters.routing.conversation import route_conversation_turn
from butters.routing.model import PendingClarification


class BoundedVoiceConversation:
    """Stitch only recognized incomplete requests, with standalone commands first."""

    def __init__(
        self,
        assistant: DeterministicAssistant,
        *,
        continuation_timeout_seconds: float = 12.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if continuation_timeout_seconds <= 0:
            raise ValueError("continuation_timeout_seconds must be positive")
        self.assistant = assistant
        self.continuation_timeout_seconds = continuation_timeout_seconds
        self.clock = clock
        self._pending: PendingClarification | None = None

    @property
    def pending(self) -> PendingClarification | None:
        pending = self._pending
        if pending is not None and self.clock() >= pending.expires_monotonic:
            self._pending = None
            return None
        return pending

    def clear(self) -> None:
        self._pending = None

    def handle_text(self, raw_text: str) -> AssistantResponse:
        now = self.clock()
        outcome = route_conversation_turn(
            self.assistant.router,
            raw_text,
            self.pending,
            now=now,
            ttl_seconds=self.continuation_timeout_seconds,
            preview_route=self.assistant.preview_route,
        )
        self._pending = outcome.pending
        if outcome.disposition in {"resolved", "retry"}:
            response = self.assistant.handle_routed_text(raw_text, outcome.route)
        else:
            response = self.assistant.handle_text(raw_text)
        return response
