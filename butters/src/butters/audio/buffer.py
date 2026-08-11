"""Bounded audio history for future wake-word pre-roll."""

from __future__ import annotations

import math
from collections import deque

from butters.audio.model import AudioFrame


class PreRollBuffer:
    def __init__(self, *, frame_ms: int, duration_ms: int) -> None:
        if frame_ms <= 0 or duration_ms < 0:
            raise ValueError("invalid pre-roll duration")
        self.max_frames = math.ceil(duration_ms / frame_ms) if duration_ms else 0
        self._frames: deque[AudioFrame] = deque(maxlen=self.max_frames)

    def append(self, frame: AudioFrame) -> None:
        self._frames.append(frame)

    def snapshot(self) -> tuple[AudioFrame, ...]:
        return tuple(self._frames)

    def clear(self) -> None:
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def bytes_retained(self) -> int:
        return sum(len(frame.pcm) for frame in self._frames)
