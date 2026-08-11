# Milestone 5 local tool-router benchmark

Date: 2026-08-11  
Host: the deployed Raspberry Pi 4 (`aarch64`), alongside the existing home stack

This report is filled from measurements on this Pi. The webcam microphone is
intentionally outside this milestone because its USB audio endpoint remains in
the previously observed EIO state.

## Candidate selection before installation

The candidates were re-checked against current primary documentation rather
than copied from the earlier architecture notes.

| Candidate | File tested | Expected file size | Runtime/tool format | Why it is in the test |
|---|---|---:|---|---|
| LiquidAI LFM2-700M | `LFM2-700M-Q4_K_M.gguf` | 469 MB (publisher page) | llama.cpp; embedded LFM2 chat template and official Python-like tool-call representation | Small edge-oriented baseline, but not the dedicated Tool checkpoint |
| Qwen3-0.6B | `Qwen3-0.6B-Q4_0.gguf` | 429 MB (file page) | llama.cpp; Qwen3 non-thinking mode and native tools | Apache-2.0 compact comparison; official Qwen GGUF currently publishes Q8, so the 4-bit file comes from the official llama.cpp organization |
| LiquidAI LFM2-1.2B-Tool | `LFM2-1.2B-Tool-Q4_K_M.gguf` | 731 MB (publisher page) | llama.cpp; embedded official LFM2-Tool template, greedy decoding, and Python-like tool calls | Explicitly tuned for concise API/IoT tool use; most important quality candidate |

Primary references:

- [llama.cpp release b10360](https://github.com/ggml-org/llama.cpp/releases/tag/b10360)
- [llama.cpp grammar/JSON-schema documentation](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md)
- [LiquidAI LFM2-700M-GGUF](https://huggingface.co/LiquidAI/LFM2-700M-GGUF)
- [LiquidAI LFM2-1.2B-Tool-GGUF](https://huggingface.co/LiquidAI/LFM2-1.2B-Tool-GGUF)
- [LiquidAI LFM2-1.2B-Tool model card and tool format](https://huggingface.co/LiquidAI/LFM2-1.2B-Tool)
- [Qwen Qwen3-0.6B-GGUF model card](https://huggingface.co/Qwen/Qwen3-0.6B-GGUF)
- [llama.cpp organization Qwen3-0.6B Q4_0 file](https://huggingface.co/ggml-org/Qwen3-0.6B-GGUF/blob/main/Qwen3-0.6B-Q4_0.gguf)

All generation uses temperature zero/greedy tool selection. The llama.cpp
server applies each GGUF's embedded official chat template to native tool
definitions. Butters separately parses the resulting call without `eval` and
passes it through the existing typed, default-deny `PolicyValidator` and
`SkillRegistry`. Grammar/template output is never treated as authorization.

## Results

## Runtime and fixed corpus

The isolated runtime is the official Ubuntu ARM64 llama.cpp **b10360**, build
10360, commit **`48d22e295`**. Its release archive is 13,367,171 bytes with
locally verified SHA-256
`f71958f004a5d6fe04d8b2367660265d2268bf9ae71c3590df4b8d6961e78a89`.
No system package or Python package was changed.

`llm-corpus.json` fixes **120** ground-truth cases before model scoring: ten in
each category A-L (straightforward, unusual word order, colloquial, likely STT
substitutions, entity aliases, metric aliases, ambiguity, unsupported,
adversarial/injection, malformed/nonsense, deterministic bypass, and genuine
fallback). It covers all six read-only skills. More than 50 cases are intended
for model fallback; the rest verify that deterministic matches, explicit
clarifications, and control denials continue to bypass inference.

The full candidate/corpus cross-product was **not run**. The resource guard
tripped during the initial representative probes: zram rose from 86.5 MiB to
1.00 GiB, Qwen and LFM2-Tool could not finish one native proposal inside 120
seconds, and the Pi later recorded a soft-temperature-limit event. Continuing
with hundreds of proposals would have prioritized a benchmark over the home
server. Accuracy fields are therefore marked not available rather than
extrapolated from one request.

## Files, load time, and residency

All hashes matched the official publisher/file-page values before loading.
Cold load is the first observed mmap-backed server load; subsequent cached
loads are not used as the headline value.

| Model | Quantization | Exact GGUF bytes | SHA-256 | Cold load | Resident RSS | Swap observation |
|---|---|---:|---|---:|---:|---|
| LFM2-700M | Q4_K_M | 468,624,320 | `684e8406…be2efa` | 13.86 s | 521,932 KiB (509.7 MiB) | host zram 445 -> 770 MiB on this phase |
| Qwen3-0.6B | Q4_0 | 428,970,080 | `da2572f1…6417d4` | 12.74 s | 714,436 KiB (697.7 MiB) | host zram 785 -> 1,000 MiB on load |
| LFM2-1.2B-Tool | Q4_K_M | 730,894,048 | `3a942f51…af67a7` | 20.34 s | 842,460 KiB (822.7 MiB) | host zram 86.5 -> 445 MiB on this phase |

RSS is larger than weight-file size and is the relevant process observation.
The host still reported 1.8-2.0 GiB `MemAvailable` during individual loads,
but rapidly growing compressed zram means that apparent headroom was not a safe
residency signal. Models were never resident together. Loaded-idle CPU was not
held long enough for a defensible interval measurement after the memory gate
failed.

## Tool proposal probes

The actual prompt was intentionally small: the LFM2-700M server reported
411 tokens for native tools and 425 for constrained JSON, comfortably below
the 2K context. Temperature was zero/greedy. llama.cpp applied the embedded
chat template; the dedicated 1.2B Tool checkpoint was tested only with its
official native tool-call convention.

| Model/mode | Cold proposal | Warm proposal | Prompt speed | Generation speed | Result |
|---|---:|---:|---:|---:|---|
| LFM2-700M native tools | 72.18 s | 19.03 s | cold 7.56; warm suffix 5.44 tok/s | 5.39 / 5.25 tok/s | Produced 96 tokens of explanatory prose, not a call; strict parser rejected it |
| LFM2-700M JSON schema | 63.23 s | 8.53 s | cold 7.62; warm suffix 5.71 tok/s | 5.25 / 4.98 tok/s | Wrong `compare_sensor_metric` proposal with incompatible/missing arguments; existing policy denied it |
| Qwen3-0.6B native tools, non-thinking | >120 s timeout | not attempted | no completed response timing | no completed response timing | No proposal before bounded timeout; worker unloaded |
| LFM2-1.2B-Tool native tools | >60 s timeout after an earlier 12 s guard | not attempted | no completed response timing | no completed response timing | No proposal; residency gate failed before a longer run |

The representative request was “how damp is the third filament container,”
whose ground truth is `get_sensor_value(entity=filament_box_3,
metric=humidity)`. LFM2-700M achieved 0/2 full proposals across the two output
modes: one malformed and one policy-denied. It hallucinated no unknown skill or
unknown enum, but chose the wrong known skill/metric combination. Qwen and the
1.2B Tool model completed zero proposals, so an accuracy percentage would be
misleading.

Consequently, per-model 120-case accuracy, clarification accuracy, adversarial
refusal accuracy, and false-confident-action counts are **N/A (resource/latency
gate abort)**. The tracked harness will calculate exact skill, argument, full,
clarification, unsupported, malformed, hallucinated skill/entity/metric,
policy-denial, deterministic-bypass, and false-confident-action counts if the
same corpus is later run on suitable hardware. The local non-model security
suite did validate malformed outputs, code-like LFM output, multiple calls,
unknown tools, invalid entities/metrics, controls, injection-like text,
timeouts, and worker failure; 103 tests passed.

## Thread-count microbenchmarks

`llama-bench` used a deliberately small 32-token prompt and 8-token generation,
one repetition, one model at a time, and `nice +10`. Values are tokens/second,
not end-to-end proposal accuracy.

| Model | 1 thread pp/tg | 2 threads pp/tg | 3 threads pp/tg | 4 threads pp/tg |
|---|---:|---:|---:|---:|
| LFM2-700M Q4_K_M | 3.81 / 3.03 | 7.49 / 5.71 | **11.25 / 7.19** | 14.37 / 6.56 |
| Qwen3-0.6B Q4_0 | 5.25 / 3.73 | 10.50 / 6.91 | 13.47 / 9.22 | **19.87 / 9.51** |
| LFM2-1.2B-Tool Q4_K_M | 2.44 / 1.94 | 4.68 / 3.67 | **7.18 / 4.78** | 8.66 / 4.51 |

Four threads often improved prompt ingestion but did not consistently improve
generation and would occupy every Pi core. Had a model passed quality and RAM
gates, two threads would have been the initial home-server default pending a
real latency/service comparison. No default is selected now.

Time to first token was not separately exposed by these non-streaming bounded
requests. Total proposal time and prompt/generation phases above are the honest
available measurements. Active process CPU was not independently sampled as a
percentage; the inference tests were configured for the stated thread counts,
and the throughput matrix is reported instead of inventing utilization.

## Host impact and service health

The pre-model snapshot was about 1.8 GiB available, 86.5 MiB zram used, and
64.8 C. During/after the bounded model tests:

- available RAM remained roughly 1.8-2.2 GiB after each worker exited;
- zram occupancy climbed to **1,042 MiB** and did not immediately fall after
  unload (compressed swapped pages remain until touched/reclaimed);
- peak observed temperature was **74.01 C**;
- `get_throttled` changed from `0x0` to **`0x80000`**, recording that a soft
  temperature limit occurred at least once; no current-throttle bit was set at
  the final observation;
- after inference stopped, load was 1.65 / 1.24 / 1.00;
- dashboard health/status/root returned HTTP 200; after the thermal sweep,
  `/api/latest` and `/api/nodes` still returned 200 but took 1.78 s and 0.56 s.

A later recovery audit, with no llama process present, found 2,183,282,688
bytes available and zram down to 900,464,640 bytes (858.8 MiB), but cumulative
`pswpin`/`pswpout` had changed from the pre-milestone 139/21,364 pages to
81,759/294,490 pages. A five-second `vmstat` interval saw no new swap-out but
did see two short swap-in bursts as displaced pages were touched. This was not
mere static swap occupancy. Temperature had cooled to 64.27 C; the historical
`0x80000` flag remains latched until reboot.

No MQTT publication, database write, Home Assistant action, service restart,
USB probe/reset, permanent service, or model traffic over MQTT occurred. The
webcam was never touched. All llama servers were loopback-only, one-slot,
temporary, and `nice +10`, with no llama.cpp runtime tools enabled.

At the final audit all nine protected units were active and no failed unit was
reported. Dashboard health/latest, InfluxDB health, Grafana health, and Home
Assistant root all returned HTTP 200 in 0.003, 0.756, 0.003, 0.109, and 0.004
seconds respectively.

## Combined footprint and decision

Using actual warm observations, a rough same-process planning sum is:

| Combination | Approximate summed RSS |
|---|---:|
| live wake/STT frontend (126 MiB) + LFM2-700M + resident TTS (182 MiB) | 818 MiB |
| live frontend + Qwen3-0.6B + resident TTS | 1,006 MiB |
| live frontend + LFM2-1.2B-Tool + resident TTS | 1,131 MiB |

Summed RSS is conservative and ignores sharing, but the observed zram growth
is stronger evidence: even isolated candidate residency displaced too much of
the existing server workload. On-demand loading is also unattractive because
cold loads were 12.7-20.3 seconds before inference.

**Selection: none acceptable on this Raspberry Pi 4 in the current shared
server workload.** LFM2-700M is the smaller fallback by disk/RSS but failed the
first semantic proposal and remained slow. Qwen used unexpectedly high RSS and
timed out. LFM2-1.2B-Tool is the most task-aligned model, but its 823 MiB RSS,
swap displacement, and native-call timeout disqualify it here. No model should
remain resident, and `assistant.toml` leaves LLM fallback disabled.

The model-neutral fallback code and fixed evaluation harness remain useful for
a future separate Pi/newer edge host or a genuinely smaller tool classifier.
The deterministic read-only assistant continues to operate unchanged when no
LLM worker exists.
