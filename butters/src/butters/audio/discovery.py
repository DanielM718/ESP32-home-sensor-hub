"""Read-only ALSA capture-device discovery and optional format probes."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEVICE_PATTERN = re.compile(
    r"^card (?P<card>\d+): (?P<card_id>[^ ]+) \[(?P<card_name>[^]]+)\], "
    r"device (?P<device>\d+): (?P<device_id>.*?) \[(?P<device_name>[^]]+)\]$"
)


@dataclass(frozen=True, slots=True)
class CaptureDevice:
    card: int
    device: int
    card_id: str
    card_name: str
    device_id: str
    device_name: str

    @property
    def hw_id(self) -> str:
        return f"hw:CARD={self.card_id},DEV={self.device}"

    @property
    def plughw_id(self) -> str:
        return f"plughw:CARD={self.card_id},DEV={self.device}"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    success: bool
    detail: str


def warmup_uvc_device(
    device_path: str,
    *,
    command_runner: Callable[..., Any] = subprocess.run,
) -> ProbeResult:
    """Stream one discarded low-bandwidth frame for combined-device firmware."""

    command = [
        "v4l2-ctl",
        "--device",
        device_path,
        "--set-fmt-video=width=640,height=480,pixelformat=MJPG",
        "--stream-mmap=3",
        "--stream-count=1",
        "--stream-to=/dev/null",
    ]
    try:
        result = command_runner(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult(False, str(exc))
    if result.returncode == 0:
        return ProbeResult(True, "one discarded 640x480 MJPEG frame")
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return ProbeResult(
        False,
        lines[-1] if lines else f"v4l2-ctl exited {result.returncode}",
    )


def parse_arecord_devices(output: str) -> list[CaptureDevice]:
    devices: list[CaptureDevice] = []
    for line in output.splitlines():
        match = DEVICE_PATTERN.match(line.strip())
        if not match:
            continue
        values = match.groupdict()
        devices.append(
            CaptureDevice(
                card=int(values["card"]),
                device=int(values["device"]),
                card_id=values["card_id"],
                card_name=values["card_name"],
                device_id=values["device_id"],
                device_name=values["device_name"],
            )
        )
    return devices


def list_capture_devices(
    arecord_binary: str = "arecord",
) -> tuple[list[CaptureDevice], str]:
    try:
        result = subprocess.run(
            [arecord_binary, "--list-devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [], f"unable to run {arecord_binary}: {exc}"
    return parse_arecord_devices(result.stdout), result.stdout.strip()


def usb_stream_descriptors(device: CaptureDevice) -> list[tuple[Path, str]]:
    card_dir = Path(f"/proc/asound/card{device.card}")
    descriptors: list[tuple[Path, str]] = []
    for path in sorted(card_dir.glob("stream*")):
        try:
            descriptors.append(
                (path, path.read_text(encoding="utf-8", errors="replace"))
            )
        except OSError:
            continue
    return descriptors


def probe_16k_mono(
    device_id: str,
    *,
    arecord_binary: str = "arecord",
) -> ProbeResult:
    command = [
        arecord_binary,
        "--device",
        device_id,
        "--dump-hw-params",
        "--duration",
        "1",
        "--file-type",
        "raw",
        "--format",
        "S16_LE",
        "--rate",
        "16000",
        "--channels",
        "1",
        "/dev/null",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=4,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return ProbeResult(False, str(exc))
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    lowered = result.stderr.lower()
    inaccurate_rate = "rate is not accurate" in lowered
    success = result.returncode == 0 and not inaccurate_rate
    if success:
        detail = "opened successfully at requested format"
    elif inaccurate_rate:
        detail = "hardware substituted a different rate; use plughw"
    else:
        detail = lines[-1] if lines else f"arecord exited {result.returncode}"
    return ProbeResult(success, detail)
