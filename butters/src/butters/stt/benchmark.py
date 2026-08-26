"""Repeatable on-device benchmark for the selected streaming recognizer."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import platform
import re
import resource
import subprocess
import time
from pathlib import Path

from butters.audio.analysis import EnergyVad
from butters.audio.buffer import PreRollBuffer
from butters.audio.frontend import AudioFrontend
from butters.audio.sources import WaveAudioSource
from butters.config import default_stt_model_dir, default_vocabulary_path
from butters.stt.normalization import load_domain_vocabulary
from butters.stt.session import StreamingTranscriber
from butters.stt.sherpa_engine import SherpaOnnxStreamingSTT


def _status_kib(name: str) -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(name + ":"):
                return int(line.split()[1])
    except (FileNotFoundError, OSError, ValueError):
        pass
    return 0


def _meminfo_kib() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, raw = line.split(":", 1)
        fields = raw.split()
        if fields:
            values[key] = int(fields[0])
    return values


def _system_memory() -> dict[str, float]:
    values = _meminfo_kib()
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "available_mib": values.get("MemAvailable", 0) / 1024,
        "swap_used_mib": (swap_total - swap_free) / 1024,
    }


def _swap_io_pages() -> dict[str, int]:
    wanted = {"pswpin", "pswpout"}
    result = {name: 0 for name in wanted}
    for line in Path("/proc/vmstat").read_text().splitlines():
        name, value = line.split()
        if name in wanted:
            result[name] = int(value)
    return result


def _temperature_c() -> float | None:
    candidates = sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    for path in candidates:
        try:
            value = float(path.read_text().strip())
        except (OSError, ValueError):
            continue
        return value / 1000 if value > 1000 else value
    return None


def _throttled() -> str:
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return result.stdout.strip() or result.stderr.strip() or "unavailable"


def _snapshot() -> dict[str, object]:
    return {
        "process_rss_mib": _status_kib("VmRSS") / 1024,
        "system": _system_memory(),
        "swap_io_pages": _swap_io_pages(),
        "temperature_c": _temperature_c(),
        "throttled": _throttled(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=5.0)
    parser.add_argument("--model-dir", type=Path, default=default_stt_model_dir())
    parser.add_argument(
        "--expected",
        action="append",
        default=[],
        metavar="WAV=TRANSCRIPT",
        help="optional expected command text used for per-clip word error reporting",
    )
    parser.add_argument("wav", nargs="+", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.threads < 1 or args.repeats < 1 or args.idle_seconds < 0:
        raise SystemExit("threads/repeats must be positive and idle-seconds nonnegative")
    expected = _expected_transcripts(args.expected)

    before_load = _snapshot()
    engine = SherpaOnnxStreamingSTT(args.model_dir, num_threads=args.threads)
    after_load = _snapshot()

    idle_cpu_start = time.process_time()
    idle_wall_start = time.perf_counter()
    time.sleep(args.idle_seconds)
    idle_wall = time.perf_counter() - idle_wall_start
    idle_cpu = time.process_time() - idle_cpu_start

    vocabulary = load_domain_vocabulary(default_vocabulary_path())
    recognition_rows: list[dict[str, object]] = []
    total_audio_seconds = 0.0
    finalization_latencies: list[float] = []
    speech_end_latencies: list[float] = []
    active_cpu_start = time.process_time()
    active_wall_start = time.perf_counter()
    for repeat in range(args.repeats):
        for wav_path in args.wav:
            recognition_started = time.perf_counter()
            recognition_cpu_started = time.process_time()
            source = WaveAudioSource(wav_path, frame_ms=20, realtime=False)
            frontend = AudioFrontend(
                source,
                vad=EnergyVad(
                    threshold_dbfs=-42.0,
                    attack_frames=2,
                    release_frames=30,
                ),
                pre_roll=PreRollBuffer(frame_ms=20, duration_ms=800),
            )
            with source:
                results = StreamingTranscriber(engine, vocabulary).run(frontend)
            recognition_wall = time.perf_counter() - recognition_started
            recognition_cpu = time.process_time() - recognition_cpu_started
            source_audio_seconds = source.stats.bytes_read / 32_000
            total_audio_seconds += source_audio_seconds
            for result in results:
                finalization_latencies.append(result.finalization_latency_seconds)
                speech_end_latencies.append(result.speech_end_to_final_seconds)
            row: dict[str, object] = {
                "repeat": repeat + 1,
                "wav": wav_path.name,
                "path": str(wav_path),
                "source_audio_seconds": round(source_audio_seconds, 6),
                "engine_warm": True,
                "total_wall_seconds": round(recognition_wall, 6),
                "total_cpu_seconds": round(recognition_cpu, 6),
                "audio_preprocessing_and_control_seconds": round(
                    max(
                        0.0,
                        recognition_wall
                        - sum(item.processing_seconds for item in results),
                    ),
                    6,
                ),
                "transcription_seconds": round(
                    sum(item.processing_seconds for item in results), 6
                ),
                "total_real_time_factor": round(
                    recognition_wall / max(source_audio_seconds, 1e-9), 6
                ),
                "transcription_real_time_factor": round(
                    sum(item.processing_seconds for item in results)
                    / max(source_audio_seconds, 1e-9),
                    6,
                ),
                "utterances": len(results),
                "raw": [result.raw for result in results],
                "normalized": [result.normalized for result in results],
                "partial_counts": [len(result.partials) for result in results],
                "endpoint_reasons": [result.endpoint_reason for result in results],
            }
            expected_text = expected.get(wav_path.name) or expected.get(str(wav_path))
            if expected_text is not None:
                recognized = " ".join(
                    item.raw.strip() for item in results if item.raw.strip()
                )
                row["expected"] = expected_text
                row["recognized_command"] = recognized
                row["word_error_rate"] = round(
                    _word_error_rate(expected_text, recognized), 6
                )
                row["exact_command_match"] = (
                    _words(expected_text) == _words(recognized)
                )
            recognition_rows.append(row)
    active_wall = time.perf_counter() - active_wall_start
    active_cpu = time.process_time() - active_cpu_start
    after_active = _snapshot()
    engine.close()

    output = {
        "runtime": {
            "python": os.sys.version.split()[0],
            "sherpa_onnx": importlib.metadata.version("sherpa-onnx"),
        },
        "hardware": _hardware(),
        "backend": {
            "name": "sherpa-onnx OnlineRecognizer",
            "execution_provider": "cpu",
            "accelerator": "none",
            "quantization": "int8",
            "model_format": "ONNX online transducer",
        },
        "model": args.model_dir.name,
        "model_files_bytes": engine.model_bytes,
        "threads": args.threads,
        "repeats": args.repeats,
        "initialization_seconds": engine.initialization_seconds,
        "before_load": before_load,
        "after_load": after_load,
        "resident_rss_increase_mib": (
            float(after_load["process_rss_mib"])
            - float(before_load["process_rss_mib"])
        ),
        "idle": {
            "wall_seconds": idle_wall,
            "cpu_seconds": idle_cpu,
            "process_cpu_percent": idle_cpu / max(idle_wall, 1e-9) * 100,
        },
        "active": {
            "wall_seconds": active_wall,
            "cpu_seconds": active_cpu,
            "process_cpu_percent": active_cpu / max(active_wall, 1e-9) * 100,
            "audio_seconds": total_audio_seconds,
            "real_time_factor": active_wall / max(total_audio_seconds, 1e-9),
            "mean_finalization_latency_ms": (
                1000 * sum(finalization_latencies) / len(finalization_latencies)
                if finalization_latencies
                else math.nan
            ),
            "max_finalization_latency_ms": (
                1000 * max(finalization_latencies)
                if finalization_latencies
                else math.nan
            ),
            "mean_speech_end_to_final_ms": (
                1000 * sum(speech_end_latencies) / len(speech_end_latencies)
                if speech_end_latencies
                else math.nan
            ),
            "max_speech_end_to_final_ms": (
                1000 * max(speech_end_latencies)
                if speech_end_latencies
                else math.nan
            ),
        },
        "after_active": after_active,
        "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        / 1024,
        "recognitions": recognition_rows,
        "accuracy": {
            "evaluated_rows": sum("word_error_rate" in row for row in recognition_rows),
            "exact_rows": sum(
                row.get("exact_command_match") is True for row in recognition_rows
            ),
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def _expected_transcripts(values: list[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for value in values:
        name, separator, transcript = value.partition("=")
        if not separator or not name.strip() or not transcript.strip():
            raise SystemExit("--expected must use WAV=TRANSCRIPT")
        expected[name.strip()] = transcript.strip()
    return expected


def _words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", text.casefold()))


def _word_error_rate(expected: str, actual: str) -> float:
    reference = _words(expected)
    hypothesis = _words(actual)
    previous = list(range(len(hypothesis) + 1))
    for row, reference_word in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_word in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (reference_word != hypothesis_word),
                )
            )
        previous = current
    return previous[-1] / max(len(reference), 1)


def _hardware() -> dict[str, object]:
    model = "unknown"
    try:
        model = Path("/proc/device-tree/model").read_bytes().rstrip(b"\0").decode()
    except (OSError, UnicodeDecodeError):
        pass
    flags = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.casefold().startswith(("features", "flags")):
                flags = line.split(":", 1)[1].strip()
                break
    except (OSError, IndexError):
        pass
    return {
        "model": model,
        "architecture": platform.machine(),
        "cpu_count": os.cpu_count(),
        "cpu_features": flags.split(),
        "dri_available": Path("/dev/dri").is_dir(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
