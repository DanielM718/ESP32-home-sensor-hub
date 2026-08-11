"""End-to-end deterministic transcript-to-response orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from queue import Full, Queue
from threading import Thread

from butters.assistant_config import AssistantSettings
from butters.integrations.dashboard import DashboardSensorAdapter
from butters.integrations.server_health import LocalServerHealthAdapter
from butters.responses.formatter import ResponseFormatter
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.routing.model import RoutedIntent
from butters.routing.router import IntentRouter
from butters.skills.implementations import build_read_only_registry
from butters.skills.model import SkillExecution
from butters.skills.registry import SkillRegistry
from butters.stt.normalization import DomainVocabulary, normalize_transcript


@dataclass(frozen=True, slots=True)
class AssistantResponse:
    raw_text: str
    normalized_text: str
    route: RoutedIntent
    response_text: str
    elapsed_seconds: float
    execution: SkillExecution | None = None


class DeterministicAssistant:
    def __init__(
        self,
        router: IntentRouter,
        skills: SkillRegistry,
        formatter: ResponseFormatter,
        vocabulary: DomainVocabulary,
    ) -> None:
        self.router = router
        self.skills = skills
        self.formatter = formatter
        self.vocabulary = vocabulary

    def handle_text(self, raw_text: str) -> AssistantResponse:
        started = time.perf_counter()
        normalized = normalize_transcript(raw_text.strip(), self.vocabulary)
        route = self.router.route(normalized)
        if not route.matched or route.skill is None:
            return AssistantResponse(
                raw_text,
                normalized,
                route,
                route.message or "That request is not supported.",
                time.perf_counter() - started,
            )
        execution = self.skills.execute(route.skill, route.arguments)
        return AssistantResponse(
            raw_text,
            normalized,
            route,
            self.formatter.format_execution(execution),
            time.perf_counter() - started,
            execution,
        )


class AsyncAssistantResponder:
    """Bounded worker that keeps integration latency off the audio capture loop."""

    _STOP = object()

    def __init__(
        self,
        assistant: DeterministicAssistant,
        on_response: Callable[[AssistantResponse], None],
        *,
        max_pending: int = 2,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.assistant = assistant
        self.on_response = on_response
        self._queue: Queue[str | object] = Queue(maxsize=max_pending)
        self._closed = False
        self._thread = Thread(
            target=self._run,
            name="butters-read-only-responder",
            daemon=True,
        )
        self._thread.start()

    def submit(self, transcript: str) -> bool:
        if self._closed:
            return False
        try:
            self._queue.put_nowait(transcript)
        except Full:
            return False
        return True

    def close(self, *, timeout_seconds: float = 10.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(self._STOP, timeout=timeout_seconds)
        except Full:
            return
        self._thread.join(timeout=timeout_seconds)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                response = self.assistant.handle_text(str(item))
                self.on_response(response)
            finally:
                self._queue.task_done()


def create_assistant(
    settings: AssistantSettings,
    vocabulary: DomainVocabulary,
    *,
    sensor_adapter: DashboardSensorAdapter | None = None,
    server_adapter: LocalServerHealthAdapter | None = None,
) -> DeterministicAssistant:
    entities = EntityRegistry(settings.entities)
    metrics = MetricRegistry()
    sensor_provider = sensor_adapter or DashboardSensorAdapter(settings.integration)
    server_provider = server_adapter or LocalServerHealthAdapter()
    skills = build_read_only_registry(
        sensor_provider, server_provider, entities, metrics
    )
    return DeterministicAssistant(
        IntentRouter(entities, metrics),
        skills,
        ResponseFormatter(),
        vocabulary,
    )
