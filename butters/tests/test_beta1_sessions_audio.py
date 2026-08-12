from __future__ import annotations

from dataclasses import replace

import pytest

from butters.assistant_config import BrowserAudioSettings
from butters.stt.model import StreamingSTTEngine
from butters.stt.normalization import DomainVocabulary
from butters.web.audio import BrowserAudioError, BrowserAudioStream
from butters.web.sessions import SessionManager
from butters.web.trace import TraceBuffer, TraceStage


class Engine(StreamingSTTEngine):
    def __init__(self) -> None:
        self.active = False
        self.samples = 0

    @property
    def initialization_seconds(self) -> float:
        return 0.0

    def start_utterance(self) -> None:
        self.active = True

    def accept_audio(self, frame):
        self.samples += frame.sample_count
        return "box three humidity" if self.samples else None

    def get_partial_transcript(self) -> str:
        return "box three humidity"

    def endpoint_detected(self) -> bool:
        return False

    def finalize(self) -> str:
        self.active = False
        return "what is the humidity in box three"

    def reset(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False


def test_browser_audio_converts_48khz_and_emits_bounded_final() -> None:
    engine = Engine()
    stream = BrowserAudioStream(engine, DomainVocabulary((), ()), BrowserAudioSettings())
    stream.start(sample_rate=48000, channels=1, encoding="pcm_s16le")
    events = stream.accept((1000).to_bytes(2, "little", signed=True) * 4800)
    final = stream.finish()

    assert any(item.kind == "speech_start" for item in events)
    assert any(item.kind == "partial" for item in events)
    assert final.result is not None
    assert final.result.raw == "what is the humidity in box three"
    assert 1500 <= engine.samples <= 1700
    assert 0.09 <= final.result.audio_seconds <= 0.11


def test_browser_audio_rejects_malformed_oversized_and_overlong_frames() -> None:
    settings = replace(BrowserAudioSettings(), max_chunk_bytes=640, max_utterance_seconds=1)
    stream = BrowserAudioStream(Engine(), DomainVocabulary((), ()), settings)
    stream.start(sample_rate=16000, channels=2, encoding="pcm_s16le")

    with pytest.raises(BrowserAudioError) as malformed:
        stream.accept(b"abc")
    with pytest.raises(BrowserAudioError) as oversized:
        stream.accept(b"\0" * 644)

    assert malformed.value.code == "malformed_frame"
    assert oversized.value.code == "frame_too_large"


def test_session_ids_are_unpredictable_bounded_reconnectable_and_expire() -> None:
    now = [100.0]
    manager = SessionManager(max_active=2, ttl_seconds=60, max_messages=4, max_context_chars=20, clock=lambda: now[0])
    first = manager.create()
    second = manager.create()

    assert first.session_id != second.session_id
    assert len(first.session_id) >= 32
    assert manager.get(first.session_id) is first
    manager.add_message(first, "user", "1234567890")
    manager.add_message(first, "assistant", "abcdefghij")
    manager.add_message(first, "user", "overflow")
    assert sum(len(item.text) for item in first.messages) <= 20

    now[0] += 61
    assert manager.get(first.session_id) is None
    assert manager.expire() >= 1


def test_live_trace_redacts_secrets_and_persistence_view_removes_text() -> None:
    traces = TraceBuffer(32)
    trace = traces.start("safe-session", "text")
    trace.emit(
        TraceStage.REQUEST,
        "accepted",
        fields={
            "raw_text": "api_key=sk-abcdefghijklmnop",
            "authorization": "Bearer never-store-this",
        },
    )

    live = trace.as_dict(include_text=True)
    persistent = trace.as_dict(include_text=False)

    assert "sk-abcdefghijklmnop" not in str(live)
    assert "never-store-this" not in str(live)
    assert persistent["events"][0]["fields"]["raw_text"] == "[LIVE_ONLY]"


def test_trace_event_count_is_bounded_during_changing_stt_partials() -> None:
    trace = TraceBuffer(32).start("safe-session", "voice")
    trace.emit(TraceStage.REQUEST, "accepted")
    for index in range(500):
        trace.emit(TraceStage.STT, "partial", fields={"partial": f"partial {index}"})
    trace.emit(TraceStage.COMPLETE, "complete")

    assert len(trace.events) == 256
    assert trace.events[0].stage == "request"
    assert trace.events[-1].stage == "complete"
    assert trace.events[-1].sequence == 502
