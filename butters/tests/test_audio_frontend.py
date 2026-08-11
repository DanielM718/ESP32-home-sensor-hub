from __future__ import annotations

import io
import math
import sys
import wave
from array import array
from pathlib import Path

import pytest
from butters.audio.analysis import EnergyVad, analyze_frame
from butters.audio.buffer import PreRollBuffer
from butters.audio.discovery import parse_arecord_devices, warmup_uvc_device
from butters.audio.model import AudioFrame, AudioSourceError
from butters.audio.operations import record_standard_wav
from butters.audio.sources import AlsaAudioSource, WaveAudioSource


def _pcm_bytes(values: array) -> bytes:
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def _write_stereo_wave(path: Path, *, rate: int = 48_000, seconds: float = 0.4) -> None:
    frame_count = round(rate * seconds)
    samples = array("h")
    for index in range(frame_count):
        elapsed = index / rate
        amplitude = 0 if elapsed < 0.1 else 10_000
        value = round(amplitude * math.sin(2 * math.pi * 440 * elapsed))
        samples.extend((value, value // 2))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(_pcm_bytes(samples))


def _frame(value: int, sequence: int = 0) -> AudioFrame:
    return AudioFrame(
        pcm=_pcm_bytes(array("h", [value] * 320)),
        sequence=sequence,
        captured_monotonic=0.0,
    )


def test_wave_source_emits_standard_frames_and_bounded_conversion_buffer(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "stereo-48k.wav"
    _write_stereo_wave(input_path)
    source = WaveAudioSource(input_path, frame_ms=20, read_chunk_frames=257)

    with source:
        frames = list(iter(source.read_frame, None))

    assert sum(frame.sample_count for frame in frames) == 6_400
    assert all(len(frame.pcm) <= 640 for frame in frames)
    assert all(frame.sample_count > 0 for frame in frames)
    assert source.input_rate == 48_000
    assert source.input_channels == 2
    assert source.input_sample_width == 2
    assert source.stats.max_buffer_bytes <= 1_024


def test_energy_vad_reports_attack_release_and_clipping() -> None:
    vad = EnergyVad(threshold_dbfs=-35.0, attack_frames=2, release_frames=2)
    silence = analyze_frame(_frame(0), vad)
    first_loud = analyze_frame(_frame(12_000, 1), vad)
    second_loud = analyze_frame(_frame(32_767, 2), vad)
    first_quiet = analyze_frame(_frame(0, 3), vad)
    second_quiet = analyze_frame(_frame(0, 4), vad)

    assert not silence.speech_active
    assert not first_loud.speech_active
    assert second_loud.speech_active
    assert second_loud.clipping
    assert first_quiet.speech_active
    assert not second_quiet.speech_active


def test_preroll_buffer_never_exceeds_configured_duration() -> None:
    buffer = PreRollBuffer(frame_ms=20, duration_ms=800)
    for sequence in range(500):
        buffer.append(_frame(sequence % 100, sequence))

    assert len(buffer) == 40
    assert buffer.max_frames == 40
    assert buffer.bytes_retained == 40 * 640
    assert buffer.snapshot()[0].sequence == 460


def test_recording_utility_writes_valid_standard_wav(tmp_path: Path) -> None:
    input_path = tmp_path / "input.wav"
    output_path = tmp_path / "recorded.wav"
    _write_stereo_wave(input_path, rate=44_100, seconds=0.25)
    source = WaveAudioSource(input_path, frame_ms=20)

    result = record_standard_wav(
        source,
        output_path,
        duration_seconds=0.12,
    )

    assert result.samples == 1_920
    with wave.open(str(output_path), "rb") as recorded:
        assert recorded.getnchannels() == 1
        assert recorded.getsampwidth() == 2
        assert recorded.getframerate() == 16_000
        assert recorded.getnframes() == 1_920


def test_repeated_wave_startup_shutdown_releases_file(tmp_path: Path) -> None:
    path = tmp_path / "repeat.wav"
    _write_stereo_wave(path, seconds=0.05)
    source = WaveAudioSource(path)

    for _ in range(5):
        source.open()
        assert source.read_frame() is not None
        source.close()
        renamed = path.with_suffix(".moved")
        path.rename(renamed)
        renamed.rename(path)


class _FakeProcess:
    def __init__(self, pcm: bytes, stderr: bytes = b"overrun!!!\n") -> None:
        self.stdout = io.BytesIO(pcm)
        self.stderr = io.BytesIO(stderr)
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_repeated_alsa_startup_shutdown_cleans_subprocess() -> None:
    processes: list[_FakeProcess] = []

    def factory(command: list[str], **kwargs: object) -> _FakeProcess:
        assert "S16_LE" in command
        assert "16000" in command
        process = _FakeProcess(_frame(1_000).pcm)
        processes.append(process)
        return process

    source = AlsaAudioSource("plughw:CARD=Fake,DEV=0", process_factory=factory)
    for _ in range(5):
        with source:
            frame = source.read_frame()
            assert frame is not None
            assert len(frame.pcm) == 640

    assert len(processes) == 5
    assert all(process.terminated for process in processes)
    assert source.stats.overruns == 1
    assert source.stats.dropped_frames == 1
    with pytest.raises(AudioSourceError, match="not open"):
        source.read_frame()


def test_optional_uvc_warmup_is_argv_scoped_and_nonfatal() -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 1
        stderr = "VIDIOC_STREAMON failed"

    def runner(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        return Result()

    process = _FakeProcess(_frame(1_000).pcm, stderr=b"")
    source = AlsaAudioSource(
        "hw:CARD=Camera,DEV=0",
        video_warmup_device="/dev/v4l/by-id/stable-camera",
        command_runner=runner,
        process_factory=lambda *args, **kwargs: process,
    )

    with source:
        assert source.read_frame() is not None

    assert calls == [
        [
            "v4l2-ctl",
            "--device",
            "/dev/v4l/by-id/stable-camera",
            "--set-fmt-video=width=640,height=480,pixelformat=MJPG",
            "--stream-mmap=3",
            "--stream-count=1",
            "--stream-to=/dev/null",
        ]
    ]
    assert source.stats.last_error == (
        "optional UVC warm-up failed: VIDIOC_STREAMON failed"
    )


def test_uvc_warmup_reports_success() -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    def runner(command: list[str], **kwargs: object) -> Result:
        calls.append(command)
        return Result()

    result = warmup_uvc_device("/dev/v4l/by-id/camera", command_runner=runner)

    assert result.success
    assert calls[0][0:3] == [
        "v4l2-ctl",
        "--device",
        "/dev/v4l/by-id/camera",
    ]


def test_arecord_device_listing_parser_does_not_assume_first_device() -> None:
    output = """\
**** List of CAPTURE Hardware Devices ****
card 1: Camera [Cheap USB Camera], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
card 3: Array [Better Microphone Array], device 2: Capture PCM [Capture PCM]
"""
    devices = parse_arecord_devices(output)

    assert [device.card for device in devices] == [1, 3]
    assert devices[0].plughw_id == "plughw:CARD=Camera,DEV=0"
    assert devices[1].device == 2
