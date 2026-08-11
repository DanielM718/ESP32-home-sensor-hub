"""Command-line audio and local streaming-STT diagnostics for Butters."""

from __future__ import annotations

import argparse
import math
import sys
import time
import wave
from dataclasses import replace
from pathlib import Path

from butters.audio.analysis import EnergyVad
from butters.audio.buffer import PreRollBuffer
from butters.audio.chime import AlsaChimePlayer, ChimeError, NullChimePlayer
from butters.audio.discovery import (
    list_capture_devices,
    probe_16k_mono,
    usb_stream_descriptors,
    warmup_uvc_device,
)
from butters.audio.frontend import AudioFrontend, FrontendFrame
from butters.audio.model import AudioSource, AudioSourceError
from butters.audio.operations import record_standard_wav
from butters.audio.sources import AlsaAudioSource, WaveAudioSource
from butters.config import (
    AudioSettings,
    ConfigError,
    load_live_settings,
    load_settings,
    load_stt_settings,
    load_wakeword_settings,
    with_overrides,
    with_stt_overrides,
    with_wakeword_overrides,
)
from butters.stt.model import STTEngineError
from butters.stt.normalization import load_domain_vocabulary
from butters.stt.session import TranscriptionEvent
from butters.wakeword.model import WakeWordError


def _common_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="TOML configuration path")
    parser.add_argument(
        "--source", choices=("alsa", "wave"), help="override source type"
    )
    parser.add_argument(
        "--input",
        help="ALSA PCM identifier or WAV path, according to --source",
    )
    parser.add_argument(
        "--realtime",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="pace WAV input in real time",
    )
    parser.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="loop WAV input",
    )


def _wakeword_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wake-model-dir", type=Path, help="override wake-word model directory"
    )
    parser.add_argument(
        "--wake-threshold", type=float, help="override keyword trigger threshold"
    )
    parser.add_argument("--wake-score", type=float, help="override keyword boost")
    parser.add_argument(
        "--wake-threads", type=int, help="override wake-word inference threads"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="butters-audio",
        description="Butters standardized audio frontend diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="list ALSA capture devices")
    discover.add_argument(
        "--probe",
        action="store_true",
        help="open each device briefly to test native and ALSA-converted 16 kHz mono",
    )
    discover.add_argument("--config", type=Path, help="TOML configuration path")
    discover.add_argument(
        "--video-warmup-device",
        help="override an optional UVC node used before every format probe",
    )

    diagnose = subparsers.add_parser("diagnose", help="show live/file audio levels")
    _common_source_arguments(diagnose)
    diagnose.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="stop after this many audio seconds; zero runs until EOF/Ctrl-C",
    )
    diagnose.add_argument("--report-every", type=float, default=0.5)

    record = subparsers.add_parser("record", help="write a short standardized WAV")
    _common_source_arguments(record)
    record.add_argument("--seconds", type=float, default=5.0)
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--overwrite", action="store_true")

    hardware = subparsers.add_parser(
        "hardware-check",
        help="diagnose an ALSA microphone while saving a short validation WAV",
    )
    hardware.add_argument("--config", type=Path, help="TOML configuration path")
    hardware.add_argument("--input", help="override configured ALSA PCM identifier")
    hardware.add_argument("--seconds", type=float, default=8.0)
    hardware.add_argument("--output", type=Path, required=True)
    hardware.add_argument("--overwrite", action="store_true")
    hardware.add_argument("--report-every", type=float, default=0.5)

    stt = subparsers.add_parser(
        "stt-test",
        help="stream source audio through VAD and local speech recognition",
    )
    _common_source_arguments(stt)
    # A file behaves like a microphone unless the caller explicitly asks for
    # accelerated processing with --no-realtime.
    stt.set_defaults(realtime=True)
    stt.add_argument("--model-dir", type=Path, help="override STT model directory")
    stt.add_argument("--threads", type=int, help="override recognizer CPU threads")
    stt.add_argument(
        "--max-audio-seconds",
        type=float,
        default=0.0,
        help="stop after this much source audio; zero runs until EOF/Ctrl-C",
    )

    wake = subparsers.add_parser(
        "wake-test", help="stream source audio through the local wake-word detector"
    )
    _common_source_arguments(wake)
    wake.set_defaults(realtime=True)
    _wakeword_arguments(wake)
    wake.add_argument("--max-detections", type=int, default=0)
    wake.add_argument("--max-audio-seconds", type=float, default=0.0)

    live = subparsers.add_parser(
        "live", help="run wake, acknowledgement, VAD, and streaming STT"
    )
    _common_source_arguments(live)
    live.set_defaults(realtime=True)
    _wakeword_arguments(live)
    live.add_argument("--model-dir", type=Path, help="override STT model directory")
    live.add_argument("--threads", type=int, help="override STT CPU threads")
    live.add_argument("--cycles", type=int, default=0, help="stop after N sessions")
    live.add_argument("--max-audio-seconds", type=float, default=0.0)
    live.add_argument(
        "--chime",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable the acknowledgement chime",
    )
    live.add_argument("--playback-device", help="override ALSA playback PCM")
    live.add_argument("--no-speech-timeout", type=float)
    return parser


def _settings_from_args(
    args: argparse.Namespace, *, force_alsa: bool = False
) -> AudioSettings:
    settings = load_settings(args.config)
    source = "alsa" if force_alsa else args.source
    return with_overrides(
        settings,
        source=source,
        input_value=args.input,
        realtime=getattr(args, "realtime", None),
        loop=getattr(args, "loop", None),
    )


def _source(settings: AudioSettings) -> AudioSource:
    if settings.source == "alsa":
        if not settings.alsa_device or "CHANGE_ME" in settings.alsa_device:
            raise ConfigError(
                "no ALSA input selected; set [alsa].device or pass --input"
            )
        return AlsaAudioSource(
            settings.alsa_device,
            frame_ms=settings.frame_ms,
            video_warmup_device=settings.alsa_video_warmup_device or None,
        )
    if settings.wave_path is None:
        raise ConfigError("no WAV input selected; set [wave].path or pass --input")
    return WaveAudioSource(
        settings.wave_path,
        frame_ms=settings.frame_ms,
        realtime=settings.wave_realtime,
        loop=settings.wave_loop,
    )


def _frontend(source: AudioSource, settings: AudioSettings) -> AudioFrontend:
    attack_frames = max(1, math.ceil(settings.vad_attack_ms / settings.frame_ms))
    release_frames = max(1, math.ceil(settings.vad_release_ms / settings.frame_ms))
    threshold = settings.vad_threshold_dbfs if settings.vad_enabled else 0.0
    return AudioFrontend(
        source,
        vad=EnergyVad(
            threshold_dbfs=threshold,
            attack_frames=attack_frames,
            release_frames=release_frames,
        ),
        pre_roll=PreRollBuffer(
            frame_ms=settings.frame_ms,
            duration_ms=settings.preroll_ms,
        ),
        clip_threshold=settings.clip_threshold,
    )


class Reporter:
    def __init__(self, report_every: float) -> None:
        self.report_every = max(0.01, report_every)
        self.next_report = 0.0
        self.audio_seconds = 0.0
        self.frames = 0

    def observe(
        self, item: FrontendFrame, source: AudioSource, pre_roll: PreRollBuffer
    ) -> None:
        self.frames += 1
        self.audio_seconds += item.audio.duration_seconds
        if self.audio_seconds + 1e-9 < self.next_report:
            return
        self.next_report = self.audio_seconds + self.report_every
        analysis = item.analysis
        dbfs = "-inf" if not math.isfinite(analysis.dbfs) else f"{analysis.dbfs:6.1f}"
        print(
            f"audio=yes time={self.audio_seconds:6.2f}s frames={self.frames:6d} "
            f"rms={analysis.rms:7.1f} level={dbfs} dBFS "
            f"speech={'yes' if analysis.speech_active else 'no '} "
            f"clip={'YES' if analysis.clipping else 'no '} "
            f"overruns={source.stats.overruns} dropped={source.stats.dropped_frames} "
            f"pre_roll={len(pre_roll)}/{pre_roll.max_frames}"
        )


def command_discover(args: argparse.Namespace) -> int:
    devices, raw_output = list_capture_devices()
    configured_warmup = load_settings(args.config).alsa_video_warmup_device
    video_warmup = args.video_warmup_device or configured_warmup
    print("ALSA capture-device report")
    print(raw_output or "(arecord returned no output)")
    if not devices:
        print("\nNo ALSA hardware capture devices found.")
        return 0
    for device in devices:
        print(
            f"\ncard {device.card}, device {device.device}: "
            f"{device.card_name} / {device.device_name}"
        )
        print(f"  native identifier:    {device.hw_id}")
        print(f"  converting identifier: {device.plughw_id}")
        for path, content in usb_stream_descriptors(device):
            print(f"  native USB descriptor ({path}):")
            for line in content.rstrip().splitlines():
                print(f"    {line}")
        if args.probe:
            native_warmup = None
            if video_warmup:
                native_warmup = warmup_uvc_device(video_warmup)
                print(
                    "  UVC warm-up before direct: "
                    + (
                        "yes"
                        if native_warmup.success
                        else f"no ({native_warmup.detail})"
                    )
                )
            native = probe_16k_mono(device.hw_id)
            converted_warmup = None
            if video_warmup:
                converted_warmup = warmup_uvc_device(video_warmup)
                print(
                    "  UVC warm-up before plughw: "
                    + (
                        "yes"
                        if converted_warmup.success
                        else f"no ({converted_warmup.detail})"
                    )
                )
            converted = probe_16k_mono(device.plughw_id)
            print(
                "  direct 16 kHz mono S16_LE: "
                + ("yes" if native.success else f"no ({native.detail})")
            )
            print(
                "  via ALSA conversion:       "
                + ("yes" if converted.success else f"no ({converted.detail})")
            )
    return 0


def command_diagnose(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    source = _source(settings)
    frontend = _frontend(source, settings)
    reporter = Reporter(args.report_every)
    limit = args.seconds
    print(
        f"source={settings.source} internal=16000 Hz mono S16_LE "
        f"frame={settings.frame_ms} ms; Ctrl-C stops cleanly"
    )
    with source:
        while limit <= 0 or reporter.audio_seconds < limit:
            item = frontend.read()
            if item is None:
                break
            reporter.observe(item, source, frontend.pre_roll)
    print(
        f"complete: {reporter.frames} frames, {reporter.audio_seconds:.3f}s audio, "
        f"overruns={source.stats.overruns}, dropped={source.stats.dropped_frames}"
    )
    return 0


def _inspect_recording(path: Path) -> str:
    with wave.open(str(path), "rb") as recorded:
        return (
            f"channels={recorded.getnchannels()} rate={recorded.getframerate()} Hz "
            f"width={recorded.getsampwidth() * 8}-bit frames={recorded.getnframes()}"
        )


def command_record(args: argparse.Namespace, *, hardware_check: bool = False) -> int:
    settings = _settings_from_args(args, force_alsa=hardware_check)
    source = _source(settings)
    frontend = _frontend(source, settings)
    reporter = Reporter(getattr(args, "report_every", 0.5))

    def report(item: FrontendFrame) -> None:
        reporter.observe(item, source, frontend.pre_roll)

    result = record_standard_wav(
        source,
        args.output,
        duration_seconds=args.seconds,
        overwrite=args.overwrite,
        frontend=frontend,
        on_frame=report if hardware_check else None,
    )
    print(
        f"wrote {result.path}: {result.duration_seconds:.3f}s, "
        f"{_inspect_recording(result.path)}"
    )
    print(
        f"source stats: frames={source.stats.frames_read} "
        f"overruns={source.stats.overruns} dropped={source.stats.dropped_frames}"
    )
    return 0


def _mib(byte_count: int) -> float:
    return byte_count / (1024 * 1024)


def _rss_mib() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (FileNotFoundError, OSError, ValueError):
        pass
    return float("nan")


def _system_status() -> tuple[float, float, float]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, raw = line.split(":", 1)
            if name in {"MemAvailable", "SwapTotal", "SwapFree"}:
                values[name] = int(raw.split()[0])
    except (FileNotFoundError, OSError, ValueError):
        pass
    available = values.get("MemAvailable", 0) / 1024
    swap_used = (values.get("SwapTotal", 0) - values.get("SwapFree", 0)) / 1024
    temperature = float("nan")
    try:
        temperature = (
            int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
            / 1000
        )
    except (FileNotFoundError, OSError, ValueError):
        pass
    return available, swap_used, temperature


def _status_text() -> str:
    available, swap_used, temperature = _system_status()
    return (
        f"available={available:.1f} MiB swap_used={swap_used:.1f} MiB "
        f"temp={temperature:.1f}C"
    )


def command_stt_test(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.vad_enabled:
        raise ConfigError("stt-test requires VAD to be enabled")
    stt_settings = with_stt_overrides(
        load_stt_settings(args.config),
        model_dir=args.model_dir,
        num_threads=args.threads,
    )
    settings = replace(settings, vad_release_ms=stt_settings.endpoint_silence_ms)
    source = _source(settings)
    frontend = _frontend(source, settings)
    vocabulary = load_domain_vocabulary(stt_settings.vocabulary_path)

    # Keep the optional native runtime out of all audio-only commands.
    from butters.stt.sherpa_engine import SherpaOnnxStreamingSTT

    engine = SherpaOnnxStreamingSTT(
        stt_settings.model_dir,
        num_threads=stt_settings.num_threads,
        decoding_method=stt_settings.decoding_method,
        endpoint_silence_seconds=stt_settings.endpoint_silence_ms / 1000,
        max_utterance_seconds=stt_settings.max_utterance_seconds,
    )
    print(
        f"model={stt_settings.model_dir.name} threads={stt_settings.num_threads} "
        f"decode={stt_settings.decoding_method} "
        f"model_files={_mib(engine.model_bytes):.1f} MiB "
        f"init={engine.initialization_seconds:.3f}s rss={_rss_mib():.1f} MiB"
    )
    print(
        f"source={settings.source} internal=16000 Hz mono S16_LE "
        f"frame={settings.frame_ms} ms "
        f"mode={'real-time' if settings.source == 'alsa' or settings.wave_realtime else 'accelerated'} "
        f"endpoint_silence={stt_settings.endpoint_silence_ms} ms"
    )

    def report(event: TranscriptionEvent) -> None:
        if event.kind == "speech_start":
            print(f"VAD: speech_start at {event.audio_position_seconds:.2f}s")
        elif event.kind == "partial":
            print(f"PARTIAL: {event.text}")
        elif event.result is not None:
            result = event.result
            print(f"FINAL RAW: {result.raw}")
            print(f"FINAL NORMALIZED: {result.normalized}")
            print(
                f"FINAL STATS: audio={result.audio_seconds:.3f}s "
                f"recognizer_cpu_wall={result.processing_seconds:.3f}s "
                f"rtf={result.processing_seconds / max(result.audio_seconds, 1e-9):.3f} "
                f"cpu_per_audio="
                f"{result.processing_cpu_seconds / max(result.audio_seconds, 1e-9) * 100:.1f}% "
                f"finalize_latency={result.finalization_latency_seconds * 1000:.1f}ms "
                f"speech_end_to_final={result.speech_end_to_final_seconds * 1000:.1f}ms "
                f"endpoint={result.endpoint_reason}"
            )

    from butters.stt.session import StreamingTranscriber

    transcriber = StreamingTranscriber(engine, vocabulary)
    try:
        with engine, source:
            results = transcriber.run(
                frontend,
                on_event=report,
                max_audio_seconds=args.max_audio_seconds,
            )
    finally:
        engine.close()
    print(
        f"complete: utterances={len(results)} source_frames={source.stats.frames_read} "
        f"overruns={source.stats.overruns} dropped={source.stats.dropped_frames} "
        f"rss={_rss_mib():.1f} MiB"
    )
    return 0


def _wake_detector(args: argparse.Namespace):
    wake_settings = with_wakeword_overrides(
        load_wakeword_settings(args.config),
        model_dir=args.wake_model_dir,
        threshold=args.wake_threshold,
        score=args.wake_score,
        num_threads=args.wake_threads,
    )
    from butters.wakeword.sherpa_detector import SherpaOnnxWakeWordDetector

    detector = SherpaOnnxWakeWordDetector(
        wake_settings.model_dir,
        wake_settings.keywords_path,
        num_threads=wake_settings.num_threads,
        chunk_size=wake_settings.chunk_size,
        score=wake_settings.score,
        threshold=wake_settings.threshold,
    )
    return wake_settings, detector


def command_wake_test(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    source = _source(settings)
    wake_settings, detector = _wake_detector(args)
    print(
        f"wake_model={wake_settings.model_dir.name} phrase={wake_settings.phrase!r} "
        f"threshold={wake_settings.threshold:.3f} score={wake_settings.score:.2f} "
        f"chunk={wake_settings.chunk_size} threads={wake_settings.num_threads} "
        f"model_files={_mib(detector.model_bytes):.1f} MiB "
        f"init={detector.initialization_seconds:.3f}s rss={_rss_mib():.1f} MiB "
        f"{_status_text()}"
    )
    print(
        f"source={settings.source} input="
        f"{settings.alsa_device if settings.source == 'alsa' else settings.wave_path} "
        "internal=16000 Hz mono S16_LE; Ctrl-C stops cleanly"
    )
    detections = 0
    audio_seconds = 0.0
    processing_started = time.perf_counter()
    cpu_started = time.process_time()
    try:
        with detector, source:
            while (
                (args.max_detections <= 0 or detections < args.max_detections)
                and (
                    args.max_audio_seconds <= 0
                    or audio_seconds < args.max_audio_seconds
                )
            ):
                frame = source.read_frame()
                if frame is None:
                    break
                audio_seconds += frame.duration_seconds
                detection = detector.accept_audio(frame)
                if detection is None:
                    continue
                detections += 1
                confidence = (
                    "n/a"
                    if detection.confidence is None
                    else f"{detection.confidence:.3f}"
                )
                latency = (
                    "n/a"
                    if detection.model_latency_seconds is None
                    else f"{detection.model_latency_seconds * 1000:.0f}ms"
                )
                print(
                    f"[WAKE] keyword={detection.keyword!r} confidence={confidence} "
                    f"threshold={detection.threshold:.3f} "
                    f"model_delay={latency} audio_time={audio_seconds:.2f}s"
                )
                detector.reset()
    finally:
        detector.close()
    processing_wall = time.perf_counter() - processing_started
    processing_cpu = time.process_time() - cpu_started
    print(
        f"complete: detections={detections} audio={audio_seconds:.2f}s "
        f"frames={source.stats.frames_read} overruns={source.stats.overruns} "
        f"dropped={source.stats.dropped_frames} rss={_rss_mib():.1f} MiB "
        f"wall={processing_wall:.2f}s cpu={processing_cpu:.2f}s "
        f"average_cpu={processing_cpu / max(processing_wall, 1e-9) * 100:.1f}% "
        f"{_status_text()}"
    )
    return 0


def command_live(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.vad_enabled:
        raise ConfigError("live mode requires VAD to be enabled")
    stt_settings = with_stt_overrides(
        load_stt_settings(args.config),
        model_dir=args.model_dir,
        num_threads=args.threads,
    )
    live_settings = load_live_settings(args.config)
    changes = {}
    if args.chime is not None:
        changes["acknowledge"] = args.chime
    if args.playback_device is not None:
        changes["playback_device"] = args.playback_device
    if args.no_speech_timeout is not None:
        changes["no_speech_timeout_seconds"] = args.no_speech_timeout
    if changes:
        live_settings = replace(live_settings, **changes).validated()

    source = _source(settings)
    vocabulary = load_domain_vocabulary(stt_settings.vocabulary_path)
    from butters.stt.sherpa_engine import SherpaOnnxStreamingSTT

    engine = SherpaOnnxStreamingSTT(
        stt_settings.model_dir,
        num_threads=stt_settings.num_threads,
        decoding_method=stt_settings.decoding_method,
        endpoint_silence_seconds=stt_settings.endpoint_silence_ms / 1000,
        max_utterance_seconds=stt_settings.max_utterance_seconds,
    )
    try:
        wake_settings, detector = _wake_detector(args)
    except Exception:
        engine.close()
        raise

    chime = (
        AlsaChimePlayer(
            live_settings.playback_device,
            volume=live_settings.chime_volume,
        )
        if live_settings.acknowledge
        else NullChimePlayer()
    )
    attack_frames = max(1, math.ceil(settings.vad_attack_ms / settings.frame_ms))
    release_frames = max(
        1, math.ceil(stt_settings.endpoint_silence_ms / settings.frame_ms)
    )
    from butters.live.controller import LiveEvent, LiveVoiceController, run_live_source

    controller = LiveVoiceController(
        wake_detector=detector,
        stt_engine=engine,
        vocabulary=vocabulary,
        command_vad=EnergyVad(
            threshold_dbfs=settings.vad_threshold_dbfs,
            attack_frames=attack_frames,
            release_frames=release_frames,
        ),
        wake_preroll=PreRollBuffer(
            frame_ms=settings.frame_ms, duration_ms=settings.preroll_ms
        ),
        command_preroll=PreRollBuffer(
            frame_ms=settings.frame_ms,
            duration_ms=live_settings.command_preroll_ms,
        ),
        chime=chime,
        no_speech_timeout_seconds=live_settings.no_speech_timeout_seconds,
        max_command_seconds=live_settings.max_command_seconds,
        acknowledgement_guard_seconds=(
            live_settings.acknowledgement_guard_ms / 1000
            if live_settings.acknowledge
            else 0.0
        ),
        clip_threshold=settings.clip_threshold,
    )
    input_name = settings.alsa_device if settings.source == "alsa" else settings.wave_path
    print(
        f"live source={settings.source} input={input_name} "
        "internal=16000 Hz mono S16_LE single_capture_owner=yes"
    )
    print(
        f"wake_model={wake_settings.model_dir.name} phrase={wake_settings.phrase!r} "
        f"threshold={wake_settings.threshold:.3f} chunk={wake_settings.chunk_size} "
        f"init={detector.initialization_seconds:.3f}s "
        f"stt_model={stt_settings.model_dir.name} stt_threads={stt_settings.num_threads} "
        f"init={engine.initialization_seconds:.3f}s rss={_rss_mib():.1f} MiB "
        f"{_status_text()}"
    )
    print(
        f"no_speech_timeout={live_settings.no_speech_timeout_seconds:.1f}s "
        f"endpoint_silence={stt_settings.endpoint_silence_ms}ms "
        f"chime={'on' if live_settings.acknowledge else 'off'}"
    )

    def report(event: LiveEvent) -> None:
        if event.kind == "ready":
            print("[READY] Waiting for wake word", flush=True)
        elif event.kind == "wake" and event.detection is not None:
            detection = event.detection
            confidence = (
                "n/a" if detection.confidence is None else f"{detection.confidence:.3f}"
            )
            delay = (
                "n/a"
                if detection.model_latency_seconds is None
                else f"{detection.model_latency_seconds * 1000:.0f}ms"
            )
            print(
                f"[WAKE] phrase={detection.keyword!r} confidence={confidence} "
                f"threshold={detection.threshold:.3f} model_delay={delay}",
                flush=True,
            )
        elif event.kind == "listening":
            ack = event.acknowledgement_launch_seconds
            suffix = "" if ack is None else f" ack_launch={ack * 1000:.1f}ms"
            print(f"[LISTENING]{suffix}", flush=True)
        elif event.kind == "speech_start":
            print("[VAD] speech_start", flush=True)
        elif event.kind == "partial":
            print(f"[PARTIAL] {event.text}", flush=True)
        elif event.kind == "final" and event.result is not None:
            result = event.result
            print(f"[FINAL RAW] {result.raw}", flush=True)
            print(f"[FINAL NORMALIZED] {result.normalized}", flush=True)
            print(
                f"[FINAL STATS] audio={result.audio_seconds:.3f}s "
                f"rtf={result.processing_seconds / max(result.audio_seconds, 1e-9):.3f} "
                f"stt_cpu_per_audio="
                f"{result.processing_cpu_seconds / max(result.audio_seconds, 1e-9) * 100:.1f}% "
                f"speech_end_to_final="
                f"{result.speech_end_to_final_seconds * 1000:.1f}ms "
                f"endpoint={result.endpoint_reason}",
                flush=True,
            )
        elif event.kind == "timeout":
            print("[TIMEOUT] No speech; returning to wake listening", flush=True)
        elif event.kind == "error":
            print(f"[ERROR] {event.text}", file=sys.stderr, flush=True)

    live_started = time.perf_counter()
    live_cpu_started = time.process_time()
    try:
        cycles = run_live_source(
            source,
            controller,
            on_event=report,
            max_cycles=args.cycles,
            max_audio_seconds=args.max_audio_seconds,
            retry_seconds=live_settings.audio_retry_seconds,
            max_audio_retries=live_settings.max_audio_retries,
        )
    finally:
        controller.close()
    live_wall = time.perf_counter() - live_started
    live_cpu = time.process_time() - live_cpu_started
    print(
        f"complete: cycles={cycles} frames={source.stats.frames_read} "
        f"overruns={source.stats.overruns} dropped={source.stats.dropped_frames} "
        f"wake_pre_roll={len(controller.wake_preroll)}/"
        f"{controller.wake_preroll.max_frames} rss={_rss_mib():.1f} MiB "
        f"wall={live_wall:.2f}s cpu={live_cpu:.2f}s "
        f"average_cpu={live_cpu / max(live_wall, 1e-9) * 100:.1f}% "
        f"{_status_text()}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            return command_discover(args)
        if args.command == "diagnose":
            return command_diagnose(args)
        if args.command == "record":
            return command_record(args)
        if args.command == "hardware-check":
            return command_record(args, hardware_check=True)
        if args.command == "stt-test":
            return command_stt_test(args)
        if args.command == "wake-test":
            return command_wake_test(args)
        if args.command == "live":
            return command_live(args)
    except KeyboardInterrupt:
        print("\nInterrupted; audio source closed.", file=sys.stderr)
        return 130
    except (
        AudioSourceError,
        ChimeError,
        ConfigError,
        STTEngineError,
        WakeWordError,
        FileExistsError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2
