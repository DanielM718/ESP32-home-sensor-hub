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
from butters.llm.catalog import (
    build_tool_catalog,
    entity_alias_summary,
    metric_alias_summary,
)
from butters.llm.model import (
    LanguageModel,
    LanguageModelError,
    LanguageModelResult,
    ProposalKind,
    ToolDefinition,
)
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
    routing_path: str = "deterministic"
    llm_result: LanguageModelResult | None = None
    policy_status: str | None = None


class DeterministicAssistant:
    def __init__(
        self,
        router: IntentRouter,
        skills: SkillRegistry,
        formatter: ResponseFormatter,
        vocabulary: DomainVocabulary,
        *,
        language_model: LanguageModel | None = None,
        llm_tools: tuple[ToolDefinition, ...] = (),
        llm_context: tuple[str, ...] = (),
    ) -> None:
        self.router = router
        self.skills = skills
        self.formatter = formatter
        self.vocabulary = vocabulary
        self.language_model = language_model
        self.llm_tools = llm_tools
        self.llm_context = llm_context

    def handle_text(self, raw_text: str) -> AssistantResponse:
        started = time.perf_counter()
        normalized = normalize_transcript(raw_text.strip(), self.vocabulary)
        route = self.router.route(normalized)
        if not route.matched or route.skill is None:
            if route.allow_fallback and self.language_model is not None:
                return self._handle_fallback(raw_text, normalized, route, started)
            path = "clarification" if route.status == "clarification" else "unsupported"
            return AssistantResponse(
                raw_text,
                normalized,
                route,
                route.message or "That request is not supported.",
                time.perf_counter() - started,
                routing_path=path,
            )
        execution = self.skills.execute(route.skill, route.arguments)
        return AssistantResponse(
            raw_text,
            normalized,
            route,
            self.formatter.format_execution(execution),
            time.perf_counter() - started,
            execution,
            routing_path="deterministic",
            policy_status="allowed" if execution.ok else "denied",
        )

    def _handle_fallback(
        self,
        raw_text: str,
        normalized: str,
        original_route: RoutedIntent,
        started: float,
    ) -> AssistantResponse:
        assert self.language_model is not None
        try:
            result = self.language_model.propose_tools(
                normalized, self.llm_tools, self.llm_context
            )
        except (LanguageModelError, TimeoutError, OSError, RuntimeError) as exc:
            return AssistantResponse(
                raw_text,
                normalized,
                original_route,
                "I couldn't confidently resolve that request.",
                time.perf_counter() - started,
                routing_path="unsupported",
                policy_status=f"model_error:{type(exc).__name__}",
            )

        proposal = result.proposal
        if proposal.kind is ProposalKind.CLARIFICATION:
            message = _clarification_message(proposal.clarification_topic)
            route = RoutedIntent("clarification", normalized, message=message)
            return AssistantResponse(
                raw_text,
                normalized,
                route,
                message,
                time.perf_counter() - started,
                routing_path="llm_fallback",
                llm_result=result,
                policy_status="not_executable",
            )
        if proposal.kind is ProposalKind.UNSUPPORTED:
            route = RoutedIntent(
                "unsupported", normalized, message="That request is not supported."
            )
            return AssistantResponse(
                raw_text,
                normalized,
                route,
                route.message or "That request is not supported.",
                time.perf_counter() - started,
                routing_path="llm_fallback",
                llm_result=result,
                policy_status="not_executable",
            )
        if proposal.kind is not ProposalKind.TOOL or proposal.skill is None:
            route = RoutedIntent(
                "unsupported",
                normalized,
                message="I couldn't confidently resolve that request.",
            )
            return AssistantResponse(
                raw_text,
                normalized,
                route,
                route.message or "That request is not supported.",
                time.perf_counter() - started,
                routing_path="llm_fallback",
                llm_result=result,
                policy_status="invalid_proposal",
            )

        execution = self.skills.execute(proposal.skill, proposal.arguments)
        if not execution.ok:
            route = RoutedIntent(
                "unsupported",
                normalized,
                message="That proposed request was denied by the skill policy.",
            )
            return AssistantResponse(
                raw_text,
                normalized,
                route,
                route.message or "That request is not supported.",
                time.perf_counter() - started,
                execution,
                routing_path="llm_fallback",
                llm_result=result,
                policy_status="denied",
            )
        route = RoutedIntent(
            "matched",
            normalized,
            proposal.skill,
            proposal.arguments,
            confidence=0.0,
        )
        return AssistantResponse(
            raw_text,
            normalized,
            route,
            self.formatter.format_execution(execution),
            time.perf_counter() - started,
            execution,
            routing_path="llm_fallback",
            llm_result=result,
            policy_status="allowed",
        )


def _clarification_message(topic: str | None) -> str:
    return {
        "sensor": "Which sensor did you mean?",
        "filament_box": "Which filament box did you mean?",
        "metric": "Which measurement did you mean?",
        "request": "Could you clarify what sensor information you want?",
    }.get(topic, "Could you clarify that request?")


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
    language_model: LanguageModel | None = None,
) -> DeterministicAssistant:
    entities = EntityRegistry(settings.entities)
    metrics = MetricRegistry()
    sensor_provider = sensor_adapter or DashboardSensorAdapter(settings.integration)
    server_provider = server_adapter or LocalServerHealthAdapter()
    skills = build_read_only_registry(
        sensor_provider, server_provider, entities, metrics
    )
    if language_model is None and settings.llm.enabled:
        from butters.llm.llama_server import LlamaCppServerLanguageModel

        language_model = LlamaCppServerLanguageModel(
            settings.llm.server_url,
            settings.llm.model,
            profile=settings.llm.profile,
            output_mode=settings.llm.output_mode,
            timeout_seconds=settings.llm.timeout_seconds,
        )
    return DeterministicAssistant(
        IntentRouter(entities, metrics),
        skills,
        ResponseFormatter(),
        vocabulary,
        language_model=language_model,
        llm_tools=build_tool_catalog(entities, metrics),
        llm_context=(
            *(f"entity {item}" for item in entity_alias_summary(entities)),
            *(f"metric {item}" for item in metric_alias_summary(metrics)),
        ),
    )
