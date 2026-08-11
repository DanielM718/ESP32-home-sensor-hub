"""Recognizer-neutral streaming STT contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

from butters.audio.model import AudioFrame


class STTEngineError(RuntimeError):
    """Raised when a streaming recognizer cannot be used."""


class StreamingSTTEngine(ABC):
    """Incremental recognizer interface used by the rest of Butters.

    Implementations consume the already standardized 16 kHz mono signed
    16-bit frames. No caller needs to know which inference runtime is used.
    """

    @property
    @abstractmethod
    def initialization_seconds(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def start_utterance(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def accept_audio(self, frame: AudioFrame) -> str | None:
        """Accept one frame and return a changed partial, or None."""

    @abstractmethod
    def get_partial_transcript(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def endpoint_detected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def finalize(self) -> str:
        """Finish the active stream and return its raw transcript."""

    @abstractmethod
    def reset(self) -> None:
        """Discard utterance state while retaining the resident model."""

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
