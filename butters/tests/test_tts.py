from __future__ import annotations

import wave
from abc import ABC
from pathlib import Path

import pytest
from butters.tts.model import SynthesizedSpeech, TextToSpeechEngine, TTSError
from butters.tts.output import AudioOutput, WaveFileOutput
from butters.tts.sherpa_engine import SherpaOnnxPiperTTS


def test_tts_and_audio_output_are_separate_abstractions() -> None:
    assert issubclass(TextToSpeechEngine, ABC)
    assert issubclass(AudioOutput, ABC)


def test_wave_output_writes_valid_pcm_and_refuses_overwrite(tmp_path: Path) -> None:
    speech = SynthesizedSpeech(b"\x00\x00" * 1600, 16_000, 0.01)
    output = tmp_path / "speech.wav"

    WaveFileOutput().write(speech, output)

    with wave.open(str(output), "rb") as recorded:
        assert recorded.getnchannels() == 1
        assert recorded.getsampwidth() == 2
        assert recorded.getframerate() == 16_000
        assert recorded.getnframes() == 1600
    with pytest.raises(FileExistsError):
        WaveFileOutput().write(speech, output)


def test_missing_voice_fails_without_partial_output(tmp_path: Path) -> None:
    with pytest.raises(TTSError, match="exactly one"):
        SherpaOnnxPiperTTS(tmp_path / "missing")
