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
  -> restricted skill/tool registry
       -> read-only MQTT / InfluxDB / Home Assistant queries
       -> safe Home Assistant / MQTT / WOL controls
       -> confirmation-gated disruptive actions
  -> small local LLM only for unresolved language/tool selection
  -> policy validation of every proposed tool call
  -> response generation
  -> local TTS
```

Normal commands such as "what is the bedroom temperature?" or "turn on the
printer-room fan" should match a deterministic intent and typed arguments. They
must not pay LLM latency. An LLM is a constrained fallback for phrasing and
selection, not an authority and not the execution environment.

## Current implementation through Milestone 3

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

Use at least three action classes:

1. **Read-only:** current sensor values, trends, service health, device state.
2. **Safe/reversible control:** allow-listed lights or fans with value bounds,
   rate limits, and clear result reporting.
3. **Disruptive/sensitive:** shutdown, broad power changes, door/security
   actions, destructive data changes, or ambiguous targets. These require an
   explicit confirmation or are excluded from voice control entirely.

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

Only the streaming STT candidate below was installed in Milestone 2. Other
candidates remain research targets. Project claims and model file sizes are
useful filters, not substitutes for testing on this Pi.

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

Use `llama.cpp` directly on ARM64 with a short context and a Q4 GGUF. Its grammar
and JSON-schema constraints are valuable defense-in-depth for typed tool
proposals. The router should still handle common commands without it. See
[llama.cpp](https://github.com/ggml-org/llama.cpp) and its
[grammar/JSON-schema guide](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md).

Candidates by tier:

| Tier | Initial candidate | Reason and caution |
| --- | --- | --- |
| ~350M | SmolLM2-360M-Instruct Q4 | Very small, Apache-2.0, English/on-device oriented. Its official model card only calls out function-calling support for the 1.7B member, so assume weak tool selection until proven. |
| ~700M | Qwen3-0.6B GGUF Q4 | Official GGUF exists under Apache-2.0 and is close to the target tier. Benchmark non-thinking/short constrained output; do not infer reliability from general benchmarks. |
| ~1.2B | Llama 3.2 1B Instruct (1.23B) Q4 | Better capacity candidate with a custom Meta license, but less comfortable for residency. Validate the exact GGUF source and tool schema behavior. |

Relevant primary model cards are
[SmolLM2-360M-Instruct](https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct),
[Qwen3-0.6B-GGUF](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF), and
[Llama-3.2-1B-Instruct](https://huggingface.co/meta-llama/Llama-3.2-1B-Instruct).
Test each against the same allow-listed home-intent corpus, including refusal
and ambiguity cases. A smaller deterministic classifier may outperform all
three for routine routing.

### TTS

Benchmark **Piper** first. The maintained Open Home Foundation project provides
a local C/C++ core, Python API, streaming/raw output, and ARM64 Raspberry Pi 4
binaries. Voice model size and quality vary. A persistent worker avoids model
reload latency, but on-demand loading protects RAM; measure both. See
[OHF-Voice Piper](https://github.com/OHF-Voice/piper1-gpl) and note its GPL-3.0
license before integration.

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
