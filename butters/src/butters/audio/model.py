"""Common types shared by live and development audio sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class AudioFormat:
    sample_rate: int
    channels: int
    sample_width: int

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width


INTERNAL_AUDIO_FORMAT = AudioFormat(
    sample_rate=16_000,
    channels=1,
    sample_width=2,
)


@dataclass(frozen=True, slots=True)
class AudioFrame:
    pcm: bytes
    sequence: int
    captured_monotonic: float
    overflowed: bool = False

    def __post_init__(self) -> None:
        if len(self.pcm) % INTERNAL_AUDIO_FORMAT.sample_width:
            raise ValueError("PCM frame must contain complete signed 16-bit samples")

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // INTERNAL_AUDIO_FORMAT.sample_width

    @property
    def duration_seconds(self) -> float:
        return self.sample_count / INTERNAL_AUDIO_FORMAT.sample_rate


@dataclass(slots=True)
class SourceStats:
    frames_read: int = 0
    bytes_read: int = 0
    overruns: int = 0
    dropped_frames: int = 0
    max_buffer_bytes: int = 0
    last_error: str | None = None


class AudioSourceError(RuntimeError):
    """Raised when an audio source cannot provide the standardized stream."""


class AudioSource(ABC):
    """Pull-based source that always emits 16 kHz mono signed 16-bit PCM."""

    format = INTERNAL_AUDIO_FORMAT

    @property
    @abstractmethod
    def stats(self) -> SourceStats:
        raise NotImplementedError

    @abstractmethod
    def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def read_frame(self) -> AudioFrame | None:
        """Return the next frame, or None at a finite source's end."""

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
