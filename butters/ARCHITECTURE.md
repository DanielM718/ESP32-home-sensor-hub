# Butters architecture

## Scope and boundaries

Butters is a modular subsystem inside this repository because it will consume
sensor data and integrate with MQTT, Home Assistant, InfluxDB, Wake-on-LAN, and
monitoring sessions. It is not part of the critical environmental data path.
Mosquitto, the sensor bridge, InfluxDB, Grafana, Home Assistant, dashboards, and
historical data continue to operate when every Butters process is stopped or
failed.

The intended pipeline is:

```text
USB microphone
  -> audio capture and 16 kHz mono S16_LE normalization
  -> bounded pre-roll
  -> lightweight wake-word detector
  -> acknowledgement sound
  -> streaming speech recognition and endpointing
  -> transcript normalization
  -> deterministic intent matching
       -> typed proposal for normal commands
       -> small local LLM only for unresolved language/tool selection
  -> policy validation of every proposed tool call (default deny)
  -> restricted skill/tool registry
       -> read-only integration adapters
       -> future safe Home Assistant / MQTT / WOL controls
       -> future confirmation-gated disruptive actions
  -> response generation
  -> local TTS
```

Normal commands such as "what is the bedroom temperature?" or "turn on the
printer-room fan" should match a deterministic intent and typed arguments. They
must not pay LLM latency. An LLM is a constrained fallback for phrasing and
selection, not an authority and not the execution environment.

## Current implementation through Milestone 5

Both sources implement the same pull-based `AudioSource` contract and emit
16 kHz, mono, signed 16-bit little-endian PCM. Milestone 3 adds replaceable
wake detection and an explicit live session coordinator without changing that
contract:

```text
ALSA microphone -> arecord/ALSA conversion -------+
                                                  +-> one AudioSource owner
PCM WAV -> streaming channel/rate conversion ----+     -> 20 ms standardized frames
                                                        -> 0.8 s bounded wake history
                                                        -> WakeWordDetector
                                                             -> 3M sherpa KWS
                                                        -> wake event + async chime
                                                        -> 0.3 s command pre-roll
                                                        -> energy VAD / endpointing
                                                        -> StreamingSTTEngine
                                                             -> resident 20M INT8 Zipformer
                                                             -> partial/final raw text
                                                        -> conservative normalization
                                                        -> return to wake listening
```

The live source delegates device access and any required native-format
conversion to the installed ALSA stack. The selected A4Tech webcam natively
supports the internal format, so this host uses `hw:CARD=Camera,DEV=0`; the
stable ALSA card ID avoids boot-dependent card numbers. This webcam has a
firmware initialization quirk: initial audio reads returned an I/O error until
one low-bandwidth UVC frame had streamed. The configured stable `/dev/v4l/by-id`
path performs that bounded warm-up before every ALSA open. Normal microphones
leave the option empty. A warm-up failure is recorded but does not prevent an
ALSA attempt. The webcam later entered a persistent endpoint `EIO` state after
extended testing that UVC warm-up could not clear; the controller's bounded
reopen/retry path recovers ordinary source failures, but this hardware state
currently requires a physical replug rather than a risky shared-bus reset.

The `LiveVoiceController` is a per-frame state machine with
`WAITING_FOR_WAKE`, `WAKE_DETECTED`, `LISTENING`, `FINALIZING`, and
`RETURNING_TO_IDLE` states. It owns no device. One persistent source supplies
both KWS and STT, so a wake never closes/reopens the microphone. Source reopen
is reserved for an actual capture error and includes bounded retry/backoff.
No-speech, empty transcript, recognizer error, maximum command duration, and
normal endpoint paths all reset worker stream state and return to wake mode.

The wake detector is the small INT8 chunk-8
`sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` open-vocabulary transducer. Its
phone lexicon contains `HEY` and `BUTTERS`, encoded in a separate tracked
keyword file, so no model training or TTS-generated training corpus is needed.
The neutral `WakeWordDetector` interface keeps sherpa APIs out of the state
machine. The default trigger threshold is 0.25 with 1.5 token boosting and one
thread. sherpa exposes the threshold and token timestamps but not a winning
path confidence; diagnostics print confidence as unavailable rather than
fabricating it.

At detection, the token-end timestamp estimates how much already-captured
audio follows the wake phrase. Only that tail, bounded by the 0.3-second command
pre-roll, is retained for STT. This preserves an immediate command without
feeding the full 0.8-second wake phrase history to the recognizer. New frames
continue without a capture gap. The 110 ms chime launches asynchronously; VAD
has a matching 120 ms acknowledgement guard while audio remains buffered.

For an STT session, two active 20 ms frames open an utterance. The coordinator
feeds the bounded post-wake command pre-roll into a fresh recognizer stream,
then accepts each subsequent frame incrementally. Approximately 600 ms of VAD
silence or a native recognizer endpoint finalizes promptly. The stream is
discarded/reset while both models remain resident, preventing partial or
decoder state from leaking to the next interaction.

`StreamingSTTEngine` owns the lifecycle operations (`start_utterance`,
`accept_audio`, partial lookup, endpoint detection, `finalize`, `reset`, and
`close`). Session, normalization, source, and future router code do not import
sherpa-onnx. Replacing the recognizer therefore does not require rewriting the
assistant. The current adapter is local CPU-only inference; neither audio nor
transcripts leave the Pi.

The STT default remains one inference thread based on this Pi's measured RTF 0.268,
roughly 107-110 MiB loaded process RSS, and negligible model-idle CPU. Higher
thread values used most of the four cores, ran hotter, and did not provide a
worthwhile latency improvement. Full results and accuracy limitations are in
[benchmarks/stt.md](benchmarks/stt.md). The combined live process measured
approximately 117-126 MiB RSS, about 22% of one core while continuously running
KWS, and no capture drops. See [benchmarks/live-voice.md](benchmarks/live-voice.md).

The current energy VAD remains deterministic RMS hysteresis, not a trained
speech/noise classifier. Actual room noise calibrated this host's local gate to
-34 dBFS; another room or microphone must be measured independently.

Milestone 4 adds one common post-transcript execution path for direct text,
streaming WAV/STT results, and optional live final transcripts:

```text
raw text / final STT transcript
  -> conservative transcript and request normalization
  -> concept-based IntentRouter
       -> entity and metric alias registries
       -> matched typed intent, clarification, or unsupported result
  -> explicit SkillRegistry
       -> strict typed argument parser
       -> default-deny PolicyValidator
       -> READ_ONLY implementation
  -> narrow integration adapter
       -> typed structured result
  -> independent ResponseFormatter
       -> concise response text
  -> optional TextToSpeechEngine
       -> separate explicit AudioOutput
```

The six current skills cover one sensor metric, sensor reporting status,
last-seen time, maximum filament-box humidity, printer-room air quality, and
server health. All are `READ_ONLY`. Every skill declares its stable name,
description, argument parser, action class, policy authorizer, implementation,
and deadline. The registry rejects unknown names, missing/unexpected fields,
unknown entities/metrics, incompatible entity/metric pairs, non-allow-listed
comparisons, and any action class not admitted by policy. Integration failures
are sanitized at this boundary. The current deadlines are defense-in-depth;
the dashboard HTTP request itself has the enforceable four-second I/O timeout.

`config/assistant.toml` is the reviewed entity exposure list. It maps dashboard
air-quality source `office` to `printer_room` and environment source IDs 1-3 to
the three filament boxes, with unambiguous aliases. This explicit mapping is
intentional: deployed-but-unreviewed sources do not become voice entities by
accident. Metrics form a separate compatibility allow-list. Ambiguous
questions request clarification rather than selecting the first candidate.

Current sensor data comes from the established local dashboard `/api/latest`
representation through `DashboardSensorAdapter`. That interface was selected
after inspection because it already applies the deployed node IDs, freshness,
battery validity, field availability, and project air-quality policy. The
service-owned InfluxDB/MQTT credentials remain outside Butters, and neither the
router nor skill registry receives the dashboard URL/client. The adapter has a
hard response-size bound, typed parsing, a four-second network timeout, and a
five-second in-process cache to avoid repeated expensive latest queries.
Direct InfluxDB access may be appropriate for a later historical skill, but it
would be a separate least-privilege adapter rather than leaked query authority.

`LocalServerHealthAdapter` reads fixed local kernel/sysfs metrics and runs only
two fixed command shapes: `systemctl is-active` over a compiled allow-list and
`vcgencmd get_throttled`. Callers cannot supply a unit name, command, path, or
argument. There is no general shell, filesystem, database, MQTT, or Home
Assistant skill.

The live CLI's optional assistant handoff uses a bounded two-item worker queue.
Sensor-query latency cannot block the single audio-capture owner; saturation
drops the semantic request visibly rather than allowing unbounded memory or
audio backlog. This path is unit-tested but was not run against the known-wedged
webcam in Milestone 4.

Local TTS uses `SherpaOnnxPiperTTS`, a replaceable adapter around the existing
sherpa-onnx ARM64 runtime and the Piper-compatible
`vits-piper-en_US-kathleen-low` voice. Synthesis returns 16 kHz mono PCM in a
typed `SynthesizedSpeech`; `WaveFileOutput` is a separate explicit sink. No
normal query writes audio. The current adapter returns a complete utterance,
not first-chunk streaming. Its measured two-thread warm footprint stabilized
near 182 MiB, idle CPU was effectively zero, and RTF was 0.45-0.49. Cold load
was 3.7-4.0 seconds, so on-demand versus low-priority residency remains a
deployment choice rather than an assumption.

Milestone 5 adds an optional engine-neutral semantic fallback without changing
the deterministic or policy boundaries:

```text
normalized request
  -> IntentRouter
       matched --------------------------> typed proposal
       explicit clarification/control ---> local fixed response (no model)
       unresolved and fallback-eligible
         -> LanguageModel (untrusted, no adapters/credentials)
              -> tool proposal | clarify sentinel | unsupported sentinel
         -> strict syntax/model-format normalization
         -> existing SkillRegistry.validate/execute
              -> strict argument parser
              -> existing default-deny PolicyValidator
              -> read-only implementation only when allowed
```

`LanguageModel.propose_tools()` receives only compact public tool schemas,
canonical entity/metric aliases, and the current request. The concrete client
can talk only to a configured unauthenticated HTTP loopback URL. It supports
llama.cpp's native OpenAI-compatible tool calls, strict standalone JSON, and
the official LFM2 Python-like list representation. The latter is parsed with a
restricted AST/literal parser: only a one-element list containing one simple
named call with keyword primitive literals is admitted. `eval`, `exec`, Python
code, surrounding prose, attributes, positional arguments, multiple calls,
and non-literals are rejected.

`clarify_request` and `unsupported_request` are non-executable catalog entries;
their output is mapped to fixed local text. A syntactically valid real call is
still untrusted. Unknown skill names, missing/unexpected arguments, invalid or
incompatible entities/metrics, comparison values, and action classes fail at
the existing policy/registry boundary. Model timeout, process failure,
malformed output, and policy denial produce a safe unsupported response while
the deterministic path remains available.

No candidate passed the Pi resource/latency/quality gates, so configuration
leaves this fallback disabled and no model worker is selected or installed as
a service. The fixed 120-case ground-truth corpus and model-independent scorer
remain for future hardware. See [benchmarks/llm.md](benchmarks/llm.md).

## Future process layout

Keep responsibilities separable even if the first deployment combines a few
of them:

- **audio service:** the sole microphone owner; produces normalized frames and
  maintains bounded pre-roll;
- **wake-word worker:** always resident, consumes local audio frames, and emits
  meaningful wake events;
- **session coordinator:** acknowledgement, STT session lifetime, endpointing,
  cancellation, and degraded-mode behavior;
- **STT worker:** resident only if measured resources support it; streams
  partial/final transcripts;
- **router and skill host:** deterministic intents, policy enforcement, typed
  skill registry, audit events, and timeouts;
- **LLM worker:** optional constrained fallback with no credentials and no
  direct integrations;
- **TTS worker:** generates local response audio, preferably on demand unless
  model residency is justified;
- **integration adapters:** narrow MQTT, Home Assistant, InfluxDB, and WOL
  clients with least-privilege credentials.

High-rate PCM must stay off MQTT. On one Pi, use a bounded Unix-domain socket,
pipe, or shared-memory ring with backpressure/drop accounting. MQTT is suitable
later for low-rate semantic events such as `wake_detected`, `session_started`,
final transcript metadata, intent result, health, and availability. Raw audio
stays local and is not retained during normal operation.

## Restricted skill and policy model

The LLM is never allowed arbitrary shell access, Python evaluation, file-system
access, MQTT publication, database access, or Home Assistant credentials. It
can only propose a tool name and schema-validated arguments from a registry the
router supplies. The policy layer independently checks the proposal and invokes
the integration adapter.

The policy enum defines three action classes for future compatibility:

1. **Read-only:** current sensor values, trends, service health, device state.
2. **Safe/reversible control:** allow-listed lights or fans with value bounds,
   rate limits, and clear result reporting.
3. **Disruptive/sensitive:** shutdown, broad power changes, door/security
   actions, destructive data changes, or ambiguous targets. These require an
   explicit confirmation or are excluded from voice control entirely.

Only the first class is registered today. Merely adding an enum value or skill
implementation does not authorize it: policy continues to deny anything other
than `READ_ONLY` until a later reviewed milestone changes both the registry and
validator deliberately.

An output grammar/JSON schema can make syntax valid; it does not make a tiny
model's choice correct. Tests must cover wrong-device names, prompt injection in
sensor text, unsupported actions, ambiguous rooms, negation, confirmation, and
timeouts. Policy defaults to deny.

## Failure and degradation behavior

- Audio failure prevents new sessions but does not affect home monitoring.
- Wake-word failure may expose push-to-talk or a local diagnostic, never a
  continuous cloud stream.
- STT failure ends the session with a short local error cue.
- LLM failure falls back to deterministic intents; ordinary commands remain
  available.
- TTS failure can use an acknowledgement/error tone and publish a semantic
  status event.
- Integration timeouts are reported; they are not retried indefinitely.
- Every queue and audio buffer is bounded. Slow consumers drop their own work
  with counters instead of exhausting RAM or blocking critical services.

## Candidate plan and measured selection

Streaming STT, wake word, and TTS have selected implementations. Milestone 5
installed isolated candidate GGUFs/runtime for benchmarking only; it selected
no production LLM. Project claims and model file sizes are useful filters, not
substitutes for testing on this Pi.

### Wake word

Milestone 3 selected sherpa-onnx's
`sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` model. It is fully local,
streaming, INT8, ARM64-compatible, and reuses the already-isolated sherpa 1.13.4
runtime. Its open-vocabulary phone lexicon permits `HEY BUTTERS` without a
custom training project. The selected chunk-8 inference files total 5,449,043
bytes. The official archive is 32,885,699 bytes; the pinned installer retains
both latency variants, the lexicon, and small tests, approximately 15.4 MiB.
See the
[sherpa keyword model inventory](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html).

openWakeWord was evaluated but not installed. Its last PyPI release is 0.6.0
from February 2024, its included models do not contain “Hey Butters,” and a
responsible custom model needs thousands of positive examples plus large
negative data. Adding ONNX Runtime/TensorFlow Lite and a custom-training stack
would duplicate the current runtime without solving this milestone more
reliably. It remains a future accuracy comparison if a properly trained model
is produced. See the
[openWakeWord project](https://github.com/dscripka/openWakeWord).

Porcupine remains a licensing/account-key comparison rather than the default.
It supports Pi-class hardware and custom keywords but requires a Picovoice
AccessKey and separate terms. See the
[Porcupine Raspberry Pi documentation](https://picovoice.ai/docs/quick-start/porcupine-raspberrypi/).

The configured “Hey Butters” token sequence loads successfully and live room
audio produced no false wakes in five minutes of practical observation.
However, no deliberate human wake utterances reached the microphone during the
interactive windows, so positive detection reliability is not yet claimed.

### Streaming STT

Milestone 2 selected and tested **sherpa-onnx 1.13.4** and
`sherpa-onnx-streaming-zipformer-en-20M-2023-02-17`, using the int8 encoder and
decoder/joiner. The official model inventory describes it as a small English
streaming model; its int8 ONNX files total 43,649,301 bytes. The runtime's
current CPython 3.13 ARM64 wheel runs directly on this Pi without a general ML
framework and exposes online transducer/endpoint APIs. See
[sherpa-onnx Linux installation](https://k2-fsa.github.io/sherpa/onnx/install/linux.html)
and the
[streaming Zipformer model inventory](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html#csukuangfj-sherpa-onnx-streaming-zipformer-en-20m-2023-02-17-english).

The engine supports contextual hotwords under modified beam search, but the
chosen archive lacks the SentencePiece `bpe.model`/`bpe.vocab` needed to encode
English phrases. Hotwords are therefore intentionally off; `tokens.txt` is not
silently treated as a different file format. A configured domain-term list and
small whole-phrase normalization layer prepare for later comparison with a
compatible model. Human household-command audio, room noise, and the webcam
remain the important accuracy tests before deployment.

### Local LLM

Milestone 5 tested current official/practical candidates through the official
ARM64 llama.cpp b10360 build (`48d22e295`) and 2K context, one model at a time:

| Candidate | Quantization | File | Loaded RSS | Decision |
| --- | --- | ---: | ---: | --- |
| LiquidAI LFM2-700M | Q4_K_M | 468,624,320 B | 509.7 MiB | Rejected: malformed native output; wrong policy-denied constrained proposal; 8.6 s warmed |
| Qwen3-0.6B | Q4_0 | 428,970,080 B | 697.7 MiB | Rejected: native proposal exceeded 120 s and residency displaced too much memory |
| LiquidAI LFM2-1.2B-Tool | Q4_K_M | 730,894,048 B | 822.7 MiB | Rejected: official native format exceeded bounded timeout and caused heavy zram displacement |

LFM2-1.2B-Tool used its official tool-call chat convention and greedy
generation. Qwen used non-thinking mode. For the generic 700M model,
llama.cpp JSON-schema generation was also tested as defense-in-depth; the
grammar made syntax valid but could not make the semantic choice correct, and
the downstream policy denied it. This is exactly why constrained generation is
not authorization.

Candidate loads moved host zram from the previous 86.5 MiB baseline to about
1.04 GiB and the thermal flags recorded a past soft-temperature-limit event.
Cold load alone took 12.7-20.3 seconds. Therefore neither resident nor
per-request cold inference is acceptable on the current shared Pi. No winner
is forced; `llm.enabled` remains false. A future separate edge host or much
smaller deterministic semantic classifier should rerun the same 120 fixed
cases before integration.

Primary references are [llama.cpp b10360](https://github.com/ggml-org/llama.cpp/releases/tag/b10360),
the [grammar/JSON-schema guide](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md),
[LFM2-700M-GGUF](https://huggingface.co/LiquidAI/LFM2-700M-GGUF),
[LFM2-1.2B-Tool-GGUF](https://huggingface.co/LiquidAI/LFM2-1.2B-Tool-GGUF),
and [Qwen3-0.6B-GGUF](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF).

### TTS

Milestone 4 selected the Piper-compatible VITS voice
`vits-piper-en_US-kathleen-low`, executed through the already installed
**sherpa-onnx 1.13.4** offline TTS API. It is a single-speaker U.S. English
16 kHz low-quality voice. The ONNX file is 63,052,430 bytes and the complete
directory is 81,049,294 bytes with eSpeak data. The model card identifies its
dataset as CC0. See the
[official sherpa model page](https://k2-fsa.github.io/sherpa/onnx/tts/all/English/vits-piper-en_US-kathleen-low.html)
and [Piper voice inventory](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/kathleen/low).

The maintained Piper codebase has moved to
[OHF-Voice Piper](https://github.com/OHF-Voice/piper1-gpl) under GPL-3.0. It was
not installed because sherpa already provides the necessary ARM64 VITS runtime;
this avoids another package without claiming that the archived original Piper
runtime remains current. A future streaming-output comparison may still test
the maintained runtime if first-audio latency becomes important.

One thread synthesized at RTF about 0.72-0.75 and used about 98% process CPU.
Two threads synthesized at RTF 0.45-0.49 and about 191% process CPU while
leaving two cores available for the server, so two is the current development
default. Loaded idle CPU was approximately 0.002%. The warmed two-thread RSS
stabilized near 191,082,496 bytes; engine close returned the process to about
139 MiB RSS, subject to allocator retention. See
[benchmarks/skills-tts.md](benchmarks/skills-tts.md).

## Resource protection and systemd plan

Do not install a permanent unit until live audio, CPU, and memory behavior have
been measured. Eventually place Butters services in their own slice or define
equivalent per-unit controls. Preserve the current services and assign AI work
lower priority rather than changing the monitoring stack.

Candidate controls, with values deliberately left for benchmark results:

- positive `Nice=` for STT, LLM, and TTS workers;
- lower `CPUWeight=` for the Butters slice while leaving critical services at
  their existing/default weight;
- `MemoryHigh=` as a reclaim/throttling boundary below a hard `MemoryMax=`;
- positive `OOMScoreAdjust=` so optional AI workers are preferred over
  Mosquitto, InfluxDB, Home Assistant, or the sensor bridge under pressure;
- bounded `TasksMax=`, restart backoff, runtime directories, an unprivileged
  `butters` user, and only the audio-group/device access actually needed.

Set limits from measured whole-pipeline peaks, not weight-file size. A limit
that causes continuous reclaim or zram paging is too aggressive even if the
process remains below `MemoryMax`. Never raise the critical services' OOM
scores to make room for Butters.

## Future satellites

Keep the backend session, router, policy, and integrations independent of a
specific microphone. A future satellite can implement the same session input
contract and send authenticated, encrypted, bounded audio streams to the Pi.
Wake detection can remain on each satellite to avoid continuous LAN audio.
Satellite audio also stays off MQTT; MQTT carries discovery, availability, and
semantic assistant events. This preserves the backend design when microphones
move to other rooms.
