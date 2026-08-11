"""Reusable diagnostic and finite-recording operations."""

from __future__ import annotations

import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from butters.audio.frontend import AudioFrontend, FrontendFrame
from butters.audio.model import INTERNAL_AUDIO_FORMAT, AudioSource


@dataclass(frozen=True, slots=True)
class RecordingResult:
    path: Path
    samples: int
    frames: int

    @property
    def duration_seconds(self) -> float:
        return self.samples / INTERNAL_AUDIO_FORMAT.sample_rate


def record_standard_wav(
    source: AudioSource,
    output_path: Path,
    *,
    duration_seconds: float,
    overwrite: bool = False,
    on_frame: Callable[[FrontendFrame], None] | None = None,
    frontend: AudioFrontend | None = None,
) -> RecordingResult:
    output_path = Path(output_path)
    if duration_seconds <= 0:
        raise ValueError("recording duration must be positive")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite {output_path}")
    if not output_path.parent.is_dir():
        raise FileNotFoundError(
            f"output directory does not exist: {output_path.parent}"
        )
    sample_limit = round(duration_seconds * INTERNAL_AUDIO_FORMAT.sample_rate)
    samples_written = 0
    frames_written = 0
    with source, wave.open(str(output_path), "wb") as output:
        output.setnchannels(INTERNAL_AUDIO_FORMAT.channels)
        output.setsampwidth(INTERNAL_AUDIO_FORMAT.sample_width)
        output.setframerate(INTERNAL_AUDIO_FORMAT.sample_rate)
        while samples_written < sample_limit:
            if frontend is None:
                frame = source.read_frame()
                frontend_frame = None
            else:
                frontend_frame = frontend.read()
                frame = frontend_frame.audio if frontend_frame else None
            if frame is None:
                break
            remaining = sample_limit - samples_written
            pcm = frame.pcm[: remaining * INTERNAL_AUDIO_FORMAT.sample_width]
            output.writeframesraw(pcm)
            written = len(pcm) // INTERNAL_AUDIO_FORMAT.sample_width
            samples_written += written
            frames_written += 1
            if on_frame is not None and frontend_frame is not None:
                on_frame(frontend_frame)
    return RecordingResult(output_path, samples_written, frames_written)
