# Human voice, production STT, and semantic endpoint acceptance

Measured on the repository Raspberry Pi 4 with the A4Tech FHD 1080P PC Camera
microphone on 2026-08-11. These are the human results supplied from the actual
interactive session plus repository-local implementation/benchmark evidence.
The raw user recording remains only at `/tmp/butters-daniel-stt-test.wav`; it is
not a repository fixture and must not be committed.

## Microphone acceptance

The accepted capture device is `hw:CARD=Camera,DEV=0`, native 16 kHz, mono,
S16_LE. A 12-second live test produced 601 frames and 12.020 seconds of audio
with zero overruns, zero dropped frames, and no clipping. Quiet-room levels were
generally -42 to -47 dBFS and normal speech roughly -22 to -30 dBFS.

The former USB/audio `EIO` condition disappeared after a physical camera
replug. The first frame still has a large approximately -3.3 dBFS transient;
standalone energy-VAD diagnostics can therefore emit an empty initial
utterance. It did not prevent wake-word operation and is not treated as an STT
accuracy result.

## Wake acceptance and metric meaning

The chunk-8 `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` model, one thread,
score 1.5, threshold 0.25 detected “Hey Butters” in 5 of 5 deliberate human
attempts with zero overruns or drops. Active inference files are approximately
5.2 MiB and the measured wake-only process averaged about 19.8% CPU, where
100% is one Pi core.

Sherpa token timestamps measure positions in the decoded audio. The difference
between total accepted audio and the last token timestamp is now reported as
`token_end_lag`, not “model delay” or inference latency. No inference wall-time
claim is derived from that value. It remains useful only for bounding audio
retained immediately after the wake phrase.

## STT A/B result

The original 20M INT8 model occupied about 41.6 MiB in selected files, used
roughly 105-107 MiB process RSS, and measured RTF 0.20-0.23 / CPU-per-audio
20-23%. It repeatedly decoded the fixed human recording of “What is the carbon
dioxide level in the printer room?” as fragments such as “Y LEVEL IN THE
PRINTER ROOM” or “IDE LEVEL IN THE PRINTER ROOM”. Feeding the recording as one
continuous utterance and ignoring endpoints did not repair it. The model itself
is therefore rejected for this installation's production accuracy.

On the same recording,
`sherpa-onnx-streaming-zipformer-en-2023-06-21` produced exactly:

```text
WHAT IS THE CARBON DIOXIDE LEVEL IN THE PRINTER ROOM
```

The selected INT8 files total 188,627,621 bytes (179.9 MiB). Observed
initialization was about 6-7.7 seconds, process RSS about 240-293 MiB, RTF
0.45-0.52, and STT CPU-per-audio about 45-52%. It remains comfortably faster
than real time on this Pi with one recognizer thread. Accuracy is selected over
the smaller footprint; the monitoring/server stack retains resource priority.

A repository-local post-implementation rerun of the same `/tmp` fixture
initialized in 5.851 seconds, showed 274.9 MiB RSS after load, and decoded the
sentence exactly at RTF 0.513 / CPU-per-audio 51.2%; process RSS after the run
was 239.5 MiB. The first-frame transient separately opened an empty 0.640-second
diagnostic utterance before the real speech. That artifact is reported, not
counted as an accuracy failure or hidden from the two-utterance total.

The larger model is the code/config default and the local ignored Pi config
selects it. `scripts/download-stt-model` installs only its four required INT8
files and verifies each pinned SHA-256. `butters/models/` remains Git-ignored.
Missing files cause an explicit model-incomplete error; no normal command
automatically downloads this large archive.

## Endpoint ownership and observed failures

With Sherpa endpointing enabled, a natural hesitation yielded only “WHAT IS THE
HUMIDITY” and finalized at the recognizer endpoint. Disabling it preserved a
deliberate hesitation/filler as one command: “WHAT IS THE AH CARBON DIOXIDE
LEVEL IN THE PRINTER ROOM”. This is direct evidence that conversational endpoint
policy belongs to Butters rather than Sherpa.

The next observed failure was the external fixed 600 ms VAD release, which
again finalized “WHAT IS THE HUMIDITY” before “in filament box two”. Production
live settings now separate all relevant clocks:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `vad.release_ms` | 300 ms | Energy-VAD hysteresis only; not a command endpoint |
| `live.provisional_endpoint_silence_ms` | 1000 ms | Preview the current transcript through local deterministic routing |
| `live.hard_endpoint_silence_ms` | 2000 ms | Finalize acoustically if speech has not resumed |
| `live.continuation_timeout_seconds` | 12 s | Maximum bounded clarification/follow-up state |
| `stt.sherpa_endpoint_enabled` | false | Prevent recognizer-native preemption |

At the provisional threshold, a matched deterministic/diagnostic route
finalizes immediately. A known route with required missing arguments keeps its
same STT stream open through the hard endpoint. An unknown fragment also waits
for the hard endpoint but gains no authority. At the hard endpoint, a known
incomplete route emits its targeted clarification and enters
`AWAITING_CONTINUATION`; unrecognized text returns safely to idle with a repeat
request.

The incomplete representation is a `RoutedIntent` with clarification status,
the intended skill, already-resolved arguments, and explicit
`missing_arguments`. For example, “what is the humidity” carries
`get_sensor_value`, `metric=humidity`, and missing `entity`. Continuation text
is considered only while such a request is pending. A complete standalone
route wins before any merge, so “Is Home Assistant healthy?” cancels stale
humidity context. Every effective request still traverses normalization,
router validation, `PolicyValidator`, and `SkillRegistry`; incomplete requests
cannot execute and successful continuation produces one final execution.

The router drops only standalone benign hesitation tokens `uh`, `um`, and `ah`
from its matching view. Raw and STT-normalized transcripts retain them.

## Automated evidence and remaining human step

Focused tests cover provisional completion, 0.8-second pauses, incomplete
survival between provisional and hard thresholds, hard targeted clarification,
follow-up slot filling, unrelated complete replacement, corrupt text, expiry,
no duplicate finals/execution, filler matching, and disabled Sherpa endpoints.
The full suite and final resource/service audit are reported in the milestone
handoff.

Semantic endpoint timing is state-machine tested with deterministic frames; it
has not yet had the final five-case spoken acceptance run. The next human run
must use the exact current CLI command in the handoff and observe real pauses,
filler, hard clarification, and unrelated-command replacement. Do not call
token-timestamp lag inference latency, and do not count automated frame tests as
additional human observations.
