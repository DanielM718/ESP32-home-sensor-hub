"""Low-cost audio diagnostics and an energy-based VAD gate."""

from __future__ import annotations

import math
import sys
from array import array
from dataclasses import dataclass

from butters.audio.model import AudioFrame


@dataclass(frozen=True, slots=True)
class FrameAnalysis:
    rms: float
    dbfs: float
    peak: int
    clipping_samples: int
    speech_active: bool

    @property
    def clipping(self) -> bool:
        return self.clipping_samples > 0


class EnergyVad:
    """Deterministic energy gate with attack and release hysteresis.

    This is useful for milestone diagnostics. It is not a trained speech/noise
    classifier and is not intended to replace the future wake-word gate.
    """

    def __init__(
        self,
        *,
        threshold_dbfs: float = -42.0,
        attack_frames: int = 2,
        release_frames: int = 15,
    ) -> None:
        self.threshold_dbfs = threshold_dbfs
        self.attack_frames = max(1, attack_frames)
        self.release_frames = max(1, release_frames)
        self._above_frames = 0
        self._below_frames = 0
        self.active = False

    def reset(self) -> None:
        """Clear hysteresis state without changing the configured thresholds."""

        self._above_frames = 0
        self._below_frames = 0
        self.active = False

    def update(self, dbfs: float) -> bool:
        if dbfs >= self.threshold_dbfs:
            self._above_frames += 1
            self._below_frames = 0
            if self._above_frames >= self.attack_frames:
                self.active = True
        else:
            self._above_frames = 0
            self._below_frames += 1
            if self._below_frames >= self.release_frames:
                self.active = False
        return self.active


def analyze_frame(
    frame: AudioFrame,
    vad: EnergyVad,
    *,
    clip_threshold: float = 0.98,
) -> FrameAnalysis:
    samples = array("h")
    samples.frombytes(frame.pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return FrameAnalysis(0.0, float("-inf"), 0, 0, vad.update(float("-inf")))
    square_sum = sum(sample * sample for sample in samples)
    rms = math.sqrt(square_sum / len(samples))
    dbfs = 20.0 * math.log10(rms / 32768.0) if rms else float("-inf")
    peak = max(abs(sample) for sample in samples)
    clip_level = int(32767 * clip_threshold)
    clipping_samples = sum(abs(sample) >= clip_level for sample in samples)
    return FrameAnalysis(
        rms=rms,
        dbfs=dbfs,
        peak=peak,
        clipping_samples=clipping_samples,
        speech_active=vad.update(dbfs),
    )
