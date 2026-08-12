"""Structured engineering traces. This module never records chain-of-thought."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
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
    """In-memory detailed trace ring; persistent logs should use summaries only."""

    def __init__(self, capacity: int = 256) -> None:
        self._traces: deque[ExecutionTrace] = deque(maxlen=capacity)
        self._by_id: dict[str, ExecutionTrace] = {}
        self._lock = threading.RLock()

    def start(self, session_id: str, source: str) -> ExecutionTrace:
        trace = ExecutionTrace(
            secrets.token_urlsafe(18),
            session_id,
            secrets.token_urlsafe(18),
            source,
            _now(),
            time.perf_counter(),
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
        with self._lock:
            return self._by_id.get(trace_id)

    def recent(self, limit: int = 50, *, include_text: bool = True) -> list[dict[str, object]]:
        bounded = max(1, min(limit, 200))
        with self._lock:
            selected = list(self._traces)[-bounded:]
        return [item.as_dict(include_text=include_text) for item in reversed(selected)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
