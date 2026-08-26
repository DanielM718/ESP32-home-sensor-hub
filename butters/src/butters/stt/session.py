"""VAD-gated streaming session orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from butters.audio.frontend import AudioFrontend
from butters.audio.model import AudioFrame
from butters.stt.model import StreamingSTTEngine
from butters.stt.normalization import DomainVocabulary, normalize_transcript


@dataclass(frozen=True, slots=True)
class UtteranceResult:
    raw: str
    normalized: str
    partials: tuple[str, ...]
    audio_seconds: float
    processing_seconds: float
    finalization_latency_seconds: float
    speech_end_to_final_seconds: float
    endpoint_reason: str
    processing_cpu_seconds: float = 0.0
    effective_text: str = ""
    semantic_status: str = "unclassified"
    preprocessing_seconds: float = 0.0
    inference_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class TranscriptionEvent:
    kind: Literal["speech_start", "partial", "final"]
    audio_position_seconds: float
    text: str = ""
    result: UtteranceResult | None = None


EventCallback = Callable[[TranscriptionEvent], None]


class StreamingTranscriber:
    """Join the source-independent audio frontend to a streaming recognizer."""

    def __init__(
        self,
        engine: StreamingSTTEngine,
        vocabulary: DomainVocabulary,
    ) -> None:
        self.engine = engine
        self.vocabulary = vocabulary

    def run(
        self,
        frontend: AudioFrontend,
        *,
        on_event: EventCallback | None = None,
        max_audio_seconds: float = 0.0,
    ) -> list[UtteranceResult]:
        results: list[UtteranceResult] = []
        in_utterance = False
        partials: list[str] = []
        utterance_samples = 0
        processing_seconds = 0.0
        processing_cpu_seconds = 0.0
        audio_position = 0.0
        last_signal_position = 0.0

        def emit(event: TranscriptionEvent) -> None:
            if on_event is not None:
                on_event(event)

        def accept(frame: AudioFrame) -> None:
            nonlocal utterance_samples, processing_seconds, processing_cpu_seconds
            started = time.perf_counter()
            cpu_started = time.process_time()
            partial = self.engine.accept_audio(frame)
            processing_seconds += time.perf_counter() - started
            processing_cpu_seconds += time.process_time() - cpu_started
            utterance_samples += frame.sample_count
            if partial is not None and partial:
                partials.append(partial)
                emit(TranscriptionEvent("partial", audio_position, partial))

        def finish(reason: str) -> None:
            nonlocal in_utterance, partials, utterance_samples, processing_seconds
            nonlocal processing_cpu_seconds
            nonlocal last_signal_position
            started = time.perf_counter()
            cpu_started = time.process_time()
            raw = self.engine.finalize()
            finalization_latency = time.perf_counter() - started
            processing_seconds += finalization_latency
            processing_cpu_seconds += time.process_time() - cpu_started
            result = UtteranceResult(
                raw=raw,
                normalized=normalize_transcript(raw, self.vocabulary),
                partials=tuple(partials),
                audio_seconds=utterance_samples / 16_000,
                processing_seconds=processing_seconds,
                finalization_latency_seconds=finalization_latency,
                speech_end_to_final_seconds=(
                    max(0.0, audio_position - last_signal_position)
                    + finalization_latency
                ),
                endpoint_reason=reason,
                processing_cpu_seconds=processing_cpu_seconds,
            )
            results.append(result)
            emit(TranscriptionEvent("final", audio_position, raw, result))
            self.engine.reset()
            in_utterance = False
            partials = []
            utterance_samples = 0
            processing_seconds = 0.0
            processing_cpu_seconds = 0.0
            last_signal_position = 0.0

        try:
            while max_audio_seconds <= 0 or audio_position < max_audio_seconds:
                item = frontend.read()
                if item is None:
                    break
                audio_position += item.audio.duration_seconds
                just_started = False
                if not in_utterance and item.analysis.speech_active:
                    self.engine.start_utterance()
                    in_utterance = True
                    just_started = True
                    emit(TranscriptionEvent("speech_start", audio_position))
                    for retained in frontend.pre_roll.snapshot():
                        accept(retained)
                elif in_utterance:
                    accept(item.audio)

                if (
                    in_utterance
                    and item.analysis.dbfs >= frontend.vad.threshold_dbfs
                ):
                    last_signal_position = audio_position

                if not in_utterance:
                    continue
                if self.engine.endpoint_detected():
                    finish("recognizer")
                elif not item.analysis.speech_active and not just_started:
                    finish("vad_silence")
            if in_utterance:
                finish("end_of_input")
        except BaseException:
            self.engine.reset()
            raise
        return results
