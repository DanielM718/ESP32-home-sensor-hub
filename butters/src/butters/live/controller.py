"""Wake -> acknowledgement -> VAD/STT -> idle state machine."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from butters.audio.analysis import EnergyVad, analyze_frame
from butters.audio.buffer import PreRollBuffer
from butters.audio.chime import ChimePlayer
from butters.audio.model import AudioFrame, AudioSource, AudioSourceError
from butters.stt.model import StreamingSTTEngine
from butters.stt.normalization import DomainVocabulary, normalize_transcript
from butters.stt.session import UtteranceResult
from butters.wakeword.model import WakeDetection, WakeWordDetector


class LiveState(str, Enum):
    WAITING_FOR_WAKE = "waiting_for_wake"
    WAKE_DETECTED = "wake_detected"
    LISTENING = "listening"
    FINALIZING = "finalizing"
    RETURNING_TO_IDLE = "returning_to_idle"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class LiveEvent:
    kind: Literal[
        "ready",
        "wake",
        "listening",
        "speech_start",
        "partial",
        "final",
        "timeout",
        "error",
        "returning",
    ]
    state: LiveState
    text: str = ""
    detection: WakeDetection | None = None
    result: UtteranceResult | None = None
    acknowledgement_launch_seconds: float | None = None


LiveEventCallback = Callable[[LiveEvent], None]


class LiveVoiceController:
    """Pure per-frame state machine; it never opens or reopens a microphone.

    One caller owns the continuous ``AudioSource`` and gives every standardized
    frame to this controller. The same frame stream is routed to KWS while idle
    and to command VAD/STT after a wake. A bounded history is always retained.
    """

    def __init__(
        self,
        *,
        wake_detector: WakeWordDetector,
        stt_engine: StreamingSTTEngine,
        vocabulary: DomainVocabulary,
        command_vad: EnergyVad,
        wake_preroll: PreRollBuffer,
        command_preroll: PreRollBuffer,
        chime: ChimePlayer,
        no_speech_timeout_seconds: float = 4.0,
        max_command_seconds: float = 20.0,
        acknowledgement_guard_seconds: float = 0.12,
        clip_threshold: float = 0.98,
    ) -> None:
        self.wake_detector = wake_detector
        self.stt_engine = stt_engine
        self.vocabulary = vocabulary
        self.command_vad = command_vad
        self.wake_preroll = wake_preroll
        self.command_preroll = command_preroll
        self.chime = chime
        self.no_speech_timeout_seconds = no_speech_timeout_seconds
        self.max_command_seconds = max_command_seconds
        self.acknowledgement_guard_seconds = acknowledgement_guard_seconds
        self.clip_threshold = clip_threshold
        self.state = LiveState.STOPPED
        self._audio_position = 0.0
        self._listening_started = 0.0
        self._speech_started = 0.0
        self._last_signal_position = 0.0
        self._in_utterance = False
        self._partials: list[str] = []
        self._utterance_samples = 0
        self._processing_seconds = 0.0
        self._processing_cpu_seconds = 0.0

    def start(self) -> tuple[LiveEvent, ...]:
        self.wake_detector.reset()
        self.stt_engine.reset()
        self.command_vad.reset()
        self.wake_preroll.clear()
        self.command_preroll.clear()
        self._clear_utterance()
        self.state = LiveState.WAITING_FOR_WAKE
        return (LiveEvent("ready", self.state),)

    def _clear_utterance(self) -> None:
        self._in_utterance = False
        self._partials = []
        self._utterance_samples = 0
        self._processing_seconds = 0.0
        self._processing_cpu_seconds = 0.0
        self._speech_started = 0.0
        self._last_signal_position = 0.0

    def _seed_post_keyword_audio(self, detection: WakeDetection) -> None:
        """Keep only audio estimated to follow the wake phrase.

        The KWS token timestamp lets us retain immediate command audio already
        captured during the model's small detection delay without sending the
        full wake phrase to STT.
        """

        self.command_preroll.clear()
        if not detection.model_latency_seconds:
            return
        retained = self.wake_preroll.snapshot()
        if not retained:
            return
        frame_seconds = retained[-1].duration_seconds
        frames = math.ceil(detection.model_latency_seconds / frame_seconds)
        frames = min(frames, self.command_preroll.max_frames)
        for frame in retained[-frames:]:
            self.command_preroll.append(frame)

    def _begin_listening(self, detection: WakeDetection) -> list[LiveEvent]:
        self.state = LiveState.WAKE_DETECTED
        events = [LiveEvent("wake", self.state, detection=detection)]
        self._seed_post_keyword_audio(detection)
        self.command_vad.reset()
        self.stt_engine.reset()
        self._clear_utterance()
        self._listening_started = self._audio_position
        acknowledgement_latency: float | None = None
        try:
            acknowledgement_latency = self.chime.play()
        except Exception as exc:  # noqa: BLE001 - acknowledgement is non-fatal
            events.append(
                LiveEvent(
                    "error",
                    self.state,
                    text=f"acknowledgement playback failed: {exc}",
                )
            )
        self.state = LiveState.LISTENING
        events.append(
            LiveEvent(
                "listening",
                self.state,
                acknowledgement_launch_seconds=acknowledgement_latency,
            )
        )
        return events

    def _accept_stt(self, frame: AudioFrame) -> LiveEvent | None:
        started = time.perf_counter()
        cpu_started = time.process_time()
        partial = self.stt_engine.accept_audio(frame)
        self._processing_seconds += time.perf_counter() - started
        self._processing_cpu_seconds += time.process_time() - cpu_started
        self._utterance_samples += frame.sample_count
        if not partial or partial == (self._partials[-1] if self._partials else ""):
            return None
        self._partials.append(partial)
        return LiveEvent("partial", self.state, text=partial)

    def _start_utterance(self) -> list[LiveEvent]:
        self.stt_engine.start_utterance()
        self._in_utterance = True
        self._speech_started = self._audio_position
        events = [LiveEvent("speech_start", self.state)]
        for retained in self.command_preroll.snapshot():
            partial = self._accept_stt(retained)
            if partial is not None:
                events.append(partial)
        return events

    def _finalize(self, reason: str) -> list[LiveEvent]:
        self.state = LiveState.FINALIZING
        started = time.perf_counter()
        cpu_started = time.process_time()
        raw = self.stt_engine.finalize()
        finalization = time.perf_counter() - started
        self._processing_seconds += finalization
        self._processing_cpu_seconds += time.process_time() - cpu_started
        result = UtteranceResult(
            raw=raw,
            normalized=normalize_transcript(raw, self.vocabulary),
            partials=tuple(self._partials),
            audio_seconds=self._utterance_samples / 16_000,
            processing_seconds=self._processing_seconds,
            finalization_latency_seconds=finalization,
            speech_end_to_final_seconds=(
                max(0.0, self._audio_position - self._last_signal_position)
                + finalization
            ),
            endpoint_reason=reason,
            processing_cpu_seconds=self._processing_cpu_seconds,
        )
        events = [LiveEvent("final", self.state, text=raw, result=result)]
        events.extend(self._return_to_idle())
        return events

    def _return_to_idle(self) -> list[LiveEvent]:
        self.state = LiveState.RETURNING_TO_IDLE
        events = [LiveEvent("returning", self.state)]
        self.stt_engine.reset()
        self.wake_detector.reset()
        self.command_vad.reset()
        self.command_preroll.clear()
        self.wake_preroll.clear()
        self._clear_utterance()
        self.state = LiveState.WAITING_FOR_WAKE
        events.append(LiveEvent("ready", self.state))
        return events

    def _recover(self, message: str) -> list[LiveEvent]:
        events = [LiveEvent("error", self.state, text=message)]
        try:
            events.extend(self._return_to_idle())
        except Exception as reset_exc:  # noqa: BLE001 - preserve the audio loop
            self.state = LiveState.WAITING_FOR_WAKE
            events.append(
                LiveEvent("error", self.state, text=f"reset failed: {reset_exc}")
            )
        return events

    def process(self, frame: AudioFrame) -> tuple[LiveEvent, ...]:
        if self.state is LiveState.STOPPED:
            raise RuntimeError("live controller has not been started")
        self._audio_position += frame.duration_seconds
        self.wake_preroll.append(frame)
        try:
            if self.state is LiveState.WAITING_FOR_WAKE:
                detection = self.wake_detector.accept_audio(frame)
                if detection is None:
                    return ()
                return tuple(self._begin_listening(detection))

            if self.state is not LiveState.LISTENING:
                return ()

            self.command_preroll.append(frame)
            elapsed = self._audio_position - self._listening_started
            if elapsed < self.acknowledgement_guard_seconds:
                return ()

            analysis = analyze_frame(
                frame,
                self.command_vad,
                clip_threshold=self.clip_threshold,
            )
            events: list[LiveEvent] = []
            just_started = False
            if not self._in_utterance and analysis.speech_active:
                events.extend(self._start_utterance())
                just_started = True
            elif self._in_utterance:
                partial = self._accept_stt(frame)
                if partial is not None:
                    events.append(partial)

            if analysis.dbfs >= self.command_vad.threshold_dbfs:
                self._last_signal_position = self._audio_position

            if not self._in_utterance:
                if elapsed >= self.no_speech_timeout_seconds:
                    events.append(LiveEvent("timeout", self.state))
                    events.extend(self._return_to_idle())
                return tuple(events)

            if self.stt_engine.endpoint_detected():
                events.extend(self._finalize("recognizer"))
            elif not analysis.speech_active and not just_started:
                events.extend(self._finalize("vad_silence"))
            elif self._audio_position - self._speech_started >= self.max_command_seconds:
                events.extend(self._finalize("max_command"))
            return tuple(events)
        except Exception as exc:  # noqa: BLE001 - state machine must recover
            return tuple(self._recover(f"live pipeline error: {exc}"))

    def audio_error(self, exc: Exception) -> tuple[LiveEvent, ...]:
        return tuple(self._recover(f"audio source error: {exc}"))

    def close(self) -> None:
        try:
            self.stt_engine.close()
        finally:
            try:
                self.wake_detector.close()
            finally:
                self.chime.close()
        self.state = LiveState.STOPPED


def run_live_source(
    source: AudioSource,
    controller: LiveVoiceController,
    *,
    on_event: LiveEventCallback | None = None,
    max_cycles: int = 0,
    max_audio_seconds: float = 0.0,
    retry_seconds: float = 1.0,
    max_audio_retries: int = 3,
) -> int:
    """Keep one capture owner open; reopen only after an actual source error."""

    cycles = 0
    audio_seconds = 0.0
    retries = 0

    def emit(events: tuple[LiveEvent, ...]) -> None:
        nonlocal cycles
        for event in events:
            if on_event is not None:
                on_event(event)
            if event.kind in {"final", "timeout"}:
                cycles += 1

    emit(controller.start())
    source.open()
    try:
        while (
            (max_cycles <= 0 or cycles < max_cycles)
            and (max_audio_seconds <= 0 or audio_seconds < max_audio_seconds)
        ):
            try:
                frame = source.read_frame()
            except AudioSourceError as exc:
                emit(controller.audio_error(exc))
                source.close()
                retries += 1
                if max_audio_retries and retries > max_audio_retries:
                    raise
                time.sleep(retry_seconds)
                source.open()
                continue
            if frame is None:
                break
            retries = 0
            audio_seconds += frame.duration_seconds
            emit(controller.process(frame))
    finally:
        source.close()
    return cycles
