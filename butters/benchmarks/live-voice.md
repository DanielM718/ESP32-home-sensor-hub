# Live voice frontend benchmark

Measured on the repository Raspberry Pi 4 on 2026-08-10. Unlike Milestones 1
and 2, the intended webcam was attached and ALSA capture was exercised on the
host. Temporary diagnostic WAVs remain under `/tmp`; none are source assets.

## Hardware identity and native format

| Item | Observed value |
| --- | --- |
| USB identity | `09da:2695`, A4Tech FHD 1080P PC Camera |
| USB strings | Sonix Technology Co., Ltd.; serial `SN0001` |
| ALSA card/device | `Camera`, capture device 0, USB Audio |
| Stable capture PCM | `hw:CARD=Camera,DEV=0` |
| Stable UVC warm-up path | `/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._A4tech_FHD_1080P_PC_Camera_SN0001-video-index0` |
| Native encoding | S16_LE, 16-bit, mono |
| Native rates | 8000, 11025, 16000, 22050, 24000, 44100, 48000 Hz |
| Butters request | 16000 Hz, mono, S16_LE, 20 ms / 320-sample frames |
| Conversion | None; the internal format is native |
| Mixer | Mic enabled, 239/256 (93%, -6.31 dB) |

The numeric ALSA card was 3 during discovery but is intentionally absent from
configuration. `CARD=Camera` is the stable ALSA ID. `/dev/snd/by-id` points to
the card control device, not a capture PCM, so it cannot replace the ALSA PCM
name.

### Webcam initialization quirk

Direct `hw` and converted `plughw` opens both negotiated successfully but their
first data read initially returned `Input/output error`. The same USB device's
default 1080p UVC stream returned a protocol error. One discarded 640x480 MJPEG
frame initialized the webcam successfully, after which native audio capture
worked. USB runtime power stayed active with zero suspend time, no process held
the PCM, and no related kernel disconnect was logged; this was not an ALSA
format or permission failure.

`AlsaAudioSource` therefore has an explicit optional `video_warmup_device`.
This machine config selects the stable by-ID UVC node and streams one discarded
low-bandwidth frame before each ALSA open. It does not retain video and normal
audio devices leave the option blank. Five successive warm-up/open/capture/
close cycles each delivered exactly 100 frames / 64,000 bytes (2.0 seconds)
with no errors, overruns, drops, or device lock.

Late in the extended validation session, after those successful captures and
resource runs, the audio endpoint entered a persistent `Input/output error`
state. Both direct 16 kHz and native 48 kHz reads failed, although ALSA still
enumerated the stopped endpoint and no process owned it. Verified five-frame
UVC streaming, a fresh UVC frame before each ALSA attempt, and keeping video
active concurrently did not clear the condition. This is now a documented
webcam reliability issue and requires physical unplug/replug validation; no
privileged USB reset was attempted because the neighboring hub also carries
home-sensor serial adapters.

## Real microphone signal validation

The hardware check wrote valid PCM WAVs at exactly 16 kHz, mono, 16-bit. The
30-second file contained 480,000 frames / 960,044 bytes. Capture reported zero
overruns and zero dropped frames. After excluding the first second:

| Measurement | Result |
| --- | ---: |
| 20 ms dBFS, 10th percentile | -43.1 dBFS |
| Median | -41.7 dBFS |
| 90th percentile | -40.4 dBFS |
| 99th percentile | -38.7 dBFS |
| Maximum post-start sample magnitude | 1,679 / 32,767 |
| Clipped samples in the whole file | 0 |

Opening this webcam creates a decaying approximately 0.4-second transient; the
first 100 ms block was -10.7 dBFS and peaked at 14,240. It did not clip. A real
wake detector needs speech history before it can trigger, so the transient has
settled before normal command listening. A deliberately forced immediate wake
did start an empty VAD session; repeating it after one second did not.

The original -42 dBFS diagnostic VAD threshold stayed active on the measured
room floor. The machine-local command threshold is now -34 dBFS. This leaves
approximately 4.7 dB above the observed 99th-percentile background while still
requiring human-speech calibration. No unmistakable human speech arrived in
either the 12- or 30-second requested recording window, so audible speech
quality, speech level, and user distance are not claimed from those files.

## Wake-word selection and model

Selected engine: `sherpa-onnx==1.13.4` `KeywordSpotter` with
`sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20`, INT8 chunk-8 encoder/joiner and
FP32 decoder, one thread. The model is a streaming open-vocabulary transducer.
Its lexicon provides `HH EY1` for `HEY` and `B AH1 T ER0 Z` for `BUTTERS`.
The tracked keyword file is:

```text
HH EY1 B AH1 T ER0 Z @HEY_BUTTERS
```

Default boost is 1.5 and trigger threshold is 0.25. That is the model's
documented initial threshold and was retained because the limited negative
room test produced no false activation. Threshold tuning awaits positive human
trials. sherpa provides token timestamps but no winning-path confidence value;
the CLI reports `confidence=n/a` and never treats the configured threshold as
a measured confidence.

The official archive download was 32,885,699 bytes, SHA-256
`68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6`.
The active chunk-8 inference files plus tokens total 5,449,043 bytes. The clean
installer retains both chunk-8/chunk-16 INT8 variants, English lexicon, and
small natural fixtures, about 15.4 MiB. The current manually inspected model
directory includes all FP32 variants and is 40,742,732 bytes; it is Git-ignored.

openWakeWord was not installed. It has no included “Hey Butters” model, and its
documented custom path requires thousands of positives and large negative data
in addition to another inference dependency stack. sherpa supplied the custom
phrase without training and reused the existing Pi-tested runtime. Porcupine
was not selected because it adds an account, AccessKey, and licensing
dependency.

## Wake behavior and state-machine validation

Practical negative observations at threshold 0.25:

- 66.26 seconds / 3,313 chunks of a repeated natural LibriSpeech passage:
  zero false wakes;
- 300 seconds across several live room/capture sessions: zero false wakes;
- all live sessions: zero overruns and drops.

This is a small practical check, not a false-accept-rate estimate. During the
120-second positive test window no deliberate wake speech reached the captured
stream, so zero detections in that window are not counted as five misses.
“Hey Butters” positive accuracy, varied distance/volume, and detection latency
remain pending.

The model and full state machine were positively exercised with the model
archive's natural `LIGHT UP` example. The keyword was found with an estimated
400 ms token-end-to-detection delay. The controller then produced:

```text
WAKE LIGHT UP
LISTENING
PARTIAL D QUAR
PARTIAL D QUARTER OF
PARTIAL D QUARTER OF THE B
PARTIAL D QUARTER OF THE BRAFFL
FINAL RAW D QUARTER OF THE BRAFFLS
READY
```

That test demonstrates real KWS and STT models, incremental chunks, final
normalization, state reset, and return to ready. It is file-fed natural speech,
not webcam/user accuracy. The transcript inherited the small STT model's
pronunciation errors and clipped context because the keyword occurs mid-
sentence.

The acknowledgement is a generated 110 ms two-note local PCM chime played by
an asynchronous `aplay` child; no TTS is involved. Launch measured 0.93-1.67 ms.
A 120 ms VAD guard corresponds to the chime length while frames remain bounded
in memory. A real-mic forced wake after startup produced the chime, no speech
activation, a four-second no-speech timeout, and automatic return to ready.

The live owner keeps ALSA open across wake, listening, finalization, and idle.
The 0.8-second wake ring remains exactly 40 x 20 ms frames; a separate
0.3-second command ring retains only the token-timestamp-estimated post-keyword
tail. Neither normal path writes audio to disk or MQTT.

## Resource measurements

CPU percentages below use process CPU time, where 100% means one complete Pi
core. Divide by four for approximate whole-Pi capacity. RSS is the one Butters
process and shared pages make sums with other services inappropriate.

| Phase | Butters RSS | Process CPU | MemAvailable | zram used | Temperature |
| --- | ---: | ---: | ---: | ---: | ---: |
| A: Butters off, 30 s host sample | 0 | n/a; host 94.3% mean idle | 1,953-1,961 MiB | 25.5 MiB | 59.4 C |
| KWS only, real microphone | 61-66 MiB | 20.9-22.1% (5.2-5.5% of Pi) | 1,888-1,907 MiB | 25.2 MiB | 59.9-63.8 C |
| B: full process resident, live mic + KWS wait | 117-119 MiB | 21.7% (5.4% of Pi) | 1,829-1,835 MiB | 25.2 MiB | 60.4 C |
| C: full process, live mic + active paced STT | 125-126 MiB | STT 23.3%; process 19.9% over run | 1,847-1,850 MiB | 26.8 MiB | 61.8-62.3 C |

Phase C used a clearly labelled forced diagnostic wake and a temporary -45
dBFS gate so real microphone noise kept the recognizer active. It produced an
empty transcript and a live-audio RTF of 0.236; this is a resource measurement,
not recognition accuracy. The native recognizer endpoint ended the noise
utterance after 2.66 seconds. The prior accelerated STT benchmark remains the
worst active CPU reference: 99.8% of one core at RTF 0.268.

At the start of this work zram held 25.5 MiB and `/proc/vmstat` was
`pswpin=24`, `pswpout=5471`. Passive KWS did not change those paging counters.
Across later repeated model loads and active diagnostics zram rose to 26.8 MiB
and `pswpout` to 6175 (704 additional pages, about 2.75 MiB); `pswpin` remained
24. That interval included multiple short-lived model processes, so it cannot
be attributed solely to recognition. Available RAM remained approximately
1.8 GiB and there was no swap-in stall. A production residency decision should
still prefer one combined long-running process over repeated model loads.

A final Butters-off audit much later in the extended Codex and hardware-debug
session showed 71,827,456 bytes (68.5 MiB) zram used, `pswpin=45`,
`pswpout=16725`, and about 1.9 GiB RAM available. Relative to the initial
baseline, that is 21 pages swapped in and 11,254 pages swapped out. The interval
included two Codex processes using roughly 469 MiB RSS, repeated model process
startup, test suites, and webcam diagnostics, so the total is not assigned to
Butters. The tiny swap-in count and ample available RAM do not indicate active
memory thrashing, but longer resident-service observation is still warranted.

Firmware status after the live and active runs was `throttled=0x0`; temperature
was 61.3 C after those runs and 60.3 C at the later final audit. No thermal
throttling was observed.

## Existing-service health

Before testing, all dashboard probes returned HTTP 200; representative calls
were 1.19 s for `api/latest`, 0.63 s for nodes, and 1.10 s for one-hour
readings. After the live workload, all seven remained HTTP 200; the same calls
were 0.72 s, 0.53 s, and 1.30 s. Grafana, InfluxDB, and Home Assistant health
requests returned HTTP 200 in approximately 0.002 seconds.

Mosquitto, InfluxDB, Grafana, home-sensor bridge, dashboard, export worker,
Docker, containerd, and Tailscale were all active after testing, with zero
failed systemd units. No existing unit, container, port, priority, dashboard,
or data was changed. No Butters service was installed.

## Conclusions and pending human validation

Earlier capture, native formatting, bounded routing, passive wake cost,
acknowledgement, no-speech recovery, model reset, and full file-fed KWS/STT are
verified on this Pi. The resource cost is acceptable for continued development
and the combined models appear suitable to remain resident while this Codex
session is consuming additional memory. The late persistent webcam `EIO` means
the frontend is not ready for unattended operation until replug/recovery and
positive human speech are validated.

The following cannot be marked complete until a person speaks into the active
webcam stream:

1. deliberate “Hey Butters” successes at varied volume/distance;
2. missed-wake and false-wake practical counts with actual conversation;
3. real microphone partial/final command transcripts for the requested phrase
   list;
4. project vocabulary error examples from this user's voice;
5. human-speech end-of-speech latency.
6. recovery after physically replugging the currently wedged webcam.

Run `./butters/scripts/butters-wake --max-detections 5` for the focused wake
trial, then `./butters/scripts/butters-live --cycles 5` for complete repeated
interactions. Those commands use the already-configured stable devices and do
not retain audio.
