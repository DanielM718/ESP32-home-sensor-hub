# Butters

Butters is the local voice-assistant subsystem for this home network. It lives
inside the sensor repository because later restricted skills will query sensors
and integrate with MQTT, InfluxDB, Home Assistant, monitoring sessions, and
Wake-on-LAN. It is not part of the critical monitoring data path.

## Current status: Milestone 3, live voice frontend

The implementation now provides:

- one `AudioSource` contract for live ALSA and real-time/accelerated WAV input;
- standardized 16 kHz, mono, signed 16-bit little-endian PCM in 20 ms chunks;
- streaming WAV downmix/resampling without a large numerical dependency;
- RMS, dBFS, peak/clipping, overrun/drop counters, and energy VAD;
- bounded 0.8-second wake history and 0.3-second command pre-roll;
- a replaceable `WakeWordDetector` and 3M INT8 sherpa keyword model;
- the configured local phrase “Hey Butters” with threshold/boost controls;
- a 110 ms asynchronous local acknowledgement chime;
- a state machine for wake, listen, stream STT, finalize, normalize, and reset;
- a replaceable `StreamingSTTEngine` and resident 20M INT8 English Zipformer;
- partial/final transcripts, 600 ms endpointing, and conservative aliases;
- no-speech, empty-result, STT-error, audio-error, and timeout recovery;
- explicit diagnostics, finite recordings, tests, and actual Pi benchmarks.

One persistent source owns ALSA across every normal state. KWS and STT consume
the same standardized stream and never race to reopen the webcam. Normal
operation does not save audio or publish it to MQTT.

The software path and earlier real captures are validated, but this milestone
is not yet a positive human-voice acceptance result. No deliberate speech
reached the requested capture windows, and late in the extended test session
the webcam audio endpoint entered a persistent `EIO` state that now requires a
physical USB replug. See **Hardware and human-validation status** and **Known
limitations** below.

Not implemented: LLM inference, intent/tool calling, skills, MQTT/Home
Assistant actions, InfluxDB queries, TTS, conversational memory, or a permanent
service. See [ARCHITECTURE.md](ARCHITECTURE.md),
[benchmarks/baseline.md](benchmarks/baseline.md),
[benchmarks/stt.md](benchmarks/stt.md), and
[benchmarks/live-voice.md](benchmarks/live-voice.md).

## Layout

```text
butters/
  README.md
  ARCHITECTURE.md
  benchmarks/{baseline,stt,live-voice}.md
  config/{audio.example,domain_vocabulary}.toml
  config/wakewords.txt
  requirements-stt.txt
  scripts/
    butters-audio, butters-stt, butters-wake, butters-live
    download-stt-model, download-wake-model, benchmark-stt, test-butters
  src/butters/
    audio/                 capture, conversion, VAD, pre-roll, chime
    stt/                   neutral engine, sherpa adapter, normalization
    wakeword/              neutral detector and sherpa KWS adapter
    live/                  session state machine and single-owner routing
    cli.py, config.py
  tests/
```

The local `butters/.venv`, `butters/models`, and machine-specific
`config/audio.local.toml` are ignored by Git.

## Dependencies and model installation

Prerequisites are Python 3.11+, ALSA `arecord`/`aplay`, `curl`, `sha256sum`, and
`tar`. This webcam also uses the already-installed `v4l2-ctl` for its optional
firmware warm-up. Audio-only code has no third-party Python dependency.

STT and wake both reuse `sherpa-onnx==1.13.4` and its matching core wheel in the
isolated Butters environment. No PyTorch, TensorFlow, NumPy, global package, or
existing project environment was added or changed in Milestone 3.

```bash
python3 -m venv butters/.venv
butters/.venv/bin/python -m pip install \
  --only-binary=:all: -r butters/requirements-stt.txt
./butters/scripts/download-stt-model
./butters/scripts/download-wake-model
```

Both downloaders verify pinned SHA-256 values. The STT archive is 127,887,156
bytes and selected inference files are 43,649,301 bytes. The wake archive is
32,885,699 bytes; active chunk-8 files are 5,449,043 bytes, while its clean
installed directory is about 15.4 MiB because both latency variants, the
English lexicon, and small natural fixtures are retained.

## Connected microphone

The microphone is part of an A4Tech FHD 1080P PC Camera:

- USB `09da:2695`, Sonix Technology Co., Ltd., serial `SN0001`;
- ALSA card ID `Camera`, device 0;
- stable PCM `hw:CARD=Camera,DEV=0`;
- native S16_LE, mono, 16-bit;
- native 8, 11.025, 16, 22.05, 24, 44.1, and 48 kHz rates.

Butters requests native 16 kHz mono S16_LE, so no conversion is required. The
numeric card happened to be 3 but is not configured because it can change
across boots.

This webcam has a firmware initialization quirk: initial audio reads failed
until a low-bandwidth UVC frame had streamed. The ignored local config uses the
stable by-ID video node for a single discarded 640x480 MJPEG warm-up before
every ALSA open:

```toml
[alsa]
device = "hw:CARD=Camera,DEV=0"
video_warmup_device = "/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._A4tech_FHD_1080P_PC_Camera_SN0001-video-index0"
```

Leave `video_warmup_device` empty for normal microphones. Use `plughw` instead
of `hw` only when discovery shows ALSA conversion is required.

That workaround enabled the measured recordings and five clean reopen cycles.
After extended repeated testing, however, the endpoint later stayed in `EIO`
at both 16 and 48 kHz even after verified UVC frames and while video remained
active. ALSA still enumerated the idle device and no process owned it. A
physical webcam replug is required before the remaining positive voice trial;
the software deliberately does not reset the shared USB bus.

## Discover and diagnose microphones

```bash
./butters/scripts/butters-audio discover
./butters/scripts/butters-audio discover --probe
./butters/scripts/butters-audio diagnose --seconds 10
```

Discovery lists all capture devices, stable ALSA IDs, `/proc/asound` native USB
formats, and direct versus converting 16 kHz probes. Diagnosis shows readable
RMS/dBFS, VAD, peak/clipping, overruns, drops, and pre-roll occupancy.

Create a diagnostic WAV only when explicitly requested:

```bash
./butters/scripts/butters-audio hardware-check \
  --seconds 8 --output /tmp/butters-webcam-check.wav
```

The output is always standard 16 kHz mono 16-bit PCM. Existing files are not
overwritten without `--overwrite`.

## STT diagnostic

Live configured microphone:

```bash
./butters/scripts/butters-stt --source alsa
```

Real-time file simulation or accelerated chunk processing:

```bash
./butters/scripts/butters-stt --source wave --input /path/to/speech.wav
./butters/scripts/butters-stt \
  --source wave --input /path/to/speech.wav --no-realtime
```

The WAV is still streamed in approximately 20 ms chunks. Output includes VAD
start, changed partials, final raw/normalized text, endpoint reason, RTF, CPU,
finalization/speech-end latency, RSS, and source counters.

## Wake-word diagnostic

```bash
./butters/scripts/butters-wake
./butters/scripts/butters-wake \
  --max-detections 5 --max-audio-seconds 120
```

The selected `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` model contains a
phone lexicon for `HEY` and `BUTTERS`; `config/wakewords.txt` supplies the
tokenized phrase. Defaults are one thread, chunk size 8, boost 1.5, and trigger
threshold 0.25. The runtime exposes token timestamps but no winning-path
confidence, so diagnostics print `confidence=n/a` rather than fabricate one.

## Live frontend

```bash
./butters/scripts/butters-live
```

Expected output:

```text
[READY] Waiting for wake word
[WAKE] phrase='HEY BUTTERS' confidence=n/a threshold=0.250 ...
[LISTENING] ack_launch=...
[PARTIAL] ...
[FINAL RAW] ...
[FINAL NORMALIZED] ...
[READY] Waiting for wake word
```

Useful bounded variants:

```bash
./butters/scripts/butters-live --cycles 5
./butters/scripts/butters-live --cycles 1 --no-chime
./butters/scripts/butters-live \
  --source wave --input /path/to/test.wav --no-realtime
```

States are `WAITING_FOR_WAKE`, `WAKE_DETECTED`, `LISTENING`, `FINALIZING`, and
`RETURNING_TO_IDLE`. No speech after wake times out in four seconds. A command
ends after about 600 ms silence or a native recognizer endpoint.

The KWS token-end timestamp estimates how much audio follows the wake phrase.
Only that tail, bounded to 0.3 seconds, is moved from the 0.8-second history to
command STT. This supports “Hey Butters what's the temperature” without
deliberately feeding the whole wake phrase. The 110 ms chime launches
asynchronously and uses a 120 ms VAD guard while new audio remains buffered.

## Hardware and human-validation status

Verified on the actual webcam:

- native 16 kHz capture and valid 12-/30-second PCM WAV metadata;
- five repeated open/capture/close cycles without lock or error;
- several 30- to 120-second continuous live sessions;
- zero observed overruns, drops, or clipping;
- post-start room median -41.7 dBFS and 99th percentile -38.7 dBFS;
- calibrated machine-local command VAD threshold -34 dBFS;
- five minutes of practical room audio with zero false “Hey Butters” wakes;
- real chime launch in 0.93-1.67 ms;
- chime did not activate command VAD after normal startup settling;
- no-speech timeout returned to ready automatically;
- full real KWS -> partial STT -> final -> ready flow with the model archive's
  natural `LIGHT UP` fixture.

No unmistakable human phrase reached the captured audio during the requested
interactive windows. Therefore positive “Hey Butters” reliability, varied
distance/volume, real user-command transcripts, project-vocabulary errors, and
human-speech endpoint latency remain pending. Do not interpret a window with no
spoken input as a missed-wake trial.

Finish that validation with:

```bash
./butters/scripts/butters-audio discover --probe
./butters/scripts/butters-wake --max-detections 5
./butters/scripts/butters-live --cycles 5
```

First physically replug only the webcam and confirm that both capture probes
say `yes`. Then say the wake phrase at varied distances/volumes in the wake
command. In the live command, try temperature, humidity/box three, printer
exhaust, desktop, CO2, printer-room air quality, SEN66, Grafana, Home Assistant,
and KR260 phrases. Record the recognizer output exactly; do not correct it
manually.

## Measured resource summary

The original Milestone 1 baseline had 2.205-2.219 GiB RAM available, about 1.5
GiB used, 25.5 MiB of 2 GiB zram occupied with no paging, 93-97% CPU idle,
59.9-61.3 C, and `throttled=0x0`. Home Assistant, Grafana, and InfluxDB were the
largest services.

During this longer Codex session, the current Butters-off comparison had
1,953-1,961 MiB available and 94.3% mean host CPU idle. Results on the actual
live stream:

| Phase | RSS | CPU (100%=one core) | Available RAM | Temperature |
| --- | ---: | ---: | ---: | ---: |
| KWS only | 61-66 MiB | 20.9-22.1% | 1,888-1,907 MiB | 59.9-63.8 C |
| Full live process waiting | 117-119 MiB | 21.7% | 1,829-1,835 MiB | 60.4 C |
| Full process with paced active STT | 125-126 MiB | STT 23.3% | 1,847-1,850 MiB | 61.8-62.3 C |

Passive KWS caused no paging. Later repeated short model loads/active
diagnostics coincided with about 2.75 MiB additional zram swap-out, no swap-in,
and no low-memory condition. A much later Butters-off audit after the extended
Codex/hardware session found 68.5 MiB zram occupied, 1.9 GiB RAM available,
only 21 additional swap-in pages since the initial baseline, 60.3 C, and
`throttled=0x0`; that interval included the development agents and cannot be
attributed solely to inference. Mosquitto, InfluxDB, Grafana, Home Assistant,
bridge, dashboard, export worker, Docker/containerd, and Tailscale remained
healthy; all checked local routes returned HTTP 200. See the live benchmark
for exact phase definitions and caveats.

## Tests

```bash
./butters/scripts/test-butters -q
```

The 33-test Butters suite covers conversion/WAV metadata, standardized chunks,
VAD/clipping, bounded buffers, repeated ALSA cleanup, optional UVC warm-up,
normalization, STT partial/final/reset, every live state recovery path, chime
invocation, single capture ownership, repeated interactions, and real local
sherpa STT/KWS model resets when the ignored models are installed.

## Known limitations

- Positive user-voice wake and STT validation remains pending as described
  above.
- The A4Tech webcam needs its configured UVC warm-up, emits a short
  non-clipping capture-start transient, and eventually wedged in persistent
  `EIO` during the extended test session. Replug/recovery validation is still
  required before always-on use.
- Energy VAD is a level gate, not a trained speech/noise classifier; another
  room/microphone needs new calibration.
- Domain STT hotword biasing is not enabled because the selected 20M STT
  archive lacks the required SentencePiece artifacts. Raw and normalized text
  remain separate; aliases only cover unambiguous whole phrases.
- SEN66, SHT41, Bambu, KR260, Kria, Grafana, and Home Assistant are expected to
  be difficult until actual user speech is measured.
- `arecord` can count xrun events but not exact lost samples, so the drop count
  is an event estimate.
- WAV input supports uncompressed integer PCM, not compressed/float WAV.
- No systemd service has been installed or enabled.
