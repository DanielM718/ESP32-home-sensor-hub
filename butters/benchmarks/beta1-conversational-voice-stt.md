# Beta 1 conversational voice and STT investigation

Measured on the repository Raspberry Pi 4 on 2026-08-15. This report separates
model initialization, audio preprocessing/control, streaming inference, and
finalization. It supersedes any assumption that the approximately ten-second
admin trace represented only recognition inference.

Production remained online throughout. Audio fixtures and raw JSON results
were kept under `/tmp`; nothing was copied to `/opt/butters`, no service was
restarted, and no production model or configuration was changed.

## Host and backend capability

| Item | Observed capability |
| --- | --- |
| Host | Raspberry Pi 4 Model B Rev 1.2, 3.7 GiB RAM |
| CPU | 4-core ARM Cortex-A72, aarch64, 600-1500 MHz |
| SIMD | `fp asimd` (ARM NEON) |
| Runtime | Python 3.13.5, sherpa-onnx/core 1.13.4 |
| Model format | INT8 ONNX online transducer |
| Installed execution path | sherpa-onnx `OnlineRecognizer`, `provider="cpu"`, one thread |
| Graphics devices | Host exposes vc4/v3d with `/dev/dri/card0`, `card1`, and `renderD128`; the development sandbox hides them |
| Graphics tooling | `vulkaninfo`, `clinfo`, `glxinfo`, and `v3dinfo` are not installed |
| Recognizer accelerator support | The installed online recognizer exposes CPU, CUDA, and CoreML providers; no Vulkan or OpenCL provider is installed |

VideoCore VI is neither CUDA nor CoreML. There was therefore no viable GPU
backend to benchmark with the installed STT stack. A fabricated comparison
would be misleading: this investigation has **no Pi GPU latency result**. The
selected path is the measured INT8/NEON-capable CPU runtime.

## Diagnosed lifecycle

Before this milestone, every `/ws/voice` connection called the recognizer
factory and closed that recognizer at socket teardown. The selected accurate
model was consequently loaded once per utterance. Independent cold loads
during this investigation took 8.503, 12.625, and 14.104 seconds under the
current host load. That cold initialization alone accounts for an observed STT
trace near 10 seconds; streaming inference then added work while audio arrived.

The service now owns a bounded recognizer pool. It prewarms one model during
application startup, leases it to only one voice session at a time, resets and
returns it after a healthy turn, and discards it after a recognizer failure.
The existing bounded admission queue handles contention. Text operation remains
available if startup prewarm fails, and voice retries model creation lazily.

## Method

The enhanced `benchmark-stt` command accepts repeatable `WAV=TRANSCRIPT`
expectations and emits JSON with hardware, backend, model, quantization,
threads, cold initialization, per-clip preprocessing/control, transcription,
total latency, RTF, RSS, WER, and exact-match fields. For example:

```bash
./butters/scripts/benchmark-stt --threads 1 --repeats 1 --idle-seconds 0 \
  --expected padded-humidity-box-three.wav="What is the humidity in box three?" \
  /tmp/butters-beta1-stt-corpus.1GI4UW/padded-humidity-box-three.wav
```

Three controlled duration fixtures exercised the current accurate model:

| Fixture | Duration | Purpose |
| --- | ---: | --- |
| Local Piper sensor command plus trailing silence | 2.080 s | Short interactive command |
| Official model natural-speech WAV, including 8-to-16 kHz conversion | 4.825 s | Medium conversion/control path |
| Ten-second truncation of an official natural-speech WAV | 10.000 s | Longer utterance scaling |

Accuracy comparison used the seven commands in
[`beta1-stt-command-corpus.json`](beta1-stt-command-corpus.json), synthesized
locally with the configured Piper voice and padded with 0.8 seconds of trailing
silence, plus the existing untracked real-user CO2 recording. Synthetic speech
is useful controlled A/B evidence but is not a substitute for different human
voices, rooms, microphones, or iPhone Safari acceptance.

## Warm duration results

Selected 2023-06-21 model, INT8 CPU, one thread:

| Audio | Preprocessing/control | Streaming transcription | Total STT stage | Transcription RTF | Total RTF |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2.080 s | 0.116 s | 1.038 s | 1.153 s | 0.499 | 0.555 |
| 4.825 s | 0.263 s | 2.360 s | 2.623 s | 0.489 | 0.544 |
| 10.000 s | 0.540 s | 5.224 s | 5.764 s | 0.522 | 0.576 |

Smaller 20M model, INT8 CPU, one thread:

| Audio | Preprocessing/control | Streaming transcription | Total STT stage | Transcription RTF | Total RTF |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2.080 s | 0.108 s | 0.439 s | 0.546 s | 0.211 | 0.263 |
| 4.825 s | 0.246 s | 1.012 s | 1.258 s | 0.210 | 0.261 |
| 10.000 s | 0.524 s | 2.247 s | 2.771 s | 0.225 | 0.277 |

Both are faster than real time. Because inference is streaming during capture,
the user-visible delay after the stop tap is normally finalization plus routing,
not the entire table's accelerated-file wall time. The measured accurate-model
finalization maximum in this command run was approximately 165 ms. New trace
fields report permission/setup, capture duration, pool acquisition/cold load,
preprocessing, inference, stop-to-final, audio duration, and inference RTF so a
future slow turn can be localized rather than inferred.

## Model and accuracy comparison

| Model/configuration | Selected INT8 files | Cold loads observed | Peak process RSS | Corpus exact | Mean WER | Deterministic command outcome |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2023-06-21 selected Zipformer, INT8 CPU, 1 thread | 188,627,621 bytes (179.9 MiB) | 8.503-14.104 s | 295.16 MiB | 3/8 | 0.267 | 5/8 executed the intended request, 2/8 safely clarified, 1/8 safely fell back |
| 20M 2023-02-17 Zipformer, INT8 CPU, 1 thread | 43,649,301 bytes (41.63 MiB) | 2.592-3.532 s | 112.86 MiB | 0/8 | 0.760 | 0/8 executed the intended request |

The larger model exactly recognized the two direct synthetic humidity/CO2
commands and the real human CO2 command. Other useful outputs included
`READINGS IN THE PRINTER ROOM`, which routed to the intended aggregate query,
and `UM THE HUMIDITY IN BOX THREE`, whose filler was safely ignored by the
deterministic router. `P M TWO POINT FIVE` normalized to PM2.5 but correctly
asked for an entity; `HUMIDITY IN BOX` also clarified instead of guessing. The
weakest variant became `THE PRINTER ROOM CENSURES LOOK` and safely fell back.

The 20M model was approximately twice as fast and much smaller, but its command
accuracy was unacceptable. In particular, the same real human recording became
`IDE LEVEL IN THE PRINTER ROOM`, reproducing the previously diagnosed loss of
the metric. Speed does not compensate for zero successful deterministic command
routes in this corpus.

Earlier controlled thread results for that smaller model were also considered:
one thread had RTF 0.268 and about 99.8% active process CPU; two threads improved
RTF only to 0.230 while averaging 354.6% CPU; three and four threads regressed to
0.317 and 0.455 and reached as high as 79.9 C. No new high-thread stress sweep
was run while production and the development workload were active. One thread
remains the service-safe choice.

## Selection and safety observations

The recommended default remains the accurate 2023-06-21 INT8 model, CPU
provider, one inference thread, with one process-local warm recognizer. This
removes the per-utterance 8.5-14.1-second cold load while retaining measured
human-command accuracy and bounding memory/concurrency. The smaller model is
not selected.

The controlled runs peaked at 75.471 C. During the whole development session,
zram occupancy rose from about 1,424 MiB to 1,613 MiB, with approximately
187 MiB of swap-out and 0.6 MiB of swap-in by page counters. The interval also
contained the development workload, so those deltas are not attributed solely
to inference. A post-run firmware value of `0x80000` records that a soft
temperature limit occurred sometime since boot; its current-limit bits were
clear, and no pre-run firmware value exists for causal attribution. Benchmarks
were stopped rather than adding sustained contention. The deployed service,
`/healthz`, and `/readyz` remained healthy afterward.

The remaining performance validation is an actual post-deployment iPhone run:
record first-tap permission latency, warm stop-to-final latency, short and long
human commands, page focus/navigation, and repeated voice turns. Repository
tests and local WAV benchmarks do not claim Safari microphone acceptance.
