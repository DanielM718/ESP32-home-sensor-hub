# Butters

Butters is the local voice-assistant subsystem for this home network. It lives
inside the sensor repository because later restricted skills will query sensors
and integrate with MQTT, InfluxDB, Home Assistant, monitoring sessions, and
Wake-on-LAN. It is not part of the critical monitoring data path.

## Current status: Milestone 5, deterministic first; no resident LLM selected

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
- concept-based intent routing with explicit clarification/unsupported paths;
- a typed, default-deny registry containing six read-only skills;
- explicit entity/metric allow-lists and strict argument validation;
- bounded read-only current-data access through the deployed dashboard API;
- a fixed-command, fixed-service server-health adapter with no shell skill;
- structured skill results and separate concise response formatting;
- direct text, streaming-WAV, and asynchronous live-transcript entry paths;
- an engine-neutral, optional local-LLM proposal interface behind unresolved
  deterministic routes only;
- strict JSON and official LFM2 tool-call parsing with no `eval`/`exec`;
- non-executable clarification/unsupported outcomes and unchanged default-deny
  skill-policy validation for every proposed call;
- a fixed 120-case tool-routing/safety corpus and repeatable scorer;
- local Piper-compatible TTS through the existing sherpa-onnx runtime;
- a separate `TextToSpeechEngine`/`AudioOutput` boundary and explicit WAV output;
- explicit diagnostics, finite recordings, tests, and actual Pi benchmarks.

One persistent source owns ALSA across every normal state. KWS and STT consume
the same standardized stream and never race to reopen the webcam. Normal
operation does not save audio or publish it to MQTT.

The deterministic assistant answers real sensor questions without a generative
model. Milestone 5 tested three local GGUF candidates, but none met this shared
Pi's latency, RAM/swap, thermal, and first-proposal quality gates. LLM fallback
therefore remains disabled by default; ordinary requests are unchanged and no
model service is installed. The webcam endpoint remains in the persistent
`EIO` state discovered in Milestone 3 and the user is away, so this milestone deliberately used direct text,
streaming WAVs, and simulated session handoff. It did not probe, reset, or
reopen the wedged device. See **Hardware and human-validation status** and
**Known limitations** below.

Not implemented/enabled: a production LLM worker or accepted model,
write/control skills, MQTT publication, Home Assistant actions, arbitrary
database queries, conversational memory, physical speaker validation, or a
permanent service. See [ARCHITECTURE.md](ARCHITECTURE.md),
[benchmarks/baseline.md](benchmarks/baseline.md),
[benchmarks/stt.md](benchmarks/stt.md), and
[benchmarks/live-voice.md](benchmarks/live-voice.md), and
[benchmarks/skills-tts.md](benchmarks/skills-tts.md), and
[benchmarks/llm.md](benchmarks/llm.md).

## Layout

```text
butters/
  README.md
  ARCHITECTURE.md
  benchmarks/{baseline,stt,live-voice,skills-tts,llm}.md
  benchmarks/llm-corpus.json
  config/{audio.example,assistant,domain_vocabulary}.toml
  config/wakewords.txt
  requirements-stt.txt
  scripts/
    butters-audio, butters-stt, butters-wake, butters-live
    butters-query, butters-speak
    download-{stt,wake,tts}-model, download-{llama-runtime,llm-models}
    benchmark-{stt,skills,tts,llm}, test-butters
  src/butters/
    audio/                 capture, conversion, VAD, pre-roll, chime
    stt/                   neutral engine, sherpa adapter, normalization
    wakeword/              neutral detector and sherpa KWS adapter
    live/                  session state machine and single-owner routing
    routing/               concept, entity, and metric resolution
    skills/                typed registry, policy, structured results
    integrations/          bounded dashboard and local-health adapters
    responses/             result-to-text templates
    llm/                   neutral proposal API, strict parsers, scorer/client
    tts/                   engine-neutral synthesis and output adapters
    assistant.py           common orchestration and bounded live handoff
    assistant_cli.py       text/WAV/TTS commands
  tests/
```

The local `butters/.venv`, `butters/models`, `butters/runtime`, and machine-specific
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
./butters/scripts/download-tts-model
```

Both downloaders verify pinned SHA-256 values. The STT archive is 127,887,156
bytes and selected inference files are 43,649,301 bytes. The wake archive is
32,885,699 bytes; active chunk-8 files are 5,449,043 bytes, while its clean
installed directory is about 15.4 MiB because both latency variants, the
English lexicon, and small natural fixtures are retained.

The TTS downloader installs only `vits-piper-en_US-kathleen-low`. Its official
archive is 67,118,360 bytes and its pinned SHA-256 is
`3dd0adaf077e19de32876608c3ac3d4a0a46a6f06310cc7a5633b3b47d762cde`.
The ONNX weights are 63,052,430 bytes; the complete local voice directory is
81,049,294 bytes including eSpeak data. Model binaries remain Git-ignored.

Milestone 5 added no Python or global dependency. Reproduce the isolated
official ARM64 llama.cpp b10360 runtime and the three pinned GGUF candidates
with:

```bash
./butters/scripts/download-llama-runtime
./butters/scripts/download-llm-models
```

The runtime reports build 10360 / commit `48d22e295`. Model files are
468,624,320 bytes (LFM2-700M Q4_K_M), 428,970,080 bytes (Qwen3-0.6B Q4_0),
and 730,894,048 bytes (LFM2-1.2B-Tool Q4_K_M). Both installers verify pinned
SHA-256 values. Runtime/build artifacts and GGUFs stay ignored.

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

## Deterministic read-only assistant

Text mode is the preferred Milestone 4 development path while the webcam is
wedged:

```bash
./butters/scripts/butters-query "what is the CO2 level"
./butters/scripts/butters-query --json "which filament box has the highest humidity"
./butters/scripts/butters-query "what is the server status"
```

Every request follows the same boundary:

```text
normalize -> deterministic router -> typed skill registry -> default-deny policy
          -> narrow integration adapter -> structured result -> response template
```

The router varies words and aliases rather than matching whole sentences. For
example, “what's box 3 humidity,” “how humid is filament box three,” and
“humidity for container 3” all select `filament_box_3`/`humidity`. “What's the
co two level” is conservatively normalized to `CO2`. An unqualified humidity
question asks which sensor; multiple boxes ask for clarification; controls and
unknown complex requests are explicitly unsupported.

Six skills are registered, and every one is classified `READ_ONLY`:

| Skill | Allow-listed operation |
| --- | --- |
| `get_sensor_value` | One compatible current metric for one configured entity |
| `get_sensor_status` | Reporting status for one entity or all configured entities |
| `get_sensor_last_seen` | Timestamp/age for one entity |
| `compare_sensor_metric` | Maximum current humidity across `filament_boxes` only |
| `get_room_air_quality` | Structured SEN66/dashboard summary for one air station |
| `get_server_health` | Fixed local metrics and fixed service-unit status list |

Unknown skills, unexpected/missing arguments, unknown entities, incompatible
metrics, non-allow-listed comparisons, and every action class other than
`READ_ONLY` are denied. The router never receives a URL, credential, database
client, MQTT client, subprocess API, or filesystem API. The health adapter may
only invoke fixed `systemctl is-active` arguments and `vcgencmd get_throttled`;
there is no caller-controlled command string and no `run_shell` skill.

### Entities and real data source

`config/assistant.toml` is non-secret and maps the deployed dashboard source
IDs to reviewed user-facing entities:

| Entity | Deployed source | Aliases |
| --- | --- | --- |
| `printer_room` | air-quality location `office` / SEN66 node 100 | printer room, printer area, office, SEN66 |
| `filament_box_1` | environment node `1` | box/filament box/container one or 1 |
| `filament_box_2` | environment node `2` | box/filament box/container two or 2 |
| `filament_box_3` | environment node `3` | box/filament box/container three or 3 |

Metrics are temperature, humidity, battery voltage, CO2, PM2.5, PM10, VOC
index, and NOx index. The integration uses the existing local
`http://127.0.0.1:8080/api/latest` representation because it already applies
the deployed node identity, freshness, battery validity, field availability,
and air-quality policy. The service credentials required for direct InfluxDB,
MQTT, or Home Assistant access are intentionally not copied into Butters.

The adapter imposes a four-second HTTP deadline, a 2 MiB response bound, typed
parsing, and a five-second in-process cache. A persistent assistant normally
pays one latest-query cost per cache interval; separate one-shot CLI processes
start cold. Sensor/database errors are converted to concise unavailable
results. Missing and stale readings never become zero, and comparison excludes
them explicitly.

During the 2026-08-11 real corpus run, representative answers were:

```text
Printer room CO2 is 684 ppm.
Printer room PM2.5 is 11.4 micrograms per cubic meter.
Filament box one battery voltage is 3.442 volts.
Filament box two is the most humid at 31 percent. Excluded unavailable data
from Filament box three.
3 of 4 configured sensors are reporting. Filament box three is stale.
```

These are point-in-time examples, not fixtures or promised current readings.
The cold first query took 1.354 seconds; cached queries in the same process took
approximately 0.5-4 ms. See `benchmarks/skills-tts.md` for the full corpus.

## Optional constrained LLM fallback

The deterministic router remains first. A matched sensor query never calls a
model; explicit ambiguous questions such as “what is the humidity” still ask
for clarification without allowing a model to guess; deterministic control
denials also bypass it. Only a route explicitly marked as unresolved/fallback
eligible may call `LanguageModel.propose_tools()`.

The model receives a compact non-secret tool/alias catalog and can return one
of: a typed read-only skill proposal, `clarify_request`, or
`unsupported_request`. The latter two are local sentinel outcomes, not skills.
JSON and LFM2's official `[function(keyword=value)]` representation are parsed
without evaluation. A real proposal then passes the same known-skill, strict
argument, entity/metric compatibility, action-class, and skill-policy checks
as a deterministic proposal. The model process receives no credentials,
adapters, shell, filesystem, Python, MQTT, database, Home Assistant, or network
tool.

`assistant.toml` intentionally has `llm.enabled = false`. The actual Pi tests
selected **none acceptable**:

| Candidate | Cold load | RSS | Proposal result |
| --- | ---: | ---: | --- |
| LFM2-700M Q4_K_M | 13.86 s | 509.7 MiB | malformed native output; constrained output chose a wrong policy-denied call; 8.6 s even warmed |
| Qwen3-0.6B Q4_0 | 12.74 s | 697.7 MiB | no native proposal before 120 s |
| LFM2-1.2B-Tool Q4_K_M | 20.34 s | 822.7 MiB | no native proposal before the bounded timeout |

Candidate loading pushed zram from the prior 86.5 MiB baseline to about
1.04 GiB, and the benchmark recorded a past soft-temperature-limit flag. A
full 120-case model sweep was stopped rather than displacing the critical home
stack. The fixed corpus and scorer remain available for suitable future
hardware:

```bash
# Against an explicitly, manually started loopback llama.cpp worker:
./butters/scripts/benchmark-llm \
  --server http://127.0.0.1:18080 \
  --model butters-router --profile lfm2

# Inspect routing; deterministic commands still work if the worker is absent.
./butters/scripts/butters-query --show-route "what is the CO2 level"
./butters/scripts/butters-query --llm --show-route \
  "how damp is the third filament container"
```

The CLI prints `ROUTE: deterministic`, `llm_fallback`, `clarification`, or
`unsupported`, plus the model proposal and policy outcome when requested. An
LLM timeout/crash/malformed proposal safely returns unsupported; it cannot take
down deterministic routing. See `benchmarks/llm.md` for exact hashes, prompt
and generation speeds, thread matrices, resource observations, and the honest
N/A corpus metrics after the resource-gate abort.

## WAV-to-assistant mode

Stream a file through the existing source/VAD/STT path and then through the
same assistant used by text and live modes:

```bash
./butters/scripts/butters-query --wav /path/to/request.wav
./butters/scripts/butters-query \
  --wav /path/to/request.wav --no-realtime --json
./butters/scripts/butters-query \
  --wav /path/to/request.wav --no-realtime \
  --tts-output /tmp/butters-response.wav --overwrite
```

The file is never handed to an offline whole-file recognizer. It remains
20 ms streaming input with VAD, partial hypotheses, endpointing, and raw versus
normalized final text. A low-quality Kathleen TTS query used for integration
testing was recognized as `ON DIOXID` rather than “what is the carbon dioxide
level.” The router correctly refused to infer a sensor query and TTS generated
the unsupported response. This proves the wiring and safe failure path, not
domain-command STT accuracy; a successful speech-to-real-sensor acceptance run
still needs a suitable human recording after webcam recovery.

## Local TTS

Butters uses the existing `sherpa-onnx==1.13.4` ARM64 runtime with the
Piper-compatible VITS voice `vits-piper-en_US-kathleen-low`. This avoids a
second runtime and preserves separate `TextToSpeechEngine` and `AudioOutput`
interfaces. Only an explicit command writes synthesized audio:

```bash
./butters/scripts/butters-speak \
  "Printer room CO2 is 742 parts per million." \
  --output /tmp/butters-response.wav
```

Output is 16 kHz, mono, 16-bit PCM WAV. No speaker was confirmed remotely, so
physical playback was not attempted. The current sherpa adapter returns a
complete utterance rather than streaming its first audio chunk. Two threads
are the measured default: roughly 1.8-2.0 seconds to synthesize about four
seconds of speech (RTF 0.45-0.49), about 182 MiB warmed RSS, and about 191%
process CPU while active. Cold load takes 3.7-4.0 seconds and loaded idle CPU
was effectively zero. Because load latency is noticeable while the warmed
footprint is modest but not free, deployment should benchmark on-demand versus
a low-priority resident worker alongside the eventual LLM before deciding.

Run the repeatable resource test with:

```bash
./butters/scripts/benchmark-tts --threads 2 --repeats 5
./butters/scripts/benchmark-skills
```

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
./butters/scripts/butters-live --assistant
```

Expected output:

```text
[READY] Waiting for wake word
[WAKE] phrase='HEY BUTTERS' confidence=n/a threshold=0.250 ...
[LISTENING] ack_launch=...
[PARTIAL] ...
[FINAL RAW] ...
[FINAL NORMALIZED] ...
[ROUTE] skill=get_sensor_value confidence=...
[RESPONSE] ...
[READY] Waiting for wake word
```

`--assistant` sends each non-empty final transcript to a bounded two-item
worker queue. Dashboard latency therefore never blocks the sole audio-capture
loop; a full queue drops only the semantic request with a visible error. The
webcam is currently wedged, so this handoff is automated-test/file prepared but
was not invoked against ALSA in Milestone 4.

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

Milestone 4 began its TTS measurements with about 1.79 GiB available RAM,
81.5 MiB zram used, and 67.2 C. The first one-thread repeated TTS run increased
zram occupancy by about 5 MiB; the subsequent two-thread five-run test did not
increase it further. After all query/STT/TTS work, the host had 1.78 GiB
available, 86.5 MiB zram used, load 0.40/0.43/0.45, 66.7 C, and
`throttled=0x0`. All nine fixed service units were active and all seven
dashboard probes returned HTTP 200. Exact TTS peaks and caveats are in
`benchmarks/skills-tts.md`.

Milestone 5 began near 1.8 GiB available and 86.5 MiB zram used. It loaded one
LLM candidate at a time under `nice +10`; RSS ranged from 509.7 to 822.7 MiB.
After the bounded probes and thread microbenchmarks, model processes were gone
and 2.17 GiB was available, but compressed zram remained at 1.04 GiB. Peak
temperature was 74.0 C and `get_throttled=0x80000` recorded a past soft thermal
limit, so no further model/corpus load was attempted. All checked dashboard
routes remained HTTP 200. This result supersedes the earlier preliminary idea
that a 700M model might be comfortably resident.

At the later final audit, 2.03 GiB remained available and zram had recovered
partly to 858.8 MiB as pages were touched back in. All nine protected services
were active; dashboard, InfluxDB, Grafana, and Home Assistant health checks
returned HTTP 200. The high cumulative swap-in/out deltas confirm that model
loading displaced real server pages, rather than merely increasing an unused
counter.

## Tests

```bash
./butters/scripts/test-butters -q
```

The 103-test Butters suite covers conversion/WAV metadata, standardized chunks,
VAD/clipping, bounded buffers, repeated ALSA cleanup, optional UVC warm-up,
normalization, STT partial/final/reset, every live state recovery path, chime
invocation, single capture ownership, repeated interactions, and real local
sherpa STT/KWS model resets when the ignored models are installed. Milestone 4
adds phrase variants, aliases, ambiguity, invalid entities/metrics, strict
argument parsing, policy denial, structured results, timeouts, stale/missing
data, comparisons, fixed health commands, response templates, TTS abstraction
and WAV metadata, text-mode orchestration, and bounded asynchronous live
handoff.
Milestone 5 adds deterministic bypass, fallback invocation, model
timeout/process-failure recovery, native/JSON normalization, rejection of
prose/code/multiple calls, unknown skill/entity/metric and control denial,
clarification safety, policy-only validation without adapter access, and all
120 fixed-corpus invariants.

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
- The small STT model performed poorly on the low-quality Kathleen synthetic
  voice, so no successful synthetic domain-query transcript is claimed.
- `api/latest` is deliberately reused for correctness, but a cold query takes
  about 1.35 seconds on this deployment. Persistent caching limits load; a
  future narrowly scoped latest-state IPC/API can improve latency if the
  dashboard endpoint becomes a bottleneck.
- The current TTS adapter returns full-utterance PCM, and physical speaker
  output is unvalidated. The Kathleen voice is compact and usable for pipeline
  work but was selected for resource fit, not a voice-quality acceptance test.
- Only four user-facing entities are mapped. Adding a sensor requires an
  explicit reviewed `assistant.toml` entry; unknown deployed sources are not
  silently exposed.
- No candidate local LLM passed the resource/latency/quality gates, so the
  implemented optional fallback is disabled and has no production worker.
- Full per-model 120-case accuracy is deliberately unavailable: the run was
  aborted after zram reached about 1 GiB and a soft-temperature-limit event was
  recorded. `benchmarks/llm.md` reports probe-level outcomes without inflating
  them into percentages.
- No write/control skill, permanent service, or production audit log exists
  yet.
- `arecord` can count xrun events but not exact lost samples, so the drop count
  is an event estimate.
- WAV input supports uncompressed integer PCM, not compressed/float WAV.
- No systemd service has been installed or enabled.
