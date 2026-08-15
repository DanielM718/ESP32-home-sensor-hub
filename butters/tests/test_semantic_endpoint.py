from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.integrations.model import (
    SensorRecord,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.live.conversation import BoundedVoiceConversation
from butters.live.semantic import SemanticEndpointEvaluator
from butters.routing.conversation import pending_from_route
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.routing.router import IntentRouter
from butters.stt.normalization import load_domain_vocabulary
from butters.wakeword.model import WakeDetection

from butters.config import (
    default_stt_model_dir,
    default_vocabulary_path,
    load_stt_settings,
)


def _router() -> IntentRouter:
    settings = load_assistant_settings()
    return IntentRouter(EntityRegistry(settings.entities), MetricRegistry())


def test_semantic_evaluator_distinguishes_complete_incomplete_and_corrupt() -> None:
    evaluator = SemanticEndpointEvaluator(_router().route)

    complete = evaluator.assess("what is the humidity in filament box two")
    incomplete = evaluator.assess("what is the humidity")
    corrupt = evaluator.assess("y level")

    assert complete.status == "complete"
    assert incomplete.status == "incomplete"
    assert incomplete.route.missing_arguments == ("entity",)
    assert corrupt.status == "unrecognized"


def test_semantic_evaluator_merges_only_when_a_pending_request_exists() -> None:
    router = _router()
    evaluator = SemanticEndpointEvaluator(router.route)
    pending = pending_from_route(
        router.route("what is the humidity"),
        "what is the humidity",
        now=0.0,
        ttl_seconds=12.0,
    )
    assert pending is not None

    without_pending = evaluator.assess("filament box two")
    continued = evaluator.assess("filament box two", pending=pending)
    unrelated = evaluator.assess("what is the server status", pending=pending)

    assert without_pending.status == "unrecognized"
    assert continued.status == "complete" and continued.continued
    assert continued.effective_text == "filament box two"
    assert continued.route.arguments == {
        "entity": "filament_box_2",
        "metric": "humidity",
    }
    assert unrelated.status == "complete" and not unrelated.continued
    assert unrelated.effective_text == "what is the server status"


class _Sensors:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> SensorSnapshot:
        self.calls += 1
        return SensorSnapshot(
            "2026-08-11T12:00:00Z",
            (
                SensorRecord(
                    "environment",
                    "2",
                    "2026-08-11T11:59:55Z",
                    5,
                    "online",
                    {"humidity": 42.5},
                    ("humidity",),
                ),
            ),
        )


class _Health:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> ServerHealthSnapshot:
        self.calls += 1
        return ServerHealthSnapshot(
            1.0, 0.1, 0.1, 0.1, 2_000_000_000, 0, 1, 2, 50.0, "0x0", ()
        )


def _conversation(
    *, clock: list[float] | None = None
) -> tuple[BoundedVoiceConversation, _Sensors, _Health]:
    settings = load_assistant_settings()
    settings = replace(
        settings,
        diagnostics=replace(settings.diagnostics, enabled=False),
    )
    sensors = _Sensors()
    health = _Health()
    assistant = create_assistant(
        settings,
        load_domain_vocabulary(default_vocabulary_path()),
        sensor_adapter=sensors,  # type: ignore[arg-type]
        server_adapter=health,  # type: ignore[arg-type]
    )
    now = clock if clock is not None else [0.0]
    return (
        BoundedVoiceConversation(
            assistant,
            continuation_timeout_seconds=12.0,
            clock=lambda: now[0],
        ),
        sensors,
        health,
    )


def test_clarification_response_fills_entity_and_executes_once() -> None:
    conversation, sensors, _ = _conversation()

    first = conversation.handle_text("what is the humidity")
    second = conversation.handle_text("filament box two")

    assert first.route.incomplete
    assert first.execution is None
    assert second.route.matched
    assert second.route.arguments == {
        "entity": "filament_box_2",
        "metric": "humidity",
    }
    assert second.execution is not None and second.execution.ok
    assert sensors.calls == 1
    assert conversation.pending is None


def test_complete_unrelated_command_cancels_pending_without_concatenation() -> None:
    conversation, sensors, health = _conversation()

    conversation.handle_text("what is the humidity")
    response = conversation.handle_text("what is the server status")

    assert response.route.skill == "get_server_health"
    assert response.raw_text == "what is the server status"
    assert sensors.calls == 0
    assert health.calls == 1
    assert conversation.pending is None


def test_home_assistant_health_preview_replaces_pending_fragment() -> None:
    settings = load_assistant_settings()
    assistant = create_assistant(
        settings,
        load_domain_vocabulary(default_vocabulary_path()),
        sensor_adapter=_Sensors(),  # type: ignore[arg-type]
        server_adapter=_Health(),  # type: ignore[arg-type]
    )
    evaluator = SemanticEndpointEvaluator(assistant.preview_route)
    pending = pending_from_route(
        assistant.preview_route("what is the humidity"),
        "what is the humidity",
        now=0.0,
        ttl_seconds=12.0,
    )
    assert pending is not None

    assessment = evaluator.assess(
        "is Home Assistant healthy", pending=pending
    )

    assert assessment.status == "complete"
    assert not assessment.continued
    assert assessment.effective_text == "is Home Assistant healthy"
    assert assessment.route.skill == "diagnose_read_only"


def test_corrupt_fragment_and_expired_continuation_never_execute() -> None:
    clock = [0.0]
    conversation, sensors, health = _conversation(clock=clock)

    corrupt = conversation.handle_text("y level")
    incomplete = conversation.handle_text("what is the humidity")
    clock[0] = 12.1
    expired = conversation.handle_text("filament box two")

    assert corrupt.route.status == "unsupported"
    assert "sensor reading or status" in corrupt.response_text.lower()
    assert incomplete.route.incomplete
    assert expired.route.status == "unsupported"
    assert sensors.calls == 0 and health.calls == 0
    assert conversation.pending is None


def test_corrupt_followup_does_not_extend_or_duplicate_pending_request() -> None:
    conversation, sensors, health = _conversation()

    conversation.handle_text("what is the humidity")
    original = conversation.pending
    assert original is not None
    response = conversation.handle_text("y level")

    assert response.raw_text == "y level"
    assert response.route.status == "clarification"
    assert "Which sensor" in response.response_text
    retried = conversation.pending
    assert retried is not None
    assert retried.arguments == {"metric": "humidity"}
    assert retried.created_monotonic == original.created_monotonic
    assert retried.expires_monotonic == original.expires_monotonic
    assert sensors.calls == 0 and health.calls == 0


def test_default_stt_model_and_sherpa_endpoint_policy() -> None:
    settings = load_stt_settings()

    assert default_stt_model_dir().name == (
        "sherpa-onnx-streaming-zipformer-en-2023-06-21"
    )
    assert not settings.sherpa_endpoint_enabled


def test_sherpa_disabled_endpoint_never_queries_recognizer(
    tmp_path: Path, monkeypatch
) -> None:
    from butters.stt.sherpa_engine import MODEL_FILES, SherpaOnnxStreamingSTT

    for filename in MODEL_FILES.values():
        (tmp_path / filename).write_bytes(b"model")

    calls: dict[str, object] = {}

    class _Recognizer:
        def __init__(self) -> None:
            self.endpoint_calls = 0

        def create_stream(self) -> object:
            return object()

        def is_endpoint(self, stream: object) -> bool:
            self.endpoint_calls += 1
            return True

    recognizer = _Recognizer()

    class _OnlineRecognizer:
        @staticmethod
        def from_transducer(**kwargs):
            calls.update(kwargs)
            return recognizer

    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        SimpleNamespace(OnlineRecognizer=_OnlineRecognizer),
    )
    engine = SherpaOnnxStreamingSTT(tmp_path, sherpa_endpoint_enabled=False)
    engine.start_utterance()

    assert calls["enable_endpoint_detection"] is False
    assert not engine.endpoint_detected()
    assert recognizer.endpoint_calls == 0


def test_wake_metric_is_named_as_token_timestamp_lag() -> None:
    detection = WakeDetection("HEY BUTTERS", None, 0.25, 0.4)

    assert detection.token_end_lag_seconds == 0.4
    assert detection.model_latency_seconds == 0.4
