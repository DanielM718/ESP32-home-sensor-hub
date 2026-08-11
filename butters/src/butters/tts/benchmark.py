"""Repeatable, local-only TTS resource benchmark."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from butters.assistant_config import load_assistant_settings
from butters.tts.sherpa_engine import SherpaOnnxPiperTTS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark-tts")
    parser.add_argument("--assistant-config", type=Path)
    parser.add_argument("--threads", type=int)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--idle-seconds", type=float, default=5.0)
    parser.add_argument(
        "--text",
        default="Printer room carbon dioxide is 742 parts per million.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    if args.idle_seconds < 0:
        raise SystemExit("--idle-seconds cannot be negative")
    settings = load_assistant_settings(args.assistant_config)
    tts = settings.tts
    if args.threads is not None:
        tts = replace(tts, num_threads=args.threads).validated()

    system_before = _system_snapshot()
    rss_before = _rss_bytes()
    load_wall = time.perf_counter()
    load_cpu = time.process_time()
    engine = SherpaOnnxPiperTTS(
        tts.model_dir,
        num_threads=tts.num_threads,
        speed=tts.speed,
        max_text_chars=tts.max_text_chars,
    )
    load_wall = time.perf_counter() - load_wall
    load_cpu = time.process_time() - load_cpu
    rss_loaded = _rss_bytes()

    idle_wall = time.perf_counter()
    idle_cpu = time.process_time()
    time.sleep(args.idle_seconds)
    idle_wall = time.perf_counter() - idle_wall
    idle_cpu = time.process_time() - idle_cpu

    runs: list[dict[str, float | int]] = []
    active_wall = time.perf_counter()
    active_cpu = time.process_time()
    try:
        for index in range(args.repeats):
            rss_run_before = _rss_bytes()
            run_wall = time.perf_counter()
            run_cpu = time.process_time()
            speech = engine.synthesize(args.text)
            run_wall = time.perf_counter() - run_wall
            run_cpu = time.process_time() - run_cpu
            runs.append(
                {
                    "index": index + 1,
                    "wall_seconds": run_wall,
                    "cpu_seconds": run_cpu,
                    "cpu_percent": run_cpu / max(run_wall, 1e-9) * 100,
                    "audio_seconds": speech.audio_seconds,
                    "real_time_factor": run_wall / max(speech.audio_seconds, 1e-9),
                    "rss_before_bytes": rss_run_before,
                    "rss_after_bytes": _rss_bytes(),
                }
            )
    finally:
        engine.close()
    active_wall = time.perf_counter() - active_wall
    active_cpu = time.process_time() - active_cpu
    system_after = _system_snapshot()
    payload = {
        "model": tts.model_dir.name,
        "model_directory_bytes": engine.model_bytes,
        "onnx_bytes": sum(path.stat().st_size for path in tts.model_dir.glob("*.onnx")),
        "threads": tts.num_threads,
        "repeats": args.repeats,
        "load_wall_seconds": load_wall,
        "load_cpu_seconds": load_cpu,
        "rss_before_bytes": rss_before,
        "rss_loaded_bytes": rss_loaded,
        "idle_wall_seconds": idle_wall,
        "idle_cpu_seconds": idle_cpu,
        "idle_cpu_percent": idle_cpu / max(idle_wall, 1e-9) * 100,
        "active_wall_seconds": active_wall,
        "active_cpu_seconds": active_cpu,
        "active_cpu_percent": active_cpu / max(active_wall, 1e-9) * 100,
        "rss_final_bytes": _rss_bytes(),
        "runs": runs,
        "system_before": system_before,
        "system_after": system_after,
        "first_audio_seconds": None,
        "first_audio_note": "current adapter returns complete utterance PCM",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _rss_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _system_snapshot() -> dict[str, int | float | str | None]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", maxsplit=1)
            if key in {"MemAvailable", "SwapTotal", "SwapFree"}:
                values[key] = int(raw.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return {
        "available_memory_bytes": values.get("MemAvailable"),
        "swap_used_bytes": values.get("SwapTotal", 0) - values.get("SwapFree", 0),
        "temperature_c": _temperature_c(),
        "throttled": _throttled(),
    }


def _temperature_c() -> float | None:
    try:
        return (
            int(
                Path("/sys/class/thermal/thermal_zone0/temp").read_text(
                    encoding="ascii"
                )
            )
            / 1000
        )
    except (OSError, ValueError):
        return None


def _throttled() -> str | None:
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"throttled=(0x[0-9a-fA-F]+)", result.stdout)
    return match.group(1).lower() if match else None


if __name__ == "__main__":
    raise SystemExit(main())
