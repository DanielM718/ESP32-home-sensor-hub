"""Shared frontend consumed identically by live and file sources."""

from __future__ import annotations

from dataclasses import dataclass

from butters.audio.analysis import EnergyVad, FrameAnalysis, analyze_frame
from butters.audio.buffer import PreRollBuffer
from butters.audio.model import AudioFrame, AudioSource


@dataclass(frozen=True, slots=True)
class FrontendFrame:
    audio: AudioFrame
    analysis: FrameAnalysis


class AudioFrontend:
    def __init__(
        self,
        source: AudioSource,
        *,
        vad: EnergyVad,
        pre_roll: PreRollBuffer,
        clip_threshold: float = 0.98,
    ) -> None:
        self.source = source
        self.vad = vad
        self.pre_roll = pre_roll
        self.clip_threshold = clip_threshold

    def read(self) -> FrontendFrame | None:
        frame = self.source.read_frame()
        if frame is None:
            return None
        self.pre_roll.append(frame)
        return FrontendFrame(
            audio=frame,
            analysis=analyze_frame(
                frame,
                self.vad,
                clip_threshold=self.clip_threshold,
            ),
        )
