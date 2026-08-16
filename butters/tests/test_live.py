from __future__ import annotations

import importlib.util
import sys
from array import array
from pathlib import Path

import pytest
from butters.audio.analysis import EnergyVad
from butters.audio.buffer import PreRollBuffer
from butters.audio.model import AudioFrame, AudioSource, SourceStats
from butters.audio.sources import WaveAudioSource
from butters.live.controller import LiveState, LiveVoiceController, run_live_source
from butters.live.semantic import SemanticEndpointEvaluator
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.routing.router import IntentRouter
from butters.stt.model import StreamingSTTEngine, STTEngineError
from butters.stt.normalization import DomainVocabulary
from butters.wakeword.model import WakeDetection, WakeWordDetector

from butters.config import default_wakeword_model_dir
from butters.assistant_config import load_assistant_settings


def _pcm(value: int, samples: int = 320) -> bytes:
    values = array("h", [value] * samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def _frame(value: int, sequence: int) -> AudioFrame:
    return AudioFrame(_pcm(value), sequence, sequence * 0.02)


class _FakeWakeDetector(WakeWordDetector):
    def __init__(self, trigger_sequences: set[int]) -> None:
        self.trigger_sequences = trigger_sequences
        self.accepted: list[AudioFrame] = []
        self.resets = 0
        self.closed = False

    @property
    def initialization_seconds(self) -> float:
        return 0.0

    @property
    def model_bytes(self) -> int:
        return 1

    def accept_audio(self, frame: AudioFrame) -> WakeDetection | None:
        assert frame.sample_count == 320
        self.accepted.append(frame)
        if frame.sequence in self.trigger_sequences:
            return WakeDetection("HEY BUTTERS", None, 0.25)
        return None

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.closed = True


class _FakeSTT(StreamingSTTEngine):
    def __init__(self, finals: list[str]) -> None:
        self.finals = finals
        self.active = False
        self.accepted: list[list[AudioFrame]] = []
        self.starts = 0
        self.resets = 0
        self.closed = False
        self._partial = ""

    @property
    def initialization_seconds(self) -> float:
        return 0.0

    def start_utterance(self) -> None:
        if self.active:
            raise STTEngineError("already active")
        self.active = True
        self.starts += 1
        self.accepted.append([])
        self._partial = ""

    def accept_audio(self, frame: AudioFrame) -> str | None:
        if not self.active:
            raise STTEngineError("not active")
        assert frame.sample_count == 320
        self.accepted[-1].append(frame)
        if len(self.accepted[-1]) == 2:
            self._partial = f"partial {self.starts}"
            return self._partial
        return None

    def get_partial_transcript(self) -> str:
        return self._partial

    def endpoint_detected(self) -> bool:
        return False

    def finalize(self) -> str:
        if not self.active:
            raise STTEngineError("not active")
        self.active = False
        return self.finals[self.starts - 1]

    def reset(self) -> None:
        self.active = False
        self._partial = ""
        self.resets += 1

    def close(self) -> None:
        self.closed = True
        self.reset()


class _MutableSTT(_FakeSTT):
    """Expose a controllable full partial while retaining normal stream state."""

    def __init__(self, text: str) -> None:
        super().__init__([""] * 8)
        self.current_text = text

    def accept_audio(self, frame: AudioFrame) -> str | None:
        if not self.active:
            raise STTEngineError("not active")
        self.accepted[-1].append(frame)
        if self.current_text == self._partial:
            return None
        self._partial = self.current_text
        return self._partial

    def finalize(self) -> str:
        if not self.active:
            raise STTEngineError("not active")
        self.active = False
        self._partial = self.current_text
        return self.current_text


class _FakeChime:
    def __init__(self) -> None:
        self.plays = 0
        self.closed = False

    def play(self) -> float:
        self.plays += 1
        return 0.003

    def close(self) -> None:
        self.closed = True


VOCABULARY = DomainVocabulary(hotwords=("CO2",), aliases=(("co two", "CO2"),))


def _controller(
    *,
    triggers: set[int],
    finals: list[str],
    timeout: float = 0.12,
    engine: _FakeSTT | None = None,
) -> tuple[LiveVoiceController, _FakeWakeDetector, _FakeSTT, _FakeChime]:
    wake = _FakeWakeDetector(triggers)
    stt = engine or _FakeSTT(finals)
    chime = _FakeChime()
    controller = LiveVoiceController(
        wake_detector=wake,
        stt_engine=stt,
        vocabulary=VOCABULARY,
        command_vad=EnergyVad(threshold_dbfs=-35.0, attack_frames=1, release_frames=2),
        wake_preroll=PreRollBuffer(frame_ms=20, duration_ms=800),
        command_preroll=PreRollBuffer(frame_ms=20, duration_ms=100),
        chime=chime,
        no_speech_timeout_seconds=timeout,
        max_command_seconds=2.0,
        acknowledgement_guard_seconds=0.0,
        provisional_silence_seconds=0.02,
        hard_silence_seconds=0.039,
    )
    return controller, wake, stt, chime


def _kinds(controller: LiveVoiceController, values: list[int]) -> list[str]:
    kinds = [event.kind for event in controller.start()]
    for sequence, value in enumerate(values):
        kinds.extend(
            event.kind for event in controller.process(_frame(value, sequence))
        )
    return kinds


def _semantic_controller(
    text: str,
) -> tuple[LiveVoiceController, _MutableSTT]:
    settings = load_assistant_settings()
    router = IntentRouter(EntityRegistry(settings.entities), MetricRegistry())
    stt = _MutableSTT(text)
    controller = LiveVoiceController(
        wake_detector=_FakeWakeDetector({0}),
        stt_engine=stt,
        vocabulary=VOCABULARY,
        command_vad=EnergyVad(threshold_dbfs=-35.0, attack_frames=1, release_frames=15),
        wake_preroll=PreRollBuffer(frame_ms=20, duration_ms=800),
        command_preroll=PreRollBuffer(frame_ms=20, duration_ms=100),
        chime=_FakeChime(),
        no_speech_timeout_seconds=4.0,
        max_command_seconds=20.0,
        acknowledgement_guard_seconds=0.0,
        semantic_endpoint=SemanticEndpointEvaluator(router.route),
        provisional_silence_seconds=1.0,
        hard_silence_seconds=2.0,
        continuation_timeout_seconds=12.0,
    )
    return controller, stt


def _feed(
    controller: LiveVoiceController,
    sequence: list[int],
    value: int,
    count: int,
) -> list[object]:
    events = []
    for _ in range(count):
        events.extend(controller.process(_frame(value, sequence[0])))
        sequence[0] += 1
    return events


def test_wake_event_transitions_idle_to_listening_and_plays_chime() -> None:
    controller, _, _, chime = _controller(triggers={2}, finals=["unused"])
    events = list(controller.start())
    for sequence in range(3):
        events.extend(controller.process(_frame(0, sequence)))

    assert [event.kind for event in events] == ["ready", "wake", "listening"]
    assert controller.state is LiveState.LISTENING
    assert chime.plays == 1


def test_no_speech_timeout_returns_to_idle() -> None:
    controller, _, stt, _ = _controller(triggers={0}, finals=["unused"])
    kinds = _kinds(controller, [0] * 8)

    assert "timeout" in kinds
    assert kinds[-1] == "ready"
    assert controller.state is LiveState.WAITING_FOR_WAKE
    assert stt.starts == 0


@pytest.mark.parametrize("final", ["what is the co two level", ""])
def test_final_or_empty_transcript_returns_to_idle(final: str) -> None:
    controller, _, stt, _ = _controller(triggers={0}, finals=[final])
    events = list(controller.start())
    for sequence, value in enumerate([0, 9_000, 9_000, 0, 0]):
        events.extend(controller.process(_frame(value, sequence)))

    finals = [event.result for event in events if event.kind == "final"]
    assert len(finals) == 1
    assert finals[0] is not None
    assert finals[0].raw == final
    assert finals[0].normalized == ("what is the CO2 level" if final else "")
    assert events[-1].kind == "ready"
    assert controller.state is LiveState.WAITING_FOR_WAKE
    assert not stt.active


def test_local_confirmation_listens_without_a_second_wake() -> None:
    controller, _, _, chime = _controller(
        triggers={0}, finals=["turn on the heater", "yes"]
    )
    events = list(controller.start())
    sequence = [0]
    events.extend(_feed(controller, sequence, 0, 1))
    events.extend(_feed(controller, sequence, 9_000, 2))
    events.extend(_feed(controller, sequence, 0, 3))
    assert controller.state is LiveState.WAITING_FOR_WAKE

    controller.await_local_confirmation()
    assert controller.state is LiveState.AWAITING_CONTINUATION
    events.extend(_feed(controller, sequence, 9_000, 2))
    events.extend(_feed(controller, sequence, 0, 3))

    finals = [event.result.raw for event in events if event.result is not None]
    assert finals == ["turn on the heater", "yes"]
    assert sum(event.kind == "wake" for event in events) == 1
    assert chime.plays == 1
    assert controller.state is LiveState.WAITING_FOR_WAKE


def test_stt_error_recovers_safely_to_idle() -> None:
    class FailingSTT(_FakeSTT):
        def accept_audio(self, frame: AudioFrame) -> str | None:
            raise STTEngineError("decoder failed")

    failing = FailingSTT(["unused"])
    controller, _, _, _ = _controller(triggers={0}, finals=["unused"], engine=failing)
    events = list(controller.start())
    for sequence, value in enumerate([0, 9_000]):
        events.extend(controller.process(_frame(value, sequence)))

    errors = [event.text for event in events if event.kind == "error"]
    assert any("decoder failed" in error for error in errors)
    assert events[-1].kind == "ready"
    assert controller.state is LiveState.WAITING_FOR_WAKE
    assert not failing.active


def test_repeated_interactions_have_no_stale_partial_state() -> None:
    controller, wake, stt, _ = _controller(triggers={0, 6}, finals=["first", "second"])
    events = list(controller.start())
    values = [0, 8_000, 8_000, 0, 0, 0, 0, 9_000, 9_000, 0, 0]
    for sequence, value in enumerate(values):
        events.extend(controller.process(_frame(value, sequence)))

    results = [event.result for event in events if event.kind == "final"]
    assert [result.raw for result in results if result is not None] == [
        "first",
        "second",
    ]
    assert [result.partials for result in results if result is not None] == [
        ("partial 1",),
        ("partial 2",),
    ]
    assert stt.starts == 2
    assert wake.resets >= 3
    assert controller.state is LiveState.WAITING_FOR_WAKE


def test_idle_preroll_remains_bounded_and_close_releases_workers() -> None:
    controller, wake, stt, chime = _controller(triggers=set(), finals=[])
    controller.start()
    for sequence in range(500):
        controller.process(_frame(0, sequence))

    assert len(controller.wake_preroll) == 40
    assert controller.wake_preroll.bytes_retained == 40 * 640
    controller.close()
    assert controller.state is LiveState.STOPPED
    assert wake.closed and stt.closed and chime.closed


def test_repeated_live_cycles_keep_one_audio_capture_owner() -> None:
    class SequenceSource(AudioSource):
        def __init__(self, values: list[int]) -> None:
            self.frames = [_frame(value, i) for i, value in enumerate(values)]
            self.index = 0
            self.opens = 0
            self.closes = 0
            self._stats = SourceStats()

        @property
        def stats(self) -> SourceStats:
            return self._stats

        def open(self) -> None:
            self.opens += 1

        def read_frame(self) -> AudioFrame | None:
            if self.index >= len(self.frames):
                return None
            frame = self.frames[self.index]
            self.index += 1
            self._stats.frames_read += 1
            self._stats.bytes_read += len(frame.pcm)
            return frame

        def close(self) -> None:
            self.closes += 1

    values = [0, 8_000, 8_000, 0, 0, 0, 0, 9_000, 9_000, 0, 0]
    source = SequenceSource(values)
    controller, _, stt, _ = _controller(triggers={0, 6}, finals=["first", "second"])

    cycles = run_live_source(source, controller, max_cycles=2)

    assert cycles == 2
    assert stt.starts == 2
    assert source.opens == 1
    assert source.closes == 1
    assert source.stats.frames_read == len(values)


def test_complete_command_finalizes_at_provisional_semantic_endpoint() -> None:
    controller, _ = _semantic_controller("what is the humidity in filament box two")
    sequence = [0]
    events = list(controller.start())
    events.extend(_feed(controller, sequence, 0, 1))  # wake
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 52))

    finals = [event for event in events if event.kind == "final"]
    assert len(finals) == 1
    assert finals[0].result is not None
    assert finals[0].result.endpoint_reason == "semantic_complete"
    assert finals[0].result.semantic_status == "complete"
    assert 1.0 <= finals[0].result.speech_end_to_final_seconds < 1.2
    assert finals[0].completes_cycle
    assert controller.state is LiveState.WAITING_FOR_WAKE


def test_incomplete_request_survives_short_and_provisional_pauses() -> None:
    controller, stt = _semantic_controller("what is the humidity")
    sequence = [0]
    events = list(controller.start())
    events.extend(_feed(controller, sequence, 0, 1))
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 40))  # 0.8 seconds
    assert not any(event.kind == "final" for event in events)

    events.extend(_feed(controller, sequence, 0, 35))  # 1.5 seconds total
    assert not any(event.kind == "final" for event in events)
    assert controller.state is LiveState.LISTENING

    stt.current_text = "what is the humidity in filament box two"
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 52))

    finals = [event for event in events if event.kind == "final"]
    assert len(finals) == 1
    assert finals[0].result is not None
    assert finals[0].result.semantic_status == "complete"


def test_hard_endpoint_prompts_for_targeted_bounded_continuation() -> None:
    controller, stt = _semantic_controller("what is the humidity")
    sequence = [0]
    events = list(controller.start())
    events.extend(_feed(controller, sequence, 0, 1))
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 102))

    finals = [event for event in events if event.kind == "final"]
    prompts = [event for event in events if event.kind == "awaiting_continuation"]
    assert len(finals) == 1 and not finals[0].completes_cycle
    assert finals[0].result is not None
    assert finals[0].result.endpoint_reason == "acoustic_hard"
    assert finals[0].result.semantic_status == "incomplete"
    assert len(prompts) == 1
    assert "Which sensor" in prompts[0].text
    assert controller.state is LiveState.AWAITING_CONTINUATION

    stt.current_text = "filament box two"
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 52))

    finals = [event for event in events if event.kind == "final"]
    assert len(finals) == 2
    assert finals[-1].result is not None
    assert finals[-1].result.effective_text == "filament box two"
    assert finals[-1].result.semantic_status == "complete"
    assert finals[-1].completes_cycle
    assert controller.state is LiveState.WAITING_FOR_WAKE


def test_complete_unrelated_command_replaces_pending_live_fragment() -> None:
    controller, stt = _semantic_controller("what is the humidity")
    sequence = [0]
    events = list(controller.start())
    events.extend(_feed(controller, sequence, 0, 1))
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 102))
    assert controller.state is LiveState.AWAITING_CONTINUATION

    stt.current_text = "what is the server status"
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 52))

    finals = [event for event in events if event.kind == "final"]
    assert finals[-1].result is not None
    assert finals[-1].result.effective_text == "what is the server status"
    assert "humidity" not in finals[-1].result.effective_text
    assert controller.state is LiveState.WAITING_FOR_WAKE


def test_unrecognized_hard_endpoint_and_continuation_timeout_are_safe() -> None:
    controller, _ = _semantic_controller("y level")
    sequence = [0]
    events = list(controller.start())
    events.extend(_feed(controller, sequence, 0, 1))
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 102))

    finals = [event for event in events if event.kind == "final"]
    assert len(finals) == 1
    assert finals[0].result is not None
    assert finals[0].result.semantic_status == "unrecognized"
    assert controller.state is LiveState.WAITING_FOR_WAKE

    controller, stt = _semantic_controller("what is the humidity")
    sequence = [0]
    events = list(controller.start())
    events.extend(_feed(controller, sequence, 0, 1))
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 102))
    assert controller.state is LiveState.AWAITING_CONTINUATION
    stt.current_text = "y level"
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 102))
    finals = [event for event in events if event.kind == "final"]
    assert finals[-1].result is not None
    assert finals[-1].result.semantic_status == "incomplete"
    assert finals[-1].result.effective_text == "y level"
    assert controller.state is LiveState.AWAITING_CONTINUATION
    events.extend(_feed(controller, sequence, 0, 601))
    assert [event.kind for event in events].count("continuation_timeout") == 1
    assert controller.state is LiveState.WAITING_FOR_WAKE

    controller, _ = _semantic_controller("what is the humidity")
    sequence = [0]
    events = list(controller.start())
    events.extend(_feed(controller, sequence, 0, 1))
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 102))
    assert controller.state is LiveState.AWAITING_CONTINUATION
    events.extend(_feed(controller, sequence, 0, 601))

    assert [event.kind for event in events].count("continuation_timeout") == 1
    assert controller.state is LiveState.WAITING_FOR_WAKE


def test_extra_silence_after_final_never_duplicates_final_event() -> None:
    controller, _ = _semantic_controller(
        "what is the carbon dioxide level in the printer room"
    )
    sequence = [0]
    events = list(controller.start())
    events.extend(_feed(controller, sequence, 0, 1))
    events.extend(_feed(controller, sequence, 9_000, 3))
    events.extend(_feed(controller, sequence, 0, 150))

    assert [event.kind for event in events].count("final") == 1


def test_real_sherpa_keyword_detector_streams_and_resets(tmp_path: Path) -> None:
    model_dir = default_wakeword_model_dir()
    wav_path = model_dir / "test_wavs" / "en_0.wav"
    if not wav_path.is_file() or importlib.util.find_spec("sherpa_onnx") is None:
        pytest.skip("local sherpa-onnx wake runtime/model is not installed")
    keywords = tmp_path / "keywords.txt"
    keywords.write_text("L AY1 T AH1 P @LIGHT_UP\n", encoding="utf-8")
    from butters.wakeword.sherpa_detector import SherpaOnnxWakeWordDetector

    detector = SherpaOnnxWakeWordDetector(
        model_dir, keywords, chunk_size=8, num_threads=1
    )
    found: list[str] = []
    with detector:
        for _ in range(2):
            with WaveAudioSource(wav_path, frame_ms=20) as source:
                while (frame := source.read_frame()) is not None:
                    detection = detector.accept_audio(frame)
                    if detection is not None:
                        found.append(detection.keyword)
                        assert detection.confidence is None
                        assert detection.model_latency_seconds is not None
                        break
            detector.reset()

    assert found == ["LIGHT UP", "LIGHT UP"]
