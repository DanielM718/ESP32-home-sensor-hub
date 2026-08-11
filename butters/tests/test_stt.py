from __future__ import annotations

import importlib.util
import math
import sys
from array import array
from pathlib import Path

import pytest
from butters.audio.analysis import EnergyVad
from butters.audio.buffer import PreRollBuffer
from butters.audio.frontend import AudioFrontend
from butters.audio.model import AudioFrame, AudioSource, SourceStats
from butters.audio.sources import WaveAudioSource
from butters.stt.model import StreamingSTTEngine, STTEngineError
from butters.stt.normalization import (
    DomainVocabulary,
    load_domain_vocabulary,
    normalize_transcript,
)
from butters.stt.session import StreamingTranscriber

from butters.config import default_stt_model_dir


def _pcm(value: int, samples: int = 320) -> bytes:
    values = array("h", [value] * samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


class _FrameSource(AudioSource):
    def __init__(self, values: list[int]) -> None:
        self._frames = [
            AudioFrame(_pcm(value), index, float(index))
            for index, value in enumerate(values)
        ]
        self._index = 0
        self._open = False
        self._stats = SourceStats()

    @property
    def stats(self) -> SourceStats:
        return self._stats

    def open(self) -> None:
        self._index = 0
        self._open = True

    def read_frame(self) -> AudioFrame | None:
        if not self._open:
            raise RuntimeError("source is closed")
        if self._index >= len(self._frames):
            return None
        frame = self._frames[self._index]
        self._index += 1
        self._stats.frames_read += 1
        self._stats.bytes_read += len(frame.pcm)
        return frame

    def close(self) -> None:
        self._open = False


class _FakeStreamingEngine(StreamingSTTEngine):
    def __init__(self, finals: list[str]) -> None:
        self.finals = finals
        self.starts = 0
        self.resets = 0
        self.accepted: list[list[AudioFrame]] = []
        self.partial = ""
        self.active = False
        self.closed = False

    @property
    def initialization_seconds(self) -> float:
        return 0.0

    def start_utterance(self) -> None:
        if self.active:
            raise STTEngineError("already active")
        self.active = True
        self.starts += 1
        self.accepted.append([])
        self.partial = ""

    def accept_audio(self, frame: AudioFrame) -> str | None:
        assert self.active
        assert frame.sample_count == 320
        self.accepted[-1].append(frame)
        changed = f"partial {self.starts}" if len(self.accepted[-1]) == 2 else None
        if changed is not None:
            self.partial = changed
        return changed

    def get_partial_transcript(self) -> str:
        return self.partial

    def endpoint_detected(self) -> bool:
        return False

    def finalize(self) -> str:
        assert self.active
        self.active = False
        return self.finals[self.starts - 1]

    def reset(self) -> None:
        self.resets += 1
        self.active = False
        self.partial = ""

    def close(self) -> None:
        self.closed = True
        self.reset()


VOCABULARY = DomainVocabulary(
    hotwords=("CO2", "MQTT"),
    aliases=(("co two", "CO2"), ("m q t t", "MQTT")),
)


def test_normalization_preserves_unknown_text() -> None:
    text = "please inspect the north window"
    assert normalize_transcript(text, VOCABULARY) == text


def test_normalization_changes_only_configured_whole_phrases() -> None:
    raw = "CO TWO and m q t t but not picotwo or AM S"
    assert normalize_transcript(raw, VOCABULARY) == (
        "CO2 and MQTT but not picotwo or AM S"
    )


def test_configured_domain_vocabulary_contains_required_terms() -> None:
    vocabulary = load_domain_vocabulary(
        Path(__file__).resolve().parents[1] / "config" / "domain_vocabulary.toml"
    )
    assert {"Butters", "SEN66", "SHT41", "MQTT", "CO2", "PM2.5"} <= set(
        vocabulary.hotwords
    )


def test_vad_endpointing_partials_finals_and_repeated_reset() -> None:
    # Two tone utterances separated and followed by enough silence to satisfy
    # the three-frame VAD release. The recognizer sees identical 20 ms S16_LE
    # frames regardless of whether the eventual source is ALSA or WAV.
    values = [0] * 3 + [8_000] * 5 + [0] * 4 + [9_000] * 5 + [0] * 4
    source = _FrameSource(values)
    frontend = AudioFrontend(
        source,
        vad=EnergyVad(threshold_dbfs=-35, attack_frames=2, release_frames=3),
        pre_roll=PreRollBuffer(frame_ms=20, duration_ms=60),
    )
    engine = _FakeStreamingEngine(["first co two", "second m q t t"])
    events = []

    with source:
        results = StreamingTranscriber(engine, VOCABULARY).run(
            frontend, on_event=events.append
        )

    assert [result.raw for result in results] == [
        "first co two",
        "second m q t t",
    ]
    assert [result.normalized for result in results] == [
        "first CO2",
        "second MQTT",
    ]
    assert [result.partials for result in results] == [
        ("partial 1",),
        ("partial 2",),
    ]
    assert all(result.endpoint_reason == "vad_silence" for result in results)
    assert engine.starts == 2
    assert engine.resets == 2
    assert engine.get_partial_transcript() == ""
    assert [event.kind for event in events].count("speech_start") == 2
    assert [event.kind for event in events].count("final") == 2


def test_exception_resets_active_recognizer_state() -> None:
    class FailingEngine(_FakeStreamingEngine):
        def accept_audio(self, frame: AudioFrame) -> str | None:
            raise RuntimeError("decode failure")

    source = _FrameSource([0, 8_000, 8_000])
    frontend = AudioFrontend(
        source,
        vad=EnergyVad(threshold_dbfs=-35, attack_frames=1, release_frames=2),
        pre_roll=PreRollBuffer(frame_ms=20, duration_ms=40),
    )
    engine = FailingEngine(["unused"])

    with source, pytest.raises(RuntimeError, match="decode failure"):
        StreamingTranscriber(engine, VOCABULARY).run(frontend)

    assert not engine.active
    assert engine.resets == 1


def test_engine_context_closes_resources() -> None:
    engine = _FakeStreamingEngine(["unused"])
    with engine:
        assert not engine.closed
    assert engine.closed


def test_real_sherpa_model_streams_partials_and_resets() -> None:
    model_dir = default_stt_model_dir()
    wav_path = model_dir / "test_wavs" / "0.wav"
    if not wav_path.is_file() or importlib.util.find_spec("sherpa_onnx") is None:
        pytest.skip("local sherpa-onnx runtime/model is not installed")

    from butters.stt.sherpa_engine import SherpaOnnxStreamingSTT

    engine = SherpaOnnxStreamingSTT(model_dir, num_threads=2)
    transcripts: list[str] = []
    partial_counts: list[int] = []
    with engine:
        for _ in range(2):
            engine.start_utterance()
            partials: list[str] = []
            with WaveAudioSource(wav_path, frame_ms=20, realtime=False) as source:
                while (frame := source.read_frame()) is not None:
                    changed = engine.accept_audio(frame)
                    if changed:
                        partials.append(changed)
            transcripts.append(engine.finalize())
            partial_counts.append(len(partials))
            engine.reset()

    assert transcripts[0]
    assert transcripts[1] == transcripts[0]
    assert all(count >= 2 for count in partial_counts)
    assert not math.isnan(engine.initialization_seconds)
