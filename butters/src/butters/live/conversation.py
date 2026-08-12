"""Bounded incomplete-request state for voice clarification turns."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from butters.assistant import AssistantResponse, DeterministicAssistant
from butters.live.semantic import incomplete_route_progressed
from butters.routing.model import RoutedIntent


@dataclass(frozen=True, slots=True)
class PendingRequest:
    raw_text: str
    route: RoutedIntent
    expires_at: float


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
        self._pending: PendingRequest | None = None

    @property
    def pending(self) -> PendingRequest | None:
        pending = self._pending
        if pending is not None and self.clock() >= pending.expires_at:
            self._pending = None
            return None
        return pending

    def clear(self) -> None:
        self._pending = None

    def handle_text(self, raw_text: str) -> AssistantResponse:
        now = self.clock()
        pending = self.pending
        standalone = self.assistant.preview_route(raw_text)

        # A complete standalone request always wins over stale context.
        if pending is not None and standalone.matched:
            self.clear()
            return self.assistant.handle_text(raw_text)

        selected_text = raw_text
        if pending is not None:
            if standalone.incomplete and standalone.normalized_text.startswith(
                pending.route.normalized_text
            ):
                # The live controller may already have supplied logical text.
                selected_text = raw_text
            else:
                merged = f"{pending.raw_text.strip()} {raw_text.strip()}".strip()
                merged_route = self.assistant.preview_route(merged)
                if merged_route.matched or incomplete_route_progressed(
                    pending.route, merged_route
                ):
                    selected_text = merged
                else:
                    # No new required information was safely resolved.
                    self.clear()

        response = self.assistant.handle_text(selected_text)
        if response.route.incomplete:
            self._pending = PendingRequest(
                selected_text,
                response.route,
                now + self.continuation_timeout_seconds,
            )
        else:
            self.clear()
        return response
