"""Text/WAV query and explicit local TTS diagnostics."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import wave
from dataclasses import asdict, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from butters.assistant import AssistantResponse, create_assistant
from butters.assistant_config import load_assistant_settings
from butters.audio.analysis import EnergyVad
from butters.audio.buffer import PreRollBuffer
from butters.audio.frontend import AudioFrontend
from butters.audio.sources import WaveAudioSource
from butters.config import (
    ConfigError,
    load_settings,
    load_stt_settings,
    with_stt_overrides,
)
from butters.stt.model import STTEngineError
from butters.stt.normalization import load_domain_vocabulary
from butters.stt.session import StreamingTranscriber, UtteranceResult
from butters.tts.model import TTSError
from butters.tts.output import WaveFileOutput


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="butters-assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    query = subparsers.add_parser(
        "query", help="route text or a WAV transcript through read-only skills"
    )
    inputs = query.add_mutually_exclusive_group(required=True)
    inputs.add_argument("text", nargs="?", help="direct text request")
    inputs.add_argument("--wav", type=Path, help="stream and transcribe this WAV")
    query.add_argument("--assistant-config", type=Path)
    query.add_argument("--audio-config", type=Path)
    query.add_argument(
        "--realtime",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="pace WAV chunks like a live microphone",
    )
    query.add_argument("--model-dir", type=Path, help="override STT model directory")
    query.add_argument("--threads", type=int, help="override STT threads")
    query.add_argument("--json", action="store_true", help="print structured result")
    query.add_argument("--tts-output", type=Path, help="synthesize response to WAV")
    query.add_argument("--overwrite", action="store_true")

    speak = subparsers.add_parser("speak", help="synthesize text to a local WAV")
    speak.add_argument("text")
    speak.add_argument("--output", type=Path, required=True)
    speak.add_argument("--assistant-config", type=Path)
    speak.add_argument("--model-dir", type=Path)
    speak.add_argument("--threads", type=int)
    speak.add_argument("--speed", type=float)
    speak.add_argument("--overwrite", action="store_true")
    return parser


def command_query(args: argparse.Namespace) -> int:
    settings = load_assistant_settings(args.assistant_config)
    stt_settings = with_stt_overrides(
        load_stt_settings(args.audio_config),
        model_dir=args.model_dir,
        num_threads=args.threads,
    )
    vocabulary = load_domain_vocabulary(stt_settings.vocabulary_path)
    assistant = create_assistant(settings, vocabulary)
    utterances: list[UtteranceResult] = []
    pipeline_started = time.perf_counter()
    if args.wav is not None:
        utterances = _transcribe_wav(
            args.wav,
            audio_config=args.audio_config,
            realtime=args.realtime,
            model_dir=args.model_dir,
            threads=args.threads,
        )
        requests = [result.normalized for result in utterances if result.normalized]
        if not requests:
            print("error: WAV produced no non-empty transcript", file=sys.stderr)
            return 2
    else:
        requests = [args.text]

    responses = [assistant.handle_text(request) for request in requests]
    for response in responses:
        _print_response(response, structured=args.json)
    elapsed = time.perf_counter() - pipeline_started
    print(f"PIPELINE LATENCY: {elapsed * 1000:.1f} ms", flush=True)

    if args.tts_output is not None:
        if len(responses) != 1:
            raise ConfigError("--tts-output requires exactly one resolved utterance")
        _synthesize(
            responses[0].response_text,
            args.tts_output,
            settings=settings,
            overwrite=args.overwrite,
        )
    return 0


def command_speak(args: argparse.Namespace) -> int:
    settings = load_assistant_settings(args.assistant_config)
    tts = settings.tts
    changes: dict[str, object] = {}
    if args.model_dir is not None:
        changes["model_dir"] = args.model_dir.expanduser()
    if args.threads is not None:
        changes["num_threads"] = args.threads
    if args.speed is not None:
        changes["speed"] = args.speed
    if changes:
        tts = replace(tts, **changes).validated()
        settings = replace(settings, tts=tts)
    _synthesize(args.text, args.output, settings=settings, overwrite=args.overwrite)
    return 0


def _transcribe_wav(
    path: Path,
    *,
    audio_config: Path | None,
    realtime: bool,
    model_dir: Path | None,
    threads: int | None,
) -> list[UtteranceResult]:
    audio_settings = load_settings(audio_config)
    stt_settings = with_stt_overrides(
        load_stt_settings(audio_config), model_dir=model_dir, num_threads=threads
    )
    if not audio_settings.vad_enabled:
        raise ConfigError("WAV assistant mode requires VAD")
    source = WaveAudioSource(
        path,
        frame_ms=audio_settings.frame_ms,
        realtime=realtime,
    )
    attack_frames = max(
        1, math.ceil(audio_settings.vad_attack_ms / audio_settings.frame_ms)
    )
    release_frames = max(
        1, math.ceil(stt_settings.endpoint_silence_ms / audio_settings.frame_ms)
    )
    frontend = AudioFrontend(
        source,
        vad=EnergyVad(
            threshold_dbfs=audio_settings.vad_threshold_dbfs,
            attack_frames=attack_frames,
            release_frames=release_frames,
        ),
        pre_roll=PreRollBuffer(
            frame_ms=audio_settings.frame_ms,
            duration_ms=audio_settings.preroll_ms,
        ),
        clip_threshold=audio_settings.clip_threshold,
    )
    vocabulary = load_domain_vocabulary(stt_settings.vocabulary_path)
    from butters.stt.sherpa_engine import SherpaOnnxStreamingSTT

    engine = SherpaOnnxStreamingSTT(
        stt_settings.model_dir,
        num_threads=stt_settings.num_threads,
        decoding_method=stt_settings.decoding_method,
        endpoint_silence_seconds=stt_settings.endpoint_silence_ms / 1000,
        max_utterance_seconds=stt_settings.max_utterance_seconds,
    )

    def report(event: Any) -> None:
        if event.kind == "partial":
            print(f"PARTIAL: {event.text}", flush=True)
        elif event.kind == "final" and event.result is not None:
            print(f"FINAL RAW: {event.result.raw}", flush=True)
            print(f"FINAL NORMALIZED: {event.result.normalized}", flush=True)

    transcriber = StreamingTranscriber(engine, vocabulary)
    try:
        with engine, source:
            return transcriber.run(frontend, on_event=report)
    finally:
        engine.close()


def _print_response(response: AssistantResponse, *, structured: bool) -> None:
    print(f"REQUEST: {response.raw_text}")
    print(f"NORMALIZED: {response.normalized_text}")
    if response.route.matched:
        print(
            f"ROUTE: {response.route.skill} confidence={response.route.confidence:.2f} "
            f"arguments={response.route.arguments}"
        )
    else:
        print(f"ROUTE: {response.route.status}")
    print(f"RESPONSE: {response.response_text}")
    print(f"QUERY LATENCY: {response.elapsed_seconds * 1000:.1f} ms")
    if structured:
        print("STRUCTURED:")
        print(json.dumps(response, default=_json_default, indent=2, sort_keys=True))


def _synthesize(
    text: str,
    output: Path,
    *,
    settings: Any,
    overwrite: bool,
) -> None:
    from butters.tts.sherpa_engine import SherpaOnnxPiperTTS

    tts = settings.tts
    engine = SherpaOnnxPiperTTS(
        tts.model_dir,
        num_threads=tts.num_threads,
        speed=tts.speed,
        max_text_chars=tts.max_text_chars,
    )
    try:
        speech = engine.synthesize(text)
        WaveFileOutput().write(speech, output, overwrite=overwrite)
    finally:
        engine.close()
    with wave.open(str(output), "rb") as generated:
        metadata = (
            f"channels={generated.getnchannels()} rate={generated.getframerate()}Hz "
            f"width={generated.getsampwidth() * 8}-bit "
            f"frames={generated.getnframes()}"
        )
    rtf = speech.generation_seconds / max(speech.audio_seconds, 1e-9)
    print(
        f"TTS: model={tts.model_dir.name} init={engine.initialization_seconds:.3f}s "
        f"model_files={engine.model_bytes / (1024**2):.1f}MiB "
        f"generation={speech.generation_seconds:.3f}s "
        f"audio={speech.audio_seconds:.3f}s rtf={rtf:.3f} rss={_rss_mib():.1f}MiB"
    )
    print(f"WROTE: {output} {metadata}")


def _json_default(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _rss_mib() -> float:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return float("nan")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "query":
            return command_query(args)
        if args.command == "speak":
            return command_speak(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (
        ConfigError,
        FileExistsError,
        FileNotFoundError,
        STTEngineError,
        TTSError,
        ValueError,
        wave.Error,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
