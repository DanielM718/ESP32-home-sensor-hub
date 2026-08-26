"""Bounded browser PCM transport mapped onto the existing streaming STT API."""

from __future__ import annotations

import time
from dataclasses import dataclass

from butters.assistant_config import BrowserAudioSettings
from butters.audio.conversion import StreamingPcmConverter
from butters.audio.model import AudioFrame
from butters.stt.model import StreamingSTTEngine
from butters.stt.normalization import DomainVocabulary, normalize_transcript
from butters.stt.session import UtteranceResult


class BrowserAudioError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BrowserAudioEvent:
    kind: str
    text: str = ""
    result: UtteranceResult | None = None


class BrowserAudioStream:
    """One tap-to-record PCM stream with hard byte/time/frame limits."""

    INTERNAL_FRAME_BYTES = 640  # 20 ms, 16 kHz, mono, S16_LE

    def __init__(
        self,
        engine: StreamingSTTEngine,
        vocabulary: DomainVocabulary,
        settings: BrowserAudioSettings,
        *,
        clock: callable = time.monotonic,
    ) -> None:
        self.engine = engine
        self.vocabulary = vocabulary
        self.settings = settings
        self.clock = clock
        self._converter: StreamingPcmConverter | None = None
        self._sample_rate = 0
        self._channels = 0
        self._buffer = bytearray()
        self._input_bytes = 0
        self._sequence = 0
        self._started_at = 0.0
        self._last_chunk_at = 0.0
        self._partials: list[str] = []
        self._processing_seconds = 0.0
        self._preprocessing_seconds = 0.0
        self._inference_seconds = 0.0
        self._speech_started = False
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def audio_seconds(self) -> float:
        denominator = self._sample_rate * self._channels * 2
        return self._input_bytes / denominator if denominator else 0.0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def start(
        self,
        *,
        sample_rate: int,
        channels: int,
        encoding: str,
    ) -> tuple[BrowserAudioEvent, ...]:
        if self._active:
            raise BrowserAudioError("already_started", "audio stream is already active")
        if encoding != "pcm_s16le":
            raise BrowserAudioError("unsupported_encoding", "encoding must be pcm_s16le")
        if sample_rate not in self.settings.allowed_sample_rates:
            raise BrowserAudioError("unsupported_sample_rate", "sample rate is not allow-listed")
        if channels not in {1, 2}:
            raise BrowserAudioError("unsupported_channels", "channels must be one or two")
        self._sample_rate = sample_rate
        self._channels = channels
        self._converter = StreamingPcmConverter(
            input_rate=sample_rate,
            input_channels=channels,
            input_sample_width=2,
        )
        self._buffer.clear()
        self._input_bytes = 0
        self._sequence = 0
        self._partials.clear()
        self._processing_seconds = 0.0
        self._preprocessing_seconds = 0.0
        self._inference_seconds = 0.0
        self._speech_started = False
        self._started_at = self.clock()
        self._last_chunk_at = self._started_at
        self.engine.start_utterance()
        self._active = True
        return (BrowserAudioEvent("listening"),)

    def accept(self, chunk: bytes) -> tuple[BrowserAudioEvent, ...]:
        if not self._active or self._converter is None:
            raise BrowserAudioError("not_started", "audio stream has not started")
        if not isinstance(chunk, bytes) or not chunk:
            raise BrowserAudioError("malformed_frame", "audio frame must be non-empty bytes")
        if len(chunk) > self.settings.max_chunk_bytes:
            raise BrowserAudioError("frame_too_large", "audio frame exceeds the byte limit")
        input_frame_width = self._channels * 2
        if len(chunk) % input_frame_width:
            raise BrowserAudioError("malformed_frame", "audio frame ends mid-sample")
        now = self.clock()
        if now - self._started_at > self.settings.session_timeout_seconds:
            raise BrowserAudioError("session_timeout", "audio session exceeded its time limit")
        if now - self._last_chunk_at > self.settings.idle_timeout_seconds:
            raise BrowserAudioError("idle_timeout", "audio session was idle too long")
        next_bytes = self._input_bytes + len(chunk)
        next_seconds = next_bytes / (self._sample_rate * self._channels * 2)
        if next_seconds > self.settings.max_utterance_seconds:
            raise BrowserAudioError("utterance_too_long", "utterance exceeded its duration limit")
        self._input_bytes = next_bytes
        self._last_chunk_at = now
        preprocessing_started = time.perf_counter()
        try:
            converted = self._converter.convert(chunk)
        except ValueError as exc:
            raise BrowserAudioError("malformed_frame", "audio conversion rejected the frame") from exc
        self._preprocessing_seconds += time.perf_counter() - preprocessing_started
        if len(self._buffer) + len(converted) > self.settings.max_buffered_bytes:
            raise BrowserAudioError("buffer_limit", "audio buffer limit exceeded")
        self._buffer.extend(converted)
        events: list[BrowserAudioEvent] = []
        while len(self._buffer) >= self.INTERNAL_FRAME_BYTES:
            pcm = bytes(self._buffer[: self.INTERNAL_FRAME_BYTES])
            del self._buffer[: self.INTERNAL_FRAME_BYTES]
            events.extend(self._feed_frame(pcm))
        return tuple(events)

    def finish(self, *, endpoint_reason: str = "client_stop") -> BrowserAudioEvent:
        if not self._active or self._converter is None:
            raise BrowserAudioError("not_started", "audio stream has not started")
        preprocessing_started = time.perf_counter()
        converted = self._converter.finish()
        self._preprocessing_seconds += time.perf_counter() - preprocessing_started
        if len(self._buffer) + len(converted) > self.settings.max_buffered_bytes:
            self.abort()
            raise BrowserAudioError("buffer_limit", "audio buffer limit exceeded")
        self._buffer.extend(converted)
        while self._buffer:
            size = min(len(self._buffer), self.INTERNAL_FRAME_BYTES)
            if size % 2:
                size -= 1
            if size <= 0:
                break
            pcm = bytes(self._buffer[:size])
            del self._buffer[:size]
            self._feed_frame(pcm)
        started = time.perf_counter()
        try:
            raw = self.engine.finalize()
        except Exception as exc:  # noqa: BLE001 - recognizer boundary
            self.abort()
            raise BrowserAudioError("stt_error", "speech recognition failed safely") from exc
        finalization = time.perf_counter() - started
        self._processing_seconds += finalization
        self._inference_seconds += finalization
        normalized = normalize_transcript(raw, self.vocabulary)
        result = UtteranceResult(
            raw=raw,
            normalized=normalized,
            partials=tuple(self._partials),
            audio_seconds=self.audio_seconds,
            processing_seconds=(
                self._preprocessing_seconds + self._inference_seconds
            ),
            finalization_latency_seconds=finalization,
            speech_end_to_final_seconds=finalization,
            endpoint_reason=endpoint_reason,
            effective_text=normalized,
            preprocessing_seconds=self._preprocessing_seconds,
            inference_seconds=self._inference_seconds,
        )
        self._active = False
        self._converter = None
        self._buffer.clear()
        return BrowserAudioEvent("final", raw, result)

    def abort(self) -> None:
        try:
            self.engine.reset()
        finally:
            self._active = False
            self._converter = None
            self._buffer.clear()

    def close(self, close_engine: bool = True) -> None:
        if self._active:
            self.abort()
        if close_engine:
            self.engine.close()

    def _feed_frame(self, pcm: bytes) -> list[BrowserAudioEvent]:
        self._sequence += 1
        frame = AudioFrame(pcm, self._sequence, time.monotonic())
        events: list[BrowserAudioEvent] = []
        if not self._speech_started and _has_signal(pcm):
            self._speech_started = True
            events.append(BrowserAudioEvent("speech_start"))
        started = time.perf_counter()
        try:
            partial = self.engine.accept_audio(frame)
        except Exception as exc:  # noqa: BLE001 - recognizer boundary
            raise BrowserAudioError("stt_error", "speech recognition failed safely") from exc
        elapsed = time.perf_counter() - started
        self._processing_seconds += elapsed
        self._inference_seconds += elapsed
        if partial and (not self._partials or partial != self._partials[-1]):
            self._partials.append(partial)
            events.append(BrowserAudioEvent("partial", partial))
        return events


def _has_signal(pcm: bytes, threshold: int = 400) -> bool:
    for offset in range(0, len(pcm), 2):
        value = int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        if abs(value) >= threshold:
            return True
    return False
