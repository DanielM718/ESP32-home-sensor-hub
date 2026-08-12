# Deterministic skills and local TTS benchmark

> STT figures in this historical pipeline benchmark used the former 20M model.
> Current human/STT selection is documented in
> [human-voice-semantic-endpoint.md](human-voice-semantic-endpoint.md).

Measured on the repository Raspberry Pi 4 on 2026-08-11. The A4Tech webcam
remained physically connected but its USB audio endpoint was already in the
persistent `EIO` state documented by Milestone 3. This milestone did not open,
probe, reset, or otherwise disturb that endpoint. Inputs were direct text and
temporary WAV files under `/tmp`.

## Integration selection

Repository and deployed-service inspection found that the existing dashboard
`GET /api/latest` representation is the safest available current-state source:

- it already maps the MQTT/InfluxDB schema into current environment and SEN66
  records;
- it applies node status, age/freshness, field availability, battery validity,
  and the repository's air-quality policy;
- it requires no duplicate MQTT, InfluxDB, or Home Assistant credentials in
  Butters;
- it avoids inventing a second sensor representation.

The deployed backend environment and credentials are service-owned and not
readable by the normal development account. Butters therefore uses a narrow
localhost HTTP adapter with a four-second deadline, 2 MiB response limit, typed
parsing, and five-second process-local cache. This is intentionally read-only.
It does mean a cold one-shot CLI call pays the dashboard's latest-query cost;
the persistent assistant normally amortizes it.

The reviewed entity map is `office`/air quality as `printer_room`, and
environment nodes 1, 2, and 3 as filament boxes one, two, and three. The
dashboard currently exposes SEN66 node 100 behind the `office` air-quality
record. Unknown sources are not automatically added to the voice allow-list.

Server health uses only `/proc`, fixed sysfs paths, `shutil.disk_usage`, a fixed
`vcgencmd get_throttled`, and one fixed `systemctl is-active` invocation over
nine compiled unit names. No caller can provide a command, path, or unit.

## Real read-only corpus

`./butters/scripts/benchmark-skills` ran 15 phrases through normalization,
routing, policy, adapters, structured results, and response formatting in one
process. The complete corpus took 1.418 seconds wall / 0.113 seconds process CPU
and ended at 26.1 MiB RSS.

| Request (abridged) | Route/result | Measured latency |
| --- | --- | ---: |
| `what is the CO2 level` | `get_sensor_value`; 684 ppm | 1,354.2 ms cold |
| `printer room temperature` | 27.7 C | 3.9 ms cached |
| `what's the pm two point five` | 11.4 micrograms/m3 | 3.2 ms cached |
| `how humid is container 3` | stale/unavailable, not a stale value | 3.2 ms cached |
| `which ... highest humidity` | box two, 31%; box three excluded | 0.6 ms cached |
| `box one battery voltage` | 3.442 V | 3.1 ms cached |
| `are all sensors reporting` | 3/4; box three stale | 0.5 ms cached |
| `printer room air quality` | structured dashboard summary/readings | 2.2 ms cached |
| `server status` | 9/9 fixed services active | 31.8 ms |
| unqualified `humidity` | clarification, no skill call | 3.2 ms |
| `turn off the printer exhaust` | unsupported read-only response | 0.2 ms |

The values above are a point-in-time integration observation, not static test
expectations. Other successful corpus metrics were printer-room humidity 57%,
VOC index 35, and NOx index 4. The air-quality response reported the existing
dashboard category as dashboard context and did not invent a health claim.

Warm sensor-query latency includes routing and a cached typed snapshot; cold
latency is dominated by the existing dashboard latest query. No request was
sent through expensive historical endpoints, and no write occurred.

## TTS selection and model inventory

Selected runtime: existing isolated `sherpa-onnx==1.13.4` ARM64 wheel.
Selected voice: `vits-piper-en_US-kathleen-low`, one-speaker U.S. English,
16 kHz, low quality. It is Piper-compatible VITS but is loaded through sherpa,
so no second Python/runtime package was installed. The model card identifies
the Kathleen dataset as CC0.

| Artifact | Size |
| --- | ---: |
| Official `.tar.bz2` download | 67,118,360 bytes (64.01 MiB) |
| ONNX weights | 63,052,430 bytes (60.13 MiB) |
| Installed directory including eSpeak data | 81,049,294 bytes (77.29 MiB) |

Archive SHA-256:
`3dd0adaf077e19de32876608c3ac3d4a0a46a6f06310cc7a5633b3b47d762cde`.
The downloader pins that value. Voice binaries are Git-ignored.

Official references:

- [sherpa Kathleen model page](https://k2-fsa.github.io/sherpa/onnx/tts/all/English/vits-piper-en_US-kathleen-low.html)
- [Piper voice repository](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/kathleen/low)
- [maintained OHF Piper runtime](https://github.com/OHF-Voice/piper1-gpl)

The original Piper repository is archived and points to the maintained OHF
project. The OHF runtime was not installed because sherpa already supports this
model locally. That choice avoids a redundant dependency; it is not a claim
about voice quality or the maintained runtime's relative speed.

## TTS performance

`benchmark-tts` loaded the model once, measured three seconds resident-idle,
then repeatedly synthesized “Printer room carbon dioxide is 742 parts per
million.” CPU percentages use process CPU time, where 100% is one full Pi core.

| Threads | Cold load | Loaded RSS | Active CPU | Per-run wall | RTF | Warm RSS behavior | Temp |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 3.624 s | 119.8 MiB | 97.9% | 2.86-3.03 s | 0.724-0.751 | rose through 3 short runs; 225.8 MiB last observed | 67.2 -> 68.7 C |
| 2 | 3.716 s | 120.0 MiB | 190.9% | 1.80-1.97 s | 0.446-0.493 | stabilized at 182.2 MiB by run 4/5 | 68.2 -> 71.1 C |

Two threads are the current development default. They roughly halve response
generation latency while leaving two Pi cores outside active synthesis. Five
two-thread runs generated 19.9 seconds of audio in 9.38 seconds total. Loaded
idle CPU was 0.0021%; no always-running inference occurred while idle. The
adapter currently returns full-utterance PCM, so first-audio/first-chunk latency
is not available separately from generation latency.

The 2-thread process started at 15.4 MiB RSS, reached 120.0 MiB immediately
after load, warmed to 182.2 MiB, and reported 132.8 MiB after engine close due
to allocator retention. The one-thread short sweep's rising allocator/runtime
footprint did not establish a stable resident value, which is why the longer
two-thread run is the residency reference. No crash occurred, outputs remained
valid, and the two-thread RSS stopped growing over the final three runs.

An explicit output test generated 60,160 frames / 3.760 seconds of valid RIFF
PCM: 16 kHz, one channel, signed 16-bit. No physical playback device was
validated. No query writes TTS audio unless an output path is explicitly
provided.

## WAV/STT -> assistant -> TTS integration

A temporary Kathleen WAV saying “What is the carbon dioxide level?” was fed in
20 ms accelerated chunks through the existing VAD and streaming 20M Zipformer.
It produced partials `ON`, `ON DIO`, `ON DIOXID` and final raw/normalized
`ON DIOXID`. The deterministic router returned unsupported rather than guessing
CO2. It then synthesized that response to a valid temporary WAV.

Measured non-live stages were:

- WAV/STT plus route: 3.690 seconds including STT model initialization;
- response TTS: 3.786 seconds load + 1.611 seconds generation for 3.760 seconds
  audio (RTF 0.429);
- approximate measured compute chain: 9.09 seconds, excluding command/approval
  launch overhead.

This validates identical file/live chunk handling, partial/final delivery, safe
unresolved behavior, response formatting, and local PCM output. It does **not**
complete a successful WAV-to-real-sensor query because recognition was wrong;
therefore no dashboard query was made on that request. A second synthetic
“Humidity in box three” became `'S`, confirming the limitation. Human speech
after webcam replug is required rather than overfitting aliases to these bad
outputs.

## Host resources, swap, thermals, and services

The first one-thread TTS sweep began with 1,878,159,360 bytes available RAM and
85,458,944 bytes zram used. It ended with 1,764,085,760 bytes available and
90,701,824 bytes zram used: about 5 MiB additional swap occupancy. The following
five-run two-thread sweep began and ended at exactly 90,701,824 bytes swap used,
so it caused no further occupancy growth. Because this was a shared development
host, the initial increase is correlated with the run and is not assigned
solely to TTS.

After all Milestone 4 workloads and process exit:

| Metric | Observation |
| --- | ---: |
| Load average | 0.40 / 0.43 / 0.45 |
| RAM available | 1,906,966,528 bytes (1.78 GiB) |
| zram used | 90,701,824 bytes (86.5 MiB of 2 GiB) |
| Pi temperature | 66.705 C |
| Firmware throttle flags | `throttled=0x0` |

The TTS repeat peak was 71.1 C. No thermal throttling occurred.

All nine fixed units—Mosquitto, InfluxDB, Grafana, sensor bridge, dashboard,
export worker, Docker, containerd, and Tailscale—were active during the corpus.
After workload, seven dashboard routes returned HTTP 200 in 0.003-0.009
seconds. Direct unauthenticated checks returned HTTP 200 from InfluxDB in
0.0021 seconds, Grafana in 0.0021 seconds, and Home Assistant in 0.0027 seconds.
No service was restarted or reconfigured, no device state was changed, and no
Butters boot service was installed.

## Residency assessment

The router/skills process is cheap (about 26 MiB RSS and negligible CPU outside
a cold query). TTS is fast enough for concise responses and has zero meaningful
loaded-idle CPU, but its 3.7-second cold load is user-visible and warmed RSS is
about 182 MiB. Keeping it resident is technically feasible with the current
1.78 GiB available-RAM observation, but is not yet automatically preferred.
The next model milestone must measure TTS together with the resident voice
frontend and candidate LLM before choosing on-demand or resident deployment.
