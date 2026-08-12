# Streaming STT benchmark

> Historical 20M-model baseline. Real-user A/B testing rejected this model and
> selected the larger 2023-06-21 Zipformer; see
> [human-voice-semantic-endpoint.md](human-voice-semantic-endpoint.md).

Measured on the repository's Raspberry Pi 4 on 2026-08-10. The webcam
microphone was not connected. Recognition validation used the model archive's
small natural LibriSpeech WAVs through the same `WaveAudioSource`, conversion,
20 ms frame, VAD, and pre-roll path that live ALSA capture will use.

## Selection

| Candidate | Streaming/ARM64 fit | Resource/architecture decision |
| --- | --- | --- |
| sherpa-onnx 20M streaming Zipformer INT8 | Native online transducer, partials/endpoints, current Python 3.13 ARM64 wheel, explicit Pi support | Selected: 41.63 MiB inference files and no general ML framework. |
| Vosk `small-en-us-0.15` | Continuous/partial API and official Raspberry Pi support | Credible fallback with a 40 MiB model, but its official guidance estimates roughly 300 MiB runtime memory versus the measured approximately 110 MiB sherpa process. |
| whisper.cpp `tiny.en` | ARM CPU support; its Pi streaming demonstration processes overlapping windows with multi-second steps | Secondary comparison only: this is not the desired native transducer/endpoint streaming behavior. |

[Vosk's official model inventory](https://alphacephei.com/vosk/models) and
[streaming C API](https://github.com/alphacep/vosk-api/blob/master/src/vosk_api.h)
support the comparison above. The
[official whisper.cpp Pi discussion](https://github.com/ggml-org/whisper.cpp/discussions/166)
shows its stepped-window approach.

The initial engine is `sherpa-onnx` 1.13.4 with
`sherpa-onnx-streaming-zipformer-en-20M-2023-02-17`, using its INT8 encoder,
decoder, and joiner. This was selected before implementation for these reasons:

- it is a true streaming transducer with incremental partial results, stream
  reset, and native endpoint detection;
- the current release provides a CPython 3.13 manylinux ARM64 wheel and the
  project explicitly supports Linux ARM64/Raspberry Pi;
- it accepts 16 kHz mono samples and needs neither PyTorch nor TensorFlow;
- the selected inference files total only 43,649,301 bytes (41.63 MiB);
- its online transducer API supports hotword biasing with modified beam search
  when the model's BPE vocabulary artifacts are available.

The downloaded official archive is 127,887,156 bytes (121.96 MiB), SHA-256
`9c559283e8498d3fe95913c79ca1cb454bb26281ac2b102b41306c7d752765d9`.
It contains both FP32 and INT8 weights. The installer retains only the INT8
weights, tokens, and 0.80 MiB of test WAVs; the local installed directory is
about 42.4 MiB. Model files and test audio are Git-ignored.

The runtime was installed in `butters/.venv`: `sherpa-onnx==1.13.4` and its
matching `sherpa-onnx-core==1.13.4` dependency. The environment occupies about
61 MiB. No global package or existing project environment was changed.

Vosk remains another lightweight streaming CPU recognizer, but the sherpa-onnx
transducer has the smaller measured resident footprint and native
endpoint/hotword APIs. Whisper-style systems were not installed because their
windowed/record-then-transcribe design is not this subsystem's streaming target
and would not answer the chosen design question.

Official references:

- [sherpa-onnx PyPI release and ARM64 wheel](https://pypi.org/project/sherpa-onnx/)
- [Linux ARM64/Raspberry Pi installation](https://k2-fsa.github.io/sherpa/onnx/install/linux.html)
- [small online model guidance](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/small-online-models.html)
- [selected 20M model inventory](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html#csukuangfj-sherpa-onnx-streaming-zipformer-en-20m-2023-02-17-english)
- [hotword requirements](https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html)

## Method

Each thread configuration ran in a fresh process. The model loaded once, then
remained idle for five seconds before accelerated recognition. The 1-, 2-, and
3-thread runs each processed three natural WAVs three times: nine recognition
operations and 84.495 seconds of standardized audio. The 4-thread run was
stopped after two repetitions (56.330 seconds) because temperature approached
80 C and performance had already degraded.

The three inputs were 16 kHz mono (6.625 s), 16 kHz mono (16.715 s), and 8 kHz
mono (4.825 s). The last one deliberately exercised `WaveAudioSource` resampling
to 16 kHz. CPU percentage is total process CPU time divided by wall time, so
100% is one fully occupied Pi core and values above 100% are expected for
multithreaded inference. RTF is wall time divided by input audio duration;
lower is better and any value below 1.0 is faster than real time.

## Thread results

| Threads | Init | Loaded RSS | RSS increase | Idle CPU | Active CPU | RTF | Finalize mean / max | Temp before / after |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2.515 s | 107.3 MiB | 91.6 MiB | 0.001% | 99.8% | 0.268 | 21.9 / 65.6 ms | 64.8 / 67.7 C |
| 2 | 2.628 s | 107.6 MiB | 91.9 MiB | 2.07% | 354.6% | 0.230 | 17.0 / 51.6 ms | 65.7 / 74.5 C |
| 3 | 2.713 s | 107.7 MiB | 92.0 MiB | 4.30% | 370.0% | 0.317 | 27.0 / 91.3 ms | 69.6 / 77.9 C |
| 4 | 2.665 s | 108.6 MiB | 92.9 MiB | 7.07% | 377.4% | 0.455 | 28.4 / 101.6 ms | 70.1 / 79.9 C |

Peak process RSS was 112.9-113.0 MiB. One thread is the selected default. It is
comfortably faster than real time while leaving three cores available and is
the only tested setting with effectively zero CPU use while the model is idle.
Two threads improve accelerated RTF by only about 14% while ONNX Runtime uses
nearly all four cores. Three and four threads are slower on this Pi and add
unacceptable thermal/service contention.

The thread count passed to the runtime is not a whole-process core cap. In
particular, the two-thread configuration averaged 354.6% CPU. A later service
should therefore also use systemd/cgroup resource controls, not rely on this
setting alone.

## Endpoint and streaming behavior

The diagnostic emitted changed partial hypotheses throughout each file; the
three files produced 13, 45, and 9 partial updates per recognition. A derived
temporary test WAV appended one second of silence to the first natural sample.
With the configured 600 ms VAD release it finalized with
`endpoint=vad_silence`, not a long fixed recording timeout. Work after the last
accepted frame was approximately 0.1 ms and measured speech-end-to-final
latency was 600.1 ms in that run. The same file was paced in real time and
produced the same partial/final result.

The session starts after two 20 ms active VAD frames and feeds the bounded
0.8-second pre-roll into the new recognizer stream. Audio is then accepted one
20 ms chunk at a time. At VAD or recognizer endpoint it finalizes and resets the
stream while retaining the model. No normal path writes microphone audio to
disk.

## Recognition examples and errors

The natural fixtures are useful pipeline tests, not a representative household
accuracy corpus. Results were deterministic across every repetition.

| Input reference (abridged) | Raw/final result (abridged) | Observation |
| --- | --- | --- |
| `AFTER EARLY NIGHTFALL THE YELLOW LAMPS ... BROTHELS` | `THE YELLOW LAMPS ... THE BRAFFLEL` | Dropped the opening and badly substituted the last word. |
| `GOD AS A DIRECT CONSEQUENCE ... BLESSED SOUL IN HEAVEN` | `AS A DIRECT CONSEQUENCE ... BLESSED SOUL IN HEAVEN` | Dropped the first word; most of the long utterance was correct. |
| `YET THESE THOUGHTS AFFECTED HESTER PRYNNE ... APPREHENSION` (8 kHz) | `S AFFECTED HESTER PRYNNE ... APPREHE` | Resampling worked, but recognition quality was poor. |

The normalizer retains raw and normalized forms separately. Its deterministic
tests include:

```text
raw:        what is the co two level via m q t t
normalized: what is the CO2 level via MQTT
```

Unknown text is preserved, and aliases match only configured whole phrases.
No domain-command speech corpus was available locally, and no TTS stack or
cloud service was added just to manufacture accuracy results. Terms such as
SEN66, SHT41, Bambu, KR260, and Kria should be expected to be difficult until
they are tested with human speech.

Sherpa hotwords were investigated but not enabled. They require
`modified_beam_search` plus a SentencePiece-generated `bpe.vocab`; this small
model archive contains only `tokens.txt`, not `bpe.model` or `bpe.vocab`.
Treating `tokens.txt` as that different file format would be unsafe. The domain
term list is configured now, but actual contextual-bias testing waits for a
compatible model/artifact rather than silently pretending it is active.

## Stability, memory, swap, and existing services

Nine sequential recognitions on each of the 1-, 2-, and 3-thread processes and
six on the 4-thread process produced identical results per input. No stale
partials crossed utterances, no stream reset failed, no crash occurred, and RSS
remained approximately 107-110 MiB after load/recognition with a 113 MiB peak.

Host zram remained at 25.5 MiB used. `/proc/vmstat` counters stayed at
`pswpin=23` and `pswpout=5471` through every run, so recognition caused no swap
I/O. Host available memory during the test processes was approximately
1.88-1.98 GiB; this session also included Codex and filesystem cache, so the
pre-Butters baseline remains the capacity reference.

The high-thread sweep reached 79.85 C, but an elevated read-only firmware check
afterward reported `throttled=0x0`. One-thread operation ended at 67.7 C in its
repeatability run. No high-thread stress was continued after the four-thread
measurement.

Before, during, and after the recommended one-thread active workload, all seven
dashboard/API probes returned HTTP 200. Representative heavier calls changed
from 0.76 to 1.20 s (`api/latest`) and 1.11 to 1.36 s (one-hour readings). After
the run, Mosquitto, InfluxDB, Grafana, the sensor bridge, dashboard, export
worker, Docker, containerd, and Tailscale were all active, with zero failed
systemd units. InfluxDB and Grafana health endpoints returned HTTP 200; Home
Assistant responded in 0.006 s with its expected unauthenticated HTTP 401.

## Suitability

The selected model is suitable to keep resident for development and likely for
the eventual service: roughly 110 MiB process RSS, effectively zero one-thread
idle CPU, no observed paging, and RTF 0.268 under accelerated repeated load.
That conclusion remains conditional on microphone/noise accuracy. The future
service should use one recognizer thread, positive nice value and lower CPU
weight, and explicit memory controls derived from the whole-pipeline peak. No
permanent service is installed in this milestone.
