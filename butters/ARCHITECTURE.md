# Butters architecture

## Scope and boundaries

Butters is a modular subsystem inside this repository because it will consume
sensor data and integrate with MQTT, Home Assistant, InfluxDB, Wake-on-LAN, and
monitoring sessions. It is not part of the critical environmental data path.
Mosquitto, the sensor bridge, InfluxDB, Grafana, Home Assistant, dashboards, and
historical data continue to operate when every Butters process is stopped or
failed.

## Beta 1 deployed topology

Beta 1 implements the first persistent browser service without merging into
the Flask dashboard:

```text
private HTTPS browser
  -> Tailscale Serve (tailnet only; terminates TLS and adds identity)
  -> 127.0.0.1:8090 Starlette/Uvicorn
       -> bounded sessions + structured in-memory TraceBuffer
       -> shared BetaAssistantService
            -> deterministic SkillRegistry (default)
            -> local diagnostic planner/playbooks
            -> optional bounded general/diagnostic cloud provider
       -> browser PCM -> explicit downmix/resample -> existing StreamingSTTEngine
       -> local or explicitly paid TTS -> WAV response
       -> SQLite non-content usage/budgets + non-secret voice presets
       -> review-gated Codex job metadata
```

The web daemon is an unprivileged `butters` systemd service with a strict
loopback bind, bounded worker pool/queues, clean executor shutdown, restart
limits, no capabilities, protected system/home, and only `/var/lib/butters`
writable. It does not own ALSA. Tailscale identity headers are authoritative
only in this loopback proxy topology; every admin API and admin WebSocket also
checks the exact configured login allow-list. Mutations require same-origin
HTTPS plus a session CSRF token. Development bypass requires loopback,
`development_mode`, and an explicit `BUTTERS_DEV_ADMIN=1`.

Browser sessions use 256-bit random opaque IDs in HttpOnly/Secure/SameSite
cookies, expire on inactivity, and cap active sessions, messages, and context
characters. Detailed traces are a bounded memory ring and expose only
programmatic stages/reason codes—not model chain-of-thought. Persistent usage
stores provider/route/model/token/cost/latency/error metadata only; no prompt,
transcript, response, evidence, raw log, audio, or secret columns exist.
All paid text/STT/TTS permit-call-record sequences share one daemon gate so
concurrent requests cannot race persistent totals. Uncertain failures consume
their conservative preflight estimate.

The general cloud path receives only bounded recent context and up to six
relevant read-only schemas. Every requested tool re-enters typed parsing and
`PolicyValidator`. The loop caps output, context bytes, retries, wall time,
rounds, tools, and cost. Unknown model/speech pricing denies the call. The
diagnostic-specific cloud path remains intact for diagnostic requests.

Codex has no normal-chat endpoint. Administrator skill descriptions are
validated as untrusted, explicitly read-only job data. A clean base commit,
detached worktree, allowed path set, file/patch bounds, tests, and explicit
approval gate the patch. The subprocess environment is constructed from a
small allow-list and excludes provider, MQTT, Home Assistant, database, and
admin secrets. The in-daemon skill runner also refuses a parent containing
recognized secrets, because stripping child variables alone does not prevent
same-user parent inspection through `/proc`; such deployments need a distinct
secret-free worker. Codex completion cannot commit, push, deploy, or restart.

The intended pipeline is:

```text
USB microphone
  -> audio capture and 16 kHz mono S16_LE normalization
  -> bounded pre-roll
  -> lightweight wake-word detector
  -> acknowledgement sound
  -> streaming speech recognition
  -> acoustic silence + router-aware semantic endpoint policy
  -> transcript normalization
  -> deterministic intent matching
       -> typed proposal for normal commands
       -> small local LLM only for unresolved language/tool selection
       -> diagnostic request
            -> local deterministic planner/playbook
            -> typed read-only evidence collection
            -> local assessment when confidence is sufficient
            -> explicitly enabled cloud reasoning only when insufficient
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

## Physical voice implementation retained by Beta 1

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
                                                        -> energy VAD observations
                                                        -> StreamingSTTEngine
                                                             -> resident 2023-06-21 INT8 Zipformer
                                                             -> partial/final raw text
                                                        -> semantic endpoint evaluator
                                                             -> complete / incomplete / unrecognized
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
`WAITING_FOR_WAKE`, `WAKE_DETECTED`, `LISTENING`, `FINALIZING`, bounded
`AWAITING_CONTINUATION`, and `RETURNING_TO_IDLE` states. It owns no device. One persistent source supplies
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
then accepts each subsequent frame incrementally. VAD release is only acoustic
hysteresis. At 1.0 second of below-threshold audio, the current partial is
previewed through local deterministic/diagnostic routing without executing a
skill or invoking a model. A complete route finalizes promptly; a route with
required missing arguments or an unrecognized fragment remains open until the
2.0-second hard acoustic endpoint. Sherpa's internal endpoint is explicitly
disabled by production configuration so it cannot preempt this policy.

At a hard endpoint, a recognized incomplete route carries its intended skill,
resolved arguments, and `missing_arguments`. It emits the targeted router
question and enters `AWAITING_CONTINUATION` for at most 12 seconds. Speech that
arrives before the hard endpoint stays in the same recognizer stream. A later
clarification turn is merged only while this explicit pending request exists;
a complete standalone route is evaluated first and cancels stale context.
Unrecognized text gains no pending authority. A successful logical request
emits one executable handoff, then all recognizer and conversational state is
cleared while both models remain resident.

`StreamingSTTEngine` owns the lifecycle operations (`start_utterance`,
`accept_audio`, partial lookup, endpoint detection, `finalize`, `reset`, and
`close`). Session, normalization, source, and future router code do not import
sherpa-onnx. Replacing the recognizer therefore does not require rewriting the
assistant. The current adapter is local CPU-only inference; neither audio nor
transcripts leave the Pi.

The STT default remains one inference thread, but production selection is now
`sherpa-onnx-streaming-zipformer-en-2023-06-21`. It exactly decoded the fixed
real-user carbon-dioxide query that the old 20M model repeatedly truncated.
Measured larger-model initialization is 6-7.7 seconds, RSS 240-293 MiB, RTF
0.45-0.52, and CPU-per-audio 45-52%. Accuracy is preferred over the old
107 MiB process footprint while still comfortably below real time and leaving
monitoring services priority. See
[benchmarks/human-voice-semantic-endpoint.md](benchmarks/human-voice-semantic-endpoint.md).

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

The initial skills covered sensor metrics/status/last-seen/comparison,
printer-room air quality, and server health; printer work brought the pre-Beta
inventory to thirteen. Beta 1 adds five promoted host/stack/network/history/
project skills for eighteen total. All are `READ_ONLY`. Every skill declares
its stable name,
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

## Local diagnostics and safe escalation

Diagnostics are a separate typed path, not another large intent function and
not a synonym for cloud classification:

```text
DiagnosticRequest
  -> DiagnosticPlanner
       -> DiagnosticPlan / named playbook
       -> bounded typed DiagnosticTool calls
            -> existing default-deny PolicyValidator
            -> local read-only implementation
            -> EvidenceItem
  -> EvidenceBundle
  -> LocalDiagnosticRules
  -> DiagnosticAssessment
       status / categorical confidence
       findings -> evidence IDs
       root cause / hypotheses / unresolved questions
       recommended next steps / escalation reason
  -> DiagnosticAnswer
       concise_voice_text
       detailed_text
```

`DiagnosticPlanner` evaluates request complexity before tools run. Local rules
evaluate observation complexity afterward: known playbook match, contradiction,
number of unresolved causes, unfamiliar errors, missing/stale observations,
systems implicated, and whether logs need interpretation. Saying “diagnose” is
not sufficient for a cloud route. A stopped Grafana or bridge is solved
locally. Conversely, a short question can escalate when its observations are
contradictory.

The ten initial playbooks cover a non-reporting sensor, sensor-to-dashboard
flow, Grafana current data, Home Assistant sensor integration, MQTT, InfluxDB,
server health, configured network host/port reachability, KR260 basic health,
and the whole monitoring path. The pipeline rules inspect every planned stage
but report the first known degraded stage as the supported causal boundary.
Stale sensor plus healthy MQTT/bridge is only moderate confidence because the
Pi cannot distinguish node power from radio delivery. Healthy current state
that contradicts the reported symptom is insufficient and escalatable.

Confidence is `CONFIRMED`, `HIGH`, `MODERATE`, `LOW`, or `INSUFFICIENT`.
Unobservable tool state is never treated as proof that the target failed. A
failed diagnosis is not a Butters failure; it is either a successful explicit
escalation condition or, when cloud is disabled, a best-local-evidence result.

### Diagnostic tool and evidence boundary

`DiagnosticToolSpec` declares the tool name, natural-language description,
argument dataclass/schema, strict parser, target authorizer, `READ_ONLY` action
class, timeout, maximum output bytes, `EvidenceItem` output, error behavior,
and sensitivity behavior. The 43 tools cover server, network, sensor stack,
two approved containers, and an honest KR260 missing-transport abstraction.

Target values are enums generated from reviewed local configuration. Systemd
units, Docker names, localhost endpoints, host aliases, MQTT topics, sensor
entities, metrics, and history ranges are never free strings after parsing.
Direct procfs/sysfs/socket/HTTP APIs are preferred. Fixed subprocess argument
arrays exist only where the platform API is the command (`systemctl`,
`journalctl`, `vcgencmd`, `ping`, `tailscale`, and `docker`). No tool accepts a
command or filesystem path.

An `EvidenceItem` contains stable ID, kind, source, target, UTC observation
time, status, structured values, optional excerpt, freshness/age, error,
truncation, sensitivity/redaction metadata, and `untrusted=true`. Bundle IDs are
unique and the serialized session is capped at 64 KiB. Log calls cap time,
lines, and bytes. The sanitizer recursively handles values and text, redacts
secret-like keys/headers/tokens/private keys, and omits unknown objects.

Every cloud prompt treats evidence and request-carried text as untrusted data.
Text in a log cannot add a function, target, permission, or policy. Even a
syntactically valid model function call passes the same local parser,
allow-list, action-class check, and `PolicyValidator`; only then can Butters run
the read-only implementation.

### Provider-neutral cloud analysis

`CloudReasoner.analyze(request, evidence, available_tools,
diagnostic_context, budget)` returns a typed tool request or conclusion and has
no execution API. `CloudDiagnosticEscalator` owns the bounded loop:

```text
model function request
  -> local schema + PolicyValidator
  -> local read-only tool
  -> sanitized EvidenceItem
  -> model follow-up
  -> submit_diagnosis with cited evidence IDs
```

The first provider is the OpenAI Responses API. Its request uses flat strict
function definitions, disables parallel tool calls, defaults to `store=false`,
and requires a strict non-executable `submit_diagnosis` function. Provider
objects never cross the neutral interface. Model-produced conclusions are
parsed and bounded again locally; invented evidence IDs fail closed.

Official current documentation was checked for the Responses function-call
loop, strict schema rules, `gpt-5.6-luna`/`terra`/`sol`, supported reasoning
efforts, Pro mode, and pricing. Configuration—not diagnostic rules—holds model
IDs, tier switches, budgets, and dated prices. The initial policy is local,
Terra/high, then Sol/xhigh; contradictory evidence can start at Sol. Explicit
exhaustive requests may use Sol/max plus Pro mode. Luna is not a default
diagnostic hop absent benchmark evidence that it improves cost/quality.

One investigation allows four tool rounds, eight tool calls, five Responses
calls, 90 seconds, 1,200 output tokens, two escalation steps, and one retry by
default. Repeated identical calls, duplicate evidence IDs, budget exhaustion,
unavailable provider, malformed calls, invalid targets, and session expiry stop
safely. The 15-minute `DiagnosticSession` retains only evidence, completed
calls, hypotheses, escalation/usage accounting, and a stopping reason.

Usage records deliberately omit request/evidence content. They include model,
effort/tier, token categories, tool rounds, time, estimated dated-price cost,
success/error, and escalation. Beta 1 writes this non-content metadata plus
model-free request summaries to SQLite under `/var/lib/butters`. Per-request
and durable daily/monthly limits apply before a call, so daemon restart does
not reset spending totals.

Cloud requires explicit configuration plus `OPENAI_API_KEY`; the key is read
only for the Authorization header, never put in a request body, diagnostic,
benchmark, telemetry record, or tracked file. The persistent service constructs
the provider boundary, but committed defaults make no paid calls; availability
still requires explicit paid configuration, reviewed pricing, and a key.

### Engineering remediation is a separate authority

The general cloud analyst has no repository or shell tool. Potential software,
configuration, or deployment defects are classified separately and can be
rendered as an `EngineeringRemediationRequest` for an
`EngineeringRemediator`:

```text
assessment -> local remediation classification -> CodexJobFactory
           -> INSPECT or PATCH Codex CLI job -> typed remediation result
```

The local factory chooses the configured repository alias, validates the real
path against symlink/traversal escape, records the base commit, maps only
approved test IDs to fixed test commands, sanitizes evidence references, and
never accepts a raw command or deployment target. INSPECT uses Codex's current
ephemeral non-interactive read-only sandbox and checks that Git status is
unchanged. PATCH requires a clean tree, uses workspace-write, reports bounded
changed files/tests, and cannot deploy, restart production, commit, or push.
Programmatic execution is disabled in current configuration, so a reviewed
manual job is the default result.

DEPLOY exists only as a future enum and is rejected. A later implementation
needs an explicit confirmation transaction with pre/post health, recorded
revisions, exact target/service allow-lists, regression probes, audit output,
and safe rollback. A cloud failure does not automatically authorize Codex, and
a Codex failure does not invalidate the diagnostic session.

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
- **diagnostic host:** short-lived local planner, read-only tools, evidence,
  playbooks, and optional explicitly configured cloud loop;
- **LLM worker:** optional local constrained fallback with no credentials and no
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
- Diagnostic cloud failure, missing key, rate limit, malformed output, or
  budget exhaustion returns the best local assessment and evidence.
- Codex unavailability/timeout/malformed output leaves the diagnostic session
  intact and never expands into deployment authority.
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
audio produced no false wakes in five minutes of practical observation. After
the camera replug, deliberate human testing detected 5/5 attempts at threshold
0.25 with no capture overruns or drops.

### Streaming STT

The adapter remains **sherpa-onnx 1.13.4**, but real-user A/B evidence replaced
the original 20M selection with
`sherpa-onnx-streaming-zipformer-en-2023-06-21`. Its required INT8 files total
188,627,621 bytes. The runtime's current CPython 3.13 ARM64 wheel runs directly
on this Pi without a general ML framework. Model files remain ignored, and the
explicit installer downloads only when invoked, retains required INT8 files,
and checks their pinned SHA-256 digests. Missing weights fail with an explicit
incomplete-model error. See
[sherpa-onnx Linux installation](https://k2-fsa.github.io/sherpa/onnx/install/linux.html)
and the
[streaming Zipformer model inventory](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html).

The engine supports contextual hotwords under modified beam search, but the
selected archive lacks the SentencePiece `bpe.model`/`bpe.vocab` needed to encode
English phrases. Hotwords are therefore intentionally off; `tokens.txt` is not
silently treated as a different file format. A configured domain-term list and
small whole-phrase normalization layer prepare for later comparison with a
compatible model. The fixed real-user query is exact on the selected model;
broader household vocabulary/noise acceptance and the new semantic pauses
remain human tests before deployment.

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
