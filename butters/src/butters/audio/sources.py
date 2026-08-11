"""ALSA and WAV implementations of the common audio source interface."""

from __future__ import annotations

import subprocess
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from butters.audio.conversion import StreamingPcmConverter
from butters.audio.discovery import warmup_uvc_device
from butters.audio.model import (
    INTERNAL_AUDIO_FORMAT,
    AudioFrame,
    AudioSource,
    AudioSourceError,
    SourceStats,
)


class WaveAudioSource(AudioSource):
    """Stream and standardize PCM WAV data without loading the file at once."""

    def __init__(
        self,
        path: Path,
        *,
        frame_ms: int = 20,
        realtime: bool = False,
        loop: bool = False,
        read_chunk_frames: int = 4096,
    ) -> None:
        self.path = Path(path)
        self.frame_samples = INTERNAL_AUDIO_FORMAT.sample_rate * frame_ms // 1000
        self.frame_bytes = self.frame_samples * INTERNAL_AUDIO_FORMAT.sample_width
        self.realtime = realtime
        self.loop = loop
        self.read_chunk_frames = read_chunk_frames
        self._wave: wave.Wave_read | None = None
        self._converter: StreamingPcmConverter | None = None
        self._pending = bytearray()
        self._stats = SourceStats()
        self._sequence = 0
        self._emitted_samples = 0
        self._started_monotonic = 0.0
        self._at_end = False
        self.input_rate: int | None = None
        self.input_channels: int | None = None
        self.input_sample_width: int | None = None

    @property
    def stats(self) -> SourceStats:
        return self._stats

    def open(self) -> None:
        if self._wave is not None:
            raise AudioSourceError("WAV source is already open")
        self._stats = SourceStats()
        self._sequence = 0
        self._emitted_samples = 0
        self._pending.clear()
        self._at_end = False
        try:
            # This handle intentionally remains open for the streaming source lifetime.
            self._wave = wave.open(str(self.path), "rb")  # noqa: SIM115
        except (FileNotFoundError, wave.Error, OSError) as exc:
            raise AudioSourceError(
                f"cannot open WAV source {self.path}: {exc}"
            ) from exc
        try:
            if self._wave.getcomptype() != "NONE":
                raise AudioSourceError("compressed WAV files are not supported")
            if self._wave.getnframes() == 0:
                raise AudioSourceError("WAV source contains no audio frames")
            self.input_rate = self._wave.getframerate()
            self.input_channels = self._wave.getnchannels()
            self.input_sample_width = self._wave.getsampwidth()
            self._new_converter()
        except Exception:
            self.close()
            raise
        self._started_monotonic = time.monotonic()

    def _new_converter(self) -> None:
        assert self.input_rate is not None
        assert self.input_channels is not None
        assert self.input_sample_width is not None
        try:
            self._converter = StreamingPcmConverter(
                input_rate=self.input_rate,
                input_channels=self.input_channels,
                input_sample_width=self.input_sample_width,
            )
        except ValueError as exc:
            raise AudioSourceError(f"unsupported WAV format: {exc}") from exc

    def _read_more(self) -> None:
        assert self._wave is not None
        assert self._converter is not None
        raw = self._wave.readframes(self.read_chunk_frames)
        if raw:
            self._pending.extend(self._converter.convert(raw))
        else:
            self._pending.extend(self._converter.finish())
            if self.loop:
                self._wave.rewind()
                self._new_converter()
            else:
                self._at_end = True
        self._stats.max_buffer_bytes = max(
            self._stats.max_buffer_bytes,
            len(self._pending),
        )

    def read_frame(self) -> AudioFrame | None:
        if self._wave is None:
            raise AudioSourceError("WAV source is not open")
        while len(self._pending) < self.frame_bytes and not self._at_end:
            self._read_more()
        if not self._pending:
            return None

        byte_count = min(self.frame_bytes, len(self._pending))
        pcm = bytes(self._pending[:byte_count])
        del self._pending[:byte_count]
        if self.realtime:
            deadline = (
                self._started_monotonic
                + self._emitted_samples / INTERNAL_AUDIO_FORMAT.sample_rate
            )
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        frame = AudioFrame(
            pcm=pcm,
            sequence=self._sequence,
            captured_monotonic=time.monotonic(),
        )
        self._sequence += 1
        self._emitted_samples += frame.sample_count
        self._stats.frames_read += 1
        self._stats.bytes_read += len(pcm)
        return frame

    def close(self) -> None:
        if self._wave is not None:
            self._wave.close()
        self._wave = None
        self._converter = None
        self._pending.clear()
        self._at_end = False


class AlsaAudioSource(AudioSource):
    """Capture standardized raw PCM through the mature ALSA arecord utility."""

    def __init__(
        self,
        device: str,
        *,
        frame_ms: int = 20,
        arecord_binary: str = "arecord",
        process_factory: Callable[..., Any] = subprocess.Popen,
        video_warmup_device: str | None = None,
        command_runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        if not device:
            raise ValueError("an ALSA device must be selected explicitly")
        self.device = device
        self.frame_samples = INTERNAL_AUDIO_FORMAT.sample_rate * frame_ms // 1000
        self.frame_bytes = self.frame_samples * INTERNAL_AUDIO_FORMAT.sample_width
        self.arecord_binary = arecord_binary
        self._process_factory = process_factory
        self.video_warmup_device = video_warmup_device
        self._command_runner = command_runner
        self._process: Any | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._stats = SourceStats()
        self._sequence = 0
        self._closing = False

    @property
    def stats(self) -> SourceStats:
        return self._stats

    @property
    def command(self) -> list[str]:
        return [
            self.arecord_binary,
            "--device",
            self.device,
            "--file-type",
            "raw",
            "--format",
            "S16_LE",
            "--rate",
            str(INTERNAL_AUDIO_FORMAT.sample_rate),
            "--channels",
            str(INTERNAL_AUDIO_FORMAT.channels),
            "--period-size",
            str(self.frame_samples),
            "-",
        ]

    def _warm_up_video_interface(self) -> None:
        if not self.video_warmup_device:
            return
        result = warmup_uvc_device(
            self.video_warmup_device,
            command_runner=self._command_runner,
        )
        if not result.success:
            self._stats.last_error = f"optional UVC warm-up failed: {result.detail}"

    def open(self) -> None:
        if self._process is not None:
            raise AudioSourceError("ALSA source is already open")
        self._stats = SourceStats()
        self._sequence = 0
        self._stderr_lines.clear()
        self._closing = False
        self._warm_up_video_interface()
        try:
            self._process = self._process_factory(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except (FileNotFoundError, OSError) as exc:
            raise AudioSourceError(f"cannot start arecord: {exc}") from exc
        if self._process.stdout is None or self._process.stderr is None:
            self.close()
            raise AudioSourceError("arecord did not expose its audio pipes")
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="butters-arecord-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self._process is not None
        for raw_line in iter(self._process.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            self._stderr_lines.append(line)
            lowered = line.lower()
            if "overrun" in lowered or "xrun" in lowered:
                self._stats.overruns += 1
                self._stats.dropped_frames += 1
            if "error" in lowered:
                self._stats.last_error = line

    def read_frame(self) -> AudioFrame | None:
        if self._process is None or self._process.stdout is None:
            raise AudioSourceError("ALSA source is not open")
        chunks: list[bytes] = []
        remaining = self.frame_bytes
        while remaining:
            chunk = self._process.stdout.read(remaining)
            if not chunk:
                if self._closing:
                    return None
                status = self._process.poll()
                detail = self._stderr_lines[-1] if self._stderr_lines else "no details"
                raise AudioSourceError(
                    f"arecord stopped unexpectedly (status {status}): {detail}"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        pcm = b"".join(chunks)
        frame = AudioFrame(
            pcm=pcm,
            sequence=self._sequence,
            captured_monotonic=time.monotonic(),
            overflowed=self._stats.overruns > 0,
        )
        self._sequence += 1
        self._stats.frames_read += 1
        self._stats.bytes_read += len(pcm)
        return frame

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._closing = True
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for pipe_name in ("stdout", "stderr"):
            pipe = getattr(process, pipe_name, None)
            if pipe is not None:
                pipe.close()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        self._stderr_thread = None
        self._process = None
