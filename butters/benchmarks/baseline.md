# Raspberry Pi baseline for Butters

Measured on 2026-08-10 from 17:38:27 through 17:39:22 EDT. The sample was
taken before adding or running any AI model. Existing monitoring and automation
services were left untouched.

## Host

| Item | Measured value |
| --- | --- |
| Hardware | Raspberry Pi 4 Model B Rev 1.2, revision `c03112` |
| CPU | 4 Cortex-A72 cores, 600-1500 MHz |
| Architecture | `aarch64`, 64-bit userspace |
| OS | Debian GNU/Linux 13.4 (trixie), kernel `6.12.75+rpt-rpi-v8` |
| RAM visible to Linux | 3,887,804 KiB (3.708 GiB) |
| Root filesystem | 117 GiB total, 102 GiB available, 10% used |
| Uptime at measurement | 23 days, 20 hours |
| Python | 3.13.5 |

The repository was clean on `main` and matched `origin/main`. The existing
backend virtual environment is `server/backend/.venv`; it also uses Python
3.13.5. Butters does not reuse or modify that environment.

## One-minute CPU, memory, swap, and thermal sample

Twelve memory/load/temperature readings were taken five seconds apart. CPU and
paging activity were sampled with `vmstat` over the same interval.

| Metric | Result |
| --- | --- |
| 1-minute load average | 0.79 down to 0.34 during the sample |
| 5-minute load average | 0.49 down to 0.41 |
| 15-minute load average | 0.21 down to 0.19 |
| CPU idle, interval samples | 93-97%, mean approximately 95.2% |
| CPU busy, interval samples | 3-7%, mean approximately 4.8% |
| Available RAM | 2,312,088-2,326,660 KiB (2.205-2.219 GiB) |
| Mean available RAM | 2,318,032 KiB (2.211 GiB) |
| Used RAM (`free` initial snapshot) | 1,608,830,976 bytes (1.498 GiB) |
| Free RAM | approximately 117-131 MiB; this is not the capacity figure |
| Page cache plus reclaimable memory | approximately 2.1 GiB |
| Swap configured | 2,097,148 KiB zram (2.0 GiB), `zstd`, priority 100 |
| Swap used | 26,112 KiB (25.5 MiB), unchanged throughout the sample |
| Swap-in / swap-out | zero in every interval sample |
| CPU temperature | 59.887-61.348 C, mean 60.536 C |
| Firmware throttling status | `throttled=0x0` |

Available memory, rather than the small `MemFree` value, is the relevant
headroom measurement because Linux can reclaim much of its cache. Swap is
already nonzero under normal operation, but it was cold/stable: no paging took
place during the sample. Later model benchmarks should watch both
`MemAvailable` and `vmstat` `si`/`so`; a model that causes sustained paging is
not acceptable even if it technically starts.

The audit itself had Codex processes resident (roughly 400 MiB aggregate RSS)
and generated a little CPU activity. This makes the memory result conservative,
but it is not a pristine unattended-idle measurement. Repeat this baseline from
a normal login after Codex exits before making final resident-model limits.

## Existing services and consumers

All 23 observed system services were running and `systemctl --failed` reported
zero failed services. Important services included Mosquitto, InfluxDB, Grafana,
the bridge, dashboard, export worker, Docker/containerd, Home Assistant,
Tailscale, SSH, and network services.

Approximate host RSS values are snapshots, not hard memory limits. Shared pages
can make summed RSS misleading, particularly for the Gunicorn workers.

| Process/service | Approximate RSS | CPU snapshot / long-running average |
| --- | ---: | ---: |
| Home Assistant container process | 481,488 KiB | 1.5% |
| Grafana server | 311,540 KiB | 1.6% |
| InfluxDB | 196,760 KiB | 0.6% |
| Dashboard: Gunicorn master + 2 workers | 141,084 KiB summed | below 0.1% at snapshot |
| Tailscale | 88,396 KiB | 0.2% |
| Docker daemon | 77,524 KiB | below 0.1% |
| MQTT-to-InfluxDB bridge | 45,876 KiB | 0.1% |
| containerd | 44,680 KiB | below 0.1% |
| Export worker | 42,788 KiB | 0.5% during snapshot |
| Home Assistant MQTT discovery container | 30,452 KiB | below 0.1% |
| Mosquitto | 8,928 KiB | below 0.1% |

The user account cannot read `/var/run/docker.sock`, and non-interactive sudo
requires a password. Consequently, `docker ps` and `docker stats` could not be
captured directly. Two active Docker cgroups were observed from the host:

- container `29331d01...`, process `python3 -m homeassistant --config /config`;
- container `bb747d07...`, process `python -m home_sensor_discovery.main`.

These correspond to the `homeassistant` and `home-sensor-ha-discovery` services
declared by `home-assistant/compose.yaml`. No Docker configuration was changed.

## Relevant listeners

| Port | Service/purpose |
| ---: | --- |
| 22/tcp | SSH |
| 1883/tcp | Mosquitto MQTT |
| 3000/tcp | Grafana |
| 8080/tcp | Home Sensor dashboard/API |
| 8086/tcp | InfluxDB |
| 8123/tcp | Home Assistant |
| 41641/udp | Tailscale |

Butters did not bind a port in this milestone.

## Audio hardware at baseline

`lsusb` showed only the Pi root hubs, a VIA Labs hub, a Silicon Labs CP210x
serial adapter, and an FTDI FT4232H serial/FIFO adapter. The intended webcam was
not connected, as expected.

ALSA exposed three playback-only cards:

- card 0: `bcm2835 Headphones`;
- card 1: `vc4-hdmi-0`;
- card 2: `vc4-hdmi-1`.

There were no capture PCM nodes. `arecord --list-devices` reported no capture
hardware and `arecord --list-pcms` contained only the `null` PCM. Therefore no
webcam device name, capture identifier, native sample format, sample rate, or
channel count can be truthfully recorded yet. The discovery and probe procedure
in `butters/README.md` will collect those facts when the webcam is attached.

## Preliminary workload headroom

These are capacity estimates, not inference benchmarks. Runtime allocations,
context/KV cache, model format, thread count, and simultaneous STT/TTS use can
matter as much as the quantized weight file.

| Workload | Preliminary assessment from this baseline |
| --- | --- |
| Lightweight wake word | Comfortable. Expected to be a small fraction of one core; benchmark false activations and CPU continuously. |
| Small streaming STT | Plausible. Start with sherpa-onnx's English 20M int8 Zipformer and measure real-time factor, peak RSS, and service latency. |
| Piper-class lightweight TTS | Plausible on demand. Keep the voice loaded only if its measured resident cost and latency justify it. |
| Approximately 350M Q4 LLM | Likely enough RAM; probably the safest resident-model tier, but capability for constrained routing may be marginal. |
| Approximately 700M Q4 LLM | Likely fits by RAM in isolation. Residency alongside streaming STT/TTS is conditional on peak-RSS and latency tests. |
| Approximately 1.2B Q4 LLM | It should fit by raw capacity, but is not yet a comfortable always-resident recommendation with only 2.21 GiB measured available and pre-existing swap use. Test on demand first. |

A rough planning allowance is 0.3-0.5 GiB for a 350M-class Q4 runtime,
0.5-0.8 GiB for a 600-700M-class runtime, and 0.8-1.3 GiB for a 1.23B-class
runtime including modest context overhead. These are intentionally broad. Do
not use them as `MemoryMax` values; measure actual GGUFs through `llama.cpp` on
this Pi and preserve a meaningful reserve for Home Assistant, Grafana,
InfluxDB, filesystem cache, and transient export work.

## Next benchmark requirements

For every future wake-word, STT, TTS, or LLM candidate, record:

- cold-start and warm-start time;
- idle and active CPU by process and total host CPU;
- peak RSS/PSS and minimum host `MemAvailable`;
- zram use and any `vmstat` swap-in/swap-out;
- temperature and throttling during a sustained run;
- effect on MQTT ingestion, dashboard/API latency, InfluxDB queries, Home
  Assistant responsiveness, and export jobs;
- real-time factor for STT/TTS and tokens/second plus first-token latency for
  LLMs;
- accuracy on a fixed home-command and restricted-tool-selection test set.
