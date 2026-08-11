"""Explicit audio outputs; normal synthesis never assumes a speaker exists."""

from __future__ import annotations

import wave
from abc import ABC, abstractmethod
from pathlib import Path

from butters.tts.model import SynthesizedSpeech


class AudioOutput(ABC):
    @abstractmethod
    def write(
        self,
        speech: SynthesizedSpeech,
        destination: Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        raise NotImplementedError


class WaveFileOutput(AudioOutput):
    def write(
        self,
        speech: SynthesizedSpeech,
        destination: Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        destination = Path(destination)
        if destination.exists() and not overwrite:
            raise FileExistsError(f"output exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(speech.sample_rate)
            output.writeframes(speech.pcm_s16le)
        return destination
