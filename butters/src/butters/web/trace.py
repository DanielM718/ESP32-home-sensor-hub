"""Structured engineering traces. This module never records chain-of-thought."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from butters.diagnostics.sanitizer import sanitize_value


class TraceStage(str, Enum):
    REQUEST = "request"
    AUDIO = "audio"
    STT = "stt"
    NORMALIZATION = "normalization"
    ROUTING = "routing"
    COMPLEXITY = "complexity"
    SKILL = "skill"
    POLICY = "policy"
    TOOL = "tool"
    MODEL = "model"
    RESPONSE = "response"
    TTS = "tts"
    ERROR = "error"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class TraceEvent:
    sequence: int
    timestamp: str
    elapsed_ms: float
    stage: str
    status: str
    reason_code: str | None = None
    fields: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionTrace:
    trace_id: str
    session_id: str
    request_id: str
    source: str
    started_wall: str
    started_monotonic: float
    events: list[TraceEvent] = field(default_factory=list)
    completed: bool = False
    max_events: int = 256
    created_clock: float = 0.0

    def emit(
        self,
        stage: TraceStage | str,
        status: str,
        *,
        reason_code: str | None = None,
        fields: dict[str, object] | None = None,
    ) -> TraceEvent:
        clean, _redactions = sanitize_value(fields or {}, max_text_bytes=2048)
        assert isinstance(clean, dict)
        sequence = self.events[-1].sequence + 1 if self.events else 1
        event = TraceEvent(
            sequence,
            _now(),
            round((time.perf_counter() - self.started_monotonic) * 1000, 3),
            stage.value if isinstance(stage, TraceStage) else str(stage),
            status[:64],
            reason_code[:96] if reason_code else None,
            clean,
        )
        if len(self.events) >= self.max_events:
            # Preserve the request envelope and the newest bounded pipeline
            # state. Voice partials can otherwise grow once per 20 ms frame.
            del self.events[1 if len(self.events) > 1 else 0]
        self.events.append(event)
        if stage == TraceStage.COMPLETE or stage == TraceStage.COMPLETE.value:
            self.completed = True
        return event

    def as_dict(self, *, include_text: bool = True) -> dict[str, object]:
        events = []
        for event in self.events:
            fields = dict(event.fields)
            if not include_text:
                for key in tuple(fields):
                    if key in {"raw_text", "normalized_text", "partial", "response_text"}:
                        fields[key] = "[LIVE_ONLY]"
            events.append(
                {
                    "sequence": event.sequence,
                    "timestamp": event.timestamp,
                    "elapsed_ms": event.elapsed_ms,
                    "stage": event.stage,
                    "status": event.status,
                    "reason_code": event.reason_code,
                    "fields": fields,
                }
            )
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "source": self.source,
            "started_at": self.started_wall,
            "completed": self.completed,
            "events": events,
        }


class TraceBuffer:
    """In-memory detailed trace ring; persistent logs should use summaries only.

    Traces carry conversation text, so they are bounded by count *and* by time,
    and they are dropped with the conversation that produced them.
    """

    def __init__(
        self,
        capacity: int = 256,
        *,
        ttl_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._traces: deque[ExecutionTrace] = deque(maxlen=capacity)
        self._by_id: dict[str, ExecutionTrace] = {}
        self._lock = threading.RLock()
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def start(self, session_id: str, source: str) -> ExecutionTrace:
        self.expire()
        trace = ExecutionTrace(
            secrets.token_urlsafe(18),
            session_id,
            secrets.token_urlsafe(18),
            source,
            _now(),
            time.perf_counter(),
            created_clock=self.clock(),
        )
        with self._lock:
            if len(self._traces) == self._traces.maxlen and self._traces:
                self._by_id.pop(self._traces[0].trace_id, None)
            self._traces.append(trace)
            self._by_id[trace.trace_id] = trace
        return trace

    def get(self, trace_id: str) -> ExecutionTrace | None:
        if not isinstance(trace_id, str) or len(trace_id) > 128:
            return None
        self.expire()
        with self._lock:
            return self._by_id.get(trace_id)

    def recent(self, limit: int = 50, *, include_text: bool = True) -> list[dict[str, object]]:
        bounded = max(1, min(limit, 200))
        self.expire()
        with self._lock:
            selected = list(self._traces)[-bounded:]
        return [item.as_dict(include_text=include_text) for item in reversed(selected)]

    def expire(self) -> int:
        """Drop traces older than the configured TTL."""

        deadline = self.clock() - self.ttl_seconds
        with self._lock:
            retained = [item for item in self._traces if item.created_clock > deadline]
            removed = len(self._traces) - len(retained)
            if removed:
                self._replace(retained)
        return removed

    def drop_sessions(self, session_ids: tuple[str, ...]) -> int:
        """Forget the conversation content of expired or cleared sessions."""

        dropped = frozenset(session_ids)
        if not dropped:
            return 0
        with self._lock:
            retained = [item for item in self._traces if item.session_id not in dropped]
            removed = len(self._traces) - len(retained)
            if removed:
                self._replace(retained)
        return removed

    def _replace(self, retained: list[ExecutionTrace]) -> None:
        self._traces = deque(retained, maxlen=self._traces.maxlen)
        self._by_id = {item.trace_id: item for item in retained}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
