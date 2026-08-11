"""Recognizer-neutral streaming wake-word contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Self

from butters.audio.model import AudioFrame


class WakeWordError(RuntimeError):
    """Raised when a local wake-word detector cannot be used."""


@dataclass(frozen=True, slots=True)
class WakeDetection:
    keyword: str
    confidence: float | None
    threshold: float
    model_latency_seconds: float | None = None
    tokens: tuple[str, ...] = ()
    token_timestamps: tuple[float, ...] = ()


class WakeWordDetector(ABC):
    """Consumes standardized PCM frames without owning the audio device."""

    @property
    @abstractmethod
    def initialization_seconds(self) -> float:
        raise NotImplementedError

    @property
    @abstractmethod
    def model_bytes(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def accept_audio(self, frame: AudioFrame) -> WakeDetection | None:
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        """Discard stream state while retaining the resident model."""

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
