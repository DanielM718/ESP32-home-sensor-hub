"""TTS-neutral contracts; synthesis is independent of physical playback."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Self


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    pcm_s16le: bytes
    sample_rate: int
    generation_seconds: float
    first_chunk_seconds: float | None = None

    @property
    def sample_count(self) -> int:
        return len(self.pcm_s16le) // 2

    @property
    def audio_seconds(self) -> float:
        return self.sample_count / self.sample_rate


class TextToSpeechEngine(ABC):
    @property
    @abstractmethod
    def initialization_seconds(self) -> float:
        raise NotImplementedError

    @property
    @abstractmethod
    def model_bytes(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def synthesize(self, text: str) -> SynthesizedSpeech:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
