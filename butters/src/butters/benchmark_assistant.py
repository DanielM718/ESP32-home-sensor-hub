"""Fixed-corpus benchmark for deterministic routing and read-only integrations."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.config import default_vocabulary_path
from butters.stt.normalization import load_domain_vocabulary

DEFAULT_CORPUS = (
    "what is the CO2 level",
    "what is the printer room temperature",
    "what is the printer room humidity",
    "what's the pm two point five",
    "what is the VOC index",
    "what is the NOx index",
    "how humid is container 3",
    "which filament box has the highest humidity",
    "when was filament box two last seen",
    "what is the battery voltage of box one",
    "are all sensors reporting",
    "how is the printer room air quality",
    "what is the server status",
    "what is the humidity",
    "turn off the printer exhaust",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark-skills")
    parser.add_argument("--assistant-config", type=Path)
    parser.add_argument("--phrase", action="append", dest="phrases")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_assistant_settings(args.assistant_config)
    assistant = create_assistant(
        settings, load_domain_vocabulary(default_vocabulary_path())
    )
    corpus = tuple(args.phrases or DEFAULT_CORPUS)
    started = time.perf_counter()
    cpu_started = time.process_time()
    results = []
    for phrase in corpus:
        response = assistant.handle_text(phrase)
        results.append(
            {
                "request": phrase,
                "normalized": response.normalized_text,
                "route_status": response.route.status,
                "skill": response.route.skill,
                "arguments": response.route.arguments,
                "confidence": response.route.confidence,
                "response": response.response_text,
                "elapsed_ms": response.elapsed_seconds * 1000,
                "skill_ok": response.execution.ok if response.execution else None,
                "skill_failure": (
                    response.execution.failure.code
                    if response.execution and response.execution.failure
                    else None
                ),
            }
        )
    wall = time.perf_counter() - started
    cpu = time.process_time() - cpu_started
    print(
        json.dumps(
            {
                "count": len(results),
                "wall_seconds": wall,
                "cpu_seconds": cpu,
                "process_cpu_percent": cpu / max(wall, 1e-9) * 100,
                "rss_bytes": _rss_bytes(),
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _rss_bytes() -> int:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
