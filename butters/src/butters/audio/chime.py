"""Tiny non-blocking local acknowledgement chime."""

from __future__ import annotations

import math
import subprocess
import time
from array import array
from typing import Protocol


class ChimeError(RuntimeError):
    """Raised when acknowledgement playback cannot be started."""


class ChimePlayer(Protocol):
    def play(self) -> float:
        """Start playback and return call/launch latency in seconds."""

    def close(self) -> None: ...


class NullChimePlayer:
    def play(self) -> float:
        return 0.0

    def close(self) -> None:
        return None


def _tone_pcm(*, sample_rate: int, volume: float) -> bytes:
    notes = ((880.0, 0.045), (0.0, 0.010), (1174.66, 0.055))
    samples = array("h")
    amplitude = int(32767 * max(0.0, min(volume, 1.0)))
    for frequency, duration in notes:
        count = round(sample_rate * duration)
        fade = max(1, round(sample_rate * 0.006))
        for index in range(count):
            if frequency == 0.0:
                value = 0
            else:
                envelope = min(1.0, index / fade, (count - index - 1) / fade)
                value = round(
                    amplitude
                    * max(0.0, envelope)
                    * math.sin(2.0 * math.pi * frequency * index / sample_rate)
                )
            samples.append(value)
    return samples.tobytes()


class AlsaChimePlayer:
    """Launch a short raw-PCM ``aplay`` without blocking microphone reads."""

    def __init__(
        self,
        device: str,
        *,
        volume: float = 0.18,
        sample_rate: int = 48_000,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self._pcm = _tone_pcm(sample_rate=sample_rate, volume=volume)
        self._processes: list[subprocess.Popen[bytes]] = []

    def _reap(self) -> None:
        alive: list[subprocess.Popen[bytes]] = []
        for process in self._processes:
            if process.poll() is None:
                alive.append(process)
            else:
                process.wait()
        self._processes = alive

    def play(self) -> float:
        self._reap()
        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                [
                    "aplay",
                    "--quiet",
                    "--device",
                    self.device,
                    "--file-type",
                    "raw",
                    "--format",
                    "S16_LE",
                    "--rate",
                    str(self.sample_rate),
                    "--channels",
                    "1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if process.stdin is None:
                raise ChimeError("aplay did not provide an input pipe")
            process.stdin.write(self._pcm)
            process.stdin.close()
            self._processes.append(process)
        except (OSError, BrokenPipeError) as exc:
            raise ChimeError(f"cannot start acknowledgement chime: {exc}") from exc
        return time.perf_counter() - started

    def close(self) -> None:
        for process in self._processes:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self._processes.clear()
