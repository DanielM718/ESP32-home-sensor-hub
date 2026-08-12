# Bambu Lab X2D read-only integration

Research and implementation date: **2026-08-11**. This document deliberately
separates upstream capability, unit-tested behavior, local inspection, and live
printer verification. No printer command path exists in this design.

## Status at this revision

| Item | Status | Evidence |
|---|---|---|
| Home Assistant runtime | Locally inspected | Running; version `2026.7.3` returned by its WebSocket handshake. |
| HA container/config | Partially inspected | The `python3 -m homeassistant --config /config` process is running. Docker socket and `/opt/home-assistant/config` require privileges unavailable in this session, so container name and config entries were not read. |
| HACS installed | Awaiting authenticated discovery | Not inferable from an unauthenticated endpoint. |
| HA Bambu integration installed/configured | Awaiting authenticated discovery | No HA token was already available. |
| Actual X2D entities/attributes/update rate | Awaiting authenticated discovery | Entity IDs have not been guessed or hard-coded. |
| Actual X2D firmware | Not observed | The official download page lists current available X2D firmware, but that is not evidence of the printer's installed version. |
| Normalization, session tracking, API, dashboard, Influx schema, Butters | Implemented and unit-tested | See the test and design sections below. |
| Grafana dashboard | Prepared, not provisioned | Provisioning/restart was intentionally not performed. |
| Live X2D validation | Awaiting authenticated discovery | No print was started and no printer setting was changed. |

## Integration research

The following sources were checked on 2026-08-11.

### Option A: `greghesp/ha-bambulab`

- Current stable release reviewed: [`v2.2.22`](https://github.com/greghesp/ha-bambulab/releases/tag/v2.2.22), published 2026-05-12. Its release notes explicitly add X2D support.
- The tagged [manifest](https://raw.githubusercontent.com/greghesp/ha-bambulab/v2.2.22/custom_components/bambu_lab/manifest.json) identifies a local-push integration and the exact `pybambu` dependency used by that release.
- The tagged [models](https://raw.githubusercontent.com/greghesp/ha-bambulab/v2.2.22/custom_components/bambu_lab/pybambu/models.py) explicitly include the X2D model and dual-nozzle structures. This is stronger evidence than assuming compatibility from X1/P1 support.
- The tagged [constants](https://raw.githubusercontent.com/greghesp/ha-bambulab/v2.2.22/custom_components/bambu_lab/pybambu/const.py) define the upstream print states used by the centralized mapper.
- The upstream [entity reference](https://docs.page/greghesp/ha-bambulab/entities) documents print status/stage, job progress, current and total layers, remaining time, start/end timestamps, temperatures, active tray, and AMS-related data. Actual X2D entity availability still depends on the installed integration, firmware, connection mode, and printer state.
- The upstream [setup guide](https://docs.page/greghesp/ha-bambulab/setup) supports local and cloud-backed configuration. A local IP/access-code path can avoid making this home-sensor adapter cloud-dependent, but the actual unit's allowed mode must be discovered without changing printer settings.

Evidence favors Option A, subject to live entity discovery. It is already shaped
for Home Assistant, explicitly supports X2D in the current release, and avoids a
second Bambu protocol client on the 4 GB Pi.

### Option B: Bambuddy

- Current stable release reviewed: [`v0.2.4.4`](https://github.com/maziggy/bambuddy/releases/tag/v0.2.4.4). It is a critical security release fixing the fail-open authentication bypass identified as `GHSA-6mf4-q26m-47pv` / CVSS 9.8. An older release must not be installed.
- The [project](https://github.com/maziggy/bambuddy) and [supported-printer reference](https://wiki.bambuddy.cool/reference/printers/) explicitly list X2D, dual-nozzle behavior, AMS 2 Pro/AMS HT support, and ARM64/Raspberry Pi deployment.
- Bambuddy documents LAN-only X2D monitoring as read-only without Developer Mode and reserves full control for Developer Mode. This is useful fallback evidence, but was not verified against this printer.
- Its [API](https://wiki.bambuddy.cool/reference/api/) exposes GET status/history data, but the same service also has control-capable POST endpoints. It therefore adds a resident database/web application and a larger control/security surface than Option A. CPU and RSS were not benchmarked because it was not installed.

Bambuddy remains the second choice only if current HA X2D entities prove
insufficient. If used later, it must be `v0.2.4.4` or newer, isolated in Docker,
API-authenticated, and reachable only on LAN/Tailscale. Developer Mode is out of
scope.

### Option C: direct Bambu protocol

No direct MQTT implementation was added. It would duplicate protocol/TLS/auth
churn and create the highest risk of accidentally reaching a request/control
topic. It remains a last resort only after Options A and B fail with the actual
X2D.

### Official firmware and security sources

Bambu Lab's official [X2D firmware download page](https://bambulab.cn/zh-cn/support/firmware-download/x2d)
listed `01.02.00.00` dated 2026-08-06 when researched. That is the latest
available version shown by the page, **not** the observed firmware on this unit.
Bambu's [security whitepaper](https://cdn1.bambulab.com/trust-center/file/bambulab-security-whitepaper-en.pdf)
was also reviewed for the vendor's authentication, encryption, and local/cloud
security model. No authentication or TLS setting was weakened.

## Selected architecture

The selected implementation target is Option A:

```text
Bambu Lab X2D
    -> existing Home Assistant Bambu integration
    -> allow-listed GET-only HA adapter (separate observer process)
    -> normalized PrinterState
       -> SQLite current state + restart-safe PrintSession tracker
       -> environment_live/printer_state (high resolution)
       -> environment/printer_state_5m + print_session (long term)
       -> Flask read-only API and small dashboard status card
       -> Grafana printer/environment dashboard
       -> existing Butters entity/router/SkillRegistry/PolicyValidator
```

The observer is separate from `home-sensor-bridge`. It never subscribes or
publishes to Bambu MQTT, and failure cannot interrupt environmental MQTT
ingestion. The dashboard reads the observer's small SQLite snapshot; Butters
uses the existing dashboard integration boundary rather than accessing HA or
the printer directly.

The observer is intentionally not enabled by the normal install path until
authenticated entity discovery is complete. Its prepared systemd unit has
bounded HTTP timeouts, exponential backoff (up to 300 seconds), a 192 MiB memory
limit, a 25% CPU quota, no capabilities, and write access only to
`/var/lib/home-sensor`.

## Authenticated discovery boundary

The repository provides a GET-only, redacting discovery command. It reads
`/api/config` and `/api/states`, reports loaded HACS/Bambu components, and emits
only likely Bambu candidates. Attribute names containing token, password,
secret, access code, IP address, or serial are removed; sensitive entity states
are redacted.

The minimal user-controlled action is:

1. In Home Assistant, create or supply a long-lived access token suitable for
   read-only inspection. Do not paste it into chat, a repository file, or shell
   history.
2. On the Pi, place it temporarily in `HOME_ASSISTANT_TOKEN` and run from
   `server/backend`:

   ```bash
   .venv/bin/python -m app.printer_discovery
   ```

3. Review the redacted result to determine whether HACS, `bambu_lab`, an X2D,
   AMS/AMS 2 Pro, both nozzles, job metadata, and firmware sensors are actually
   present. Observe timestamps over several minutes to establish update
   frequency; do not start a print.

If the current HA integration is absent, the next manual boundary is installing
the latest compatible `ha-bambulab` through HACS and restarting Home Assistant.
Neither action is authorized by this milestone session. If installation would
require Developer Mode or a printer setting change, stop instead of enabling it.

After discovery, copy `server/config/printer.example.toml` outside Git to
`/etc/home-sensor/printer.toml` and replace only the fields actually observed.
The HA token belongs in root-controlled `/etc/home-sensor/printer.env`:

```text
HOME_ASSISTANT_TOKEN=...
```

Both files should be root-owned and mode `0600`. The token is never returned by
the API or logged. Do not configure an unavailable optional field.

Repository implementation is not deployment. After discovery and a separate
deployment approval, an operator still needs to copy the updated backend,
install `home-sensor-printer-observer.service`, reload systemd, start that new
observer, and restart `home-sensor-dashboard` so the new routes are served.
Butters must be deployed through its existing procedure. Provisioning
`home-sensor-printer.json` with the existing Grafana script restarts Grafana and
therefore also requires explicit approval. None of these steps was performed in
this milestone session.

## Normalized `PrinterState`

`server/backend/app/printer_model.py` is protocol-independent. Required fields
are:

- `printer_id`, `printer_model`
- `online`, `normalized_state`
- `source`, `source_timestamp`, `observed_at`

Optional observed fields remain `None` when unavailable:

- `unavailable_reason`, `current_stage`
- `job_id`, `job_name`, `progress_percent`, `remaining_seconds`
- `current_layer`, `total_layers`, `print_started_at`, `print_finished_at`
- `nozzle_1_temperature/target`, `nozzle_2_temperature/target`
- `bed_temperature/target`, `chamber_temperature`
- `active_tool`, `active_material`, `active_filament`
- `ams_state`, `ams_slot`, `print_source`, `firmware_version`
- per-field `provenance`

Derived `session_active` is true for `preparing`, `printing`, `paused`, and
`finishing`. Derived `printer_is_printing` is deliberately narrower and true
only when the printer is online and normalized state is exactly `printing`.
Heating, preparation, pauses, and cooling are not silently described as active
deposition.

### Central state mapping

| Normalized state | Accepted upstream values |
|---|---|
| `offline` | forced whenever observed availability is false; raw `offline` |
| `idle` | `idle`, `ready` |
| `preparing` | `init`, `initializing`, `prepare`, `preparing`, `slicing` |
| `printing` | `running`, `printing` |
| `paused` | `pause`, `paused` |
| `finishing` | `finishing`, `cooling` |
| `completed` | `finish`, `finished`, `complete`, `completed` |
| `failed` | `failed`, `error` |
| `cancelled` | `cancel`, `canceled`, `cancelled`, `stopped` |
| `unknown` | `unknown` and every unmapped future value |

Raw strings occur only in `printer_adapter.py`; unmapped firmware values remain
`unknown` rather than being guessed.

### Material provenance

- A specifically mapped active-job/material entity is `observed`.
- Material read from attributes of the specifically mapped **active AMS tray**
  is `inferred_active_ams_tray`.
- General AMS inventory is never equated with printed material.
- Missing or contradictory information is `unknown`.
- If the active material changes during one session, the session material is
  stored as `multiple` with `unknown` provenance. It is not mislabeled as the
  last spool seen.

This foundation permits material comparisons, but comparisons must filter for
acceptable provenance. Filtered/unfiltered status is not currently observable
and therefore is not invented.

## `PrintSession` lifecycle

SQLite stores current state, sessions, and a small terminal-transition tracker.
The file is mode `0600`; SQLite is an operational restart checkpoint, while
InfluxDB remains the historical analytics store.

- Any `preparing`, `printing`, `paused`, or `finishing` observation creates or
  resumes one active session.
- Pause/resume, duplicate upstream updates, HA restarts, observer restarts, and
  temporary printer offline/unknown states do not mint another session.
- An active session remains open while upstream is offline or unknown. No
  result is fabricated.
- A different non-empty stable job ID closes the prior active session as
  `unknown` and opens a new one.
- Terminal states require two consecutive confirmations by default, reducing
  state-flap errors. A terminal result followed by idle retains the observed
  completed/failed/cancelled result.
- Idle reached without an explicit terminal result closes the session as
  `unknown`, not cancelled.
- A repeated filename after a closed session receives a new UUID session ID.
  Filename alone is never a key.
- Reprocessing the same observation is idempotent. The database enforces one
  active session per printer.
- If the observer first reconnects during an already-active print and has no
  persisted session, it reconstructs one using an observed print-start
  timestamp when valid, otherwise the first observation timestamp with inferred
  provenance.

## InfluxDB schema

No retention policy is changed.

| Bucket/measurement | Purpose | Tags | Important fields |
|---|---|---|---|
| `environment_live/printer_state` | One high-resolution observation per poll; existing 72-hour live retention | `printer_id`, `printer_model`, `source` | state flags, progress, layers, temperatures, job/material text, provenance, source timestamp |
| `environment/printer_state_5m` | State changes plus a maximum five-minute sample interval | `printer_id`, `printer_model`, `source` | same normalized fields |
| `environment/print_session` | Idempotent start/update/final session record | `printer_id`, `source` | `session_id`, job ID/name, start/end, duration, result, material/provenance, tool/AMS slot |

Job IDs, filenames, material labels, and session UUIDs are fields, never tags.
This bounds series cardinality. Session updates reuse printer/source/start time,
so Influx upserts the logical record.

## SEN66 environmental correlation

The analysis query reads `air_quality_reading` for configured location
`printer_room` from the live 72-hour bucket. Its default windows are:

- baseline: 30 minutes before session start
- print: session start through session end (or now for an active session)
- recovery: 120 minutes after session end

All windows are configurable. For CO2, PM1, PM2.5, PM4, PM10, VOC index, NOx
index, temperature, and humidity, the foundation calculates baseline mean,
print mean, print peak, post-print mean, and print-minus-baseline change. VOC
recovery is the first of three consecutive post-print samples no more than the
baseline plus `max(5 index points, 10%)`.

Raw SEN66 data expires after 72 hours. The API reports `raw_samples_expired`
rather than substituting lower-resolution data with different semantics. The
model supports last-print questions now: it prefers the newest finished session
while another print is active, and uses the active session only when no finished
history exists. Durable per-session aggregate material comparisons are a
recommended next milestone.

Every API and Butters result describes an **observational association**. A
change during a print does not alone establish that the printer caused it.

## API, dashboard, Grafana, and Butters

Read-only Flask endpoints:

- `GET /api/printer`
- `GET /api/printer/sessions?limit=20` (`1..100`)
- `GET /api/printer/environment-summary`

No POST/PUT/PATCH/DELETE printer route exists. The small dashboard status card
refreshes separately, so a printer error cannot reject or delay sensor refresh.

`server/config/grafana/dashboards/home-sensor-printer.json` prepares a state
timeline, print start/end annotations, progress, printer temperatures,
printer-room PM2.5/VOC/NOx overlay, and session table. It has not been copied to
the running Grafana instance; the existing provisioning script would restart
Grafana, which was outside this session's authorization.

Butters reuses its normal `EntityRegistry`, deterministic router,
`SkillRegistry`, response formatter, and authoritative `PolicyValidator`. Entity
`x2d` has aliases `printer`, `3d printer`, `X2D`, `Bambu`, and `Bambu printer`.
Longest-alias resolution preserves `printer room` as the SEN66 entity. Registered
printer skills are read-only:

- `get_printer_status`
- `get_current_print`
- `get_printer_temperatures`
- `get_print_environment_summary`

There is no printer control skill. Generic sensor skills reject a printer
entity, unknown printer IDs are denied, and control verbs are rejected before
routing.

## Failure and security properties

- The observer is a non-critical process, not a code path in the MQTT bridge.
- HA reads use only bounded HTTP GET requests with a default 3-second timeout
  and bounded response size.
- Backoff doubles after failures and caps at five minutes.
- Offline/malformed/unreachable state is explicit and timestamped.
- Influx write failure is caught within the observer and cannot affect SQLite,
  the sensor bridge, dashboard sensor queries, HA, or Butters sensor skills.
- Only explicitly configured HA entity IDs are retained in normalized state.
- HA tokens stay outside Git in a restricted environment file. Access codes,
  cloud credentials, and printer secrets are neither required by nor exposed to
  the generic model/API/assistant.
- Direct Bambu MQTT request topics and all control operations are absent.
- The architecture does not require a public listener or router/Tailscale rule.

## Known limitations and next step

Actual HACS status, installed Bambu integration version, X2D entity IDs,
attributes, update cadence, AMS 2 Pro representation, both nozzle entities,
current job fields, connection mode, and unit firmware remain unverified until
authenticated HA discovery. No field should be configured before that evidence
exists.

Once idle-state live validation succeeds, the recommended next milestone is
durable per-session environmental aggregates (including material/provenance and
explicit ventilation/filter labels) so comparisons remain available after the
72-hour raw tier expires. Printer control should remain a separate, explicitly
authorized project, not an extension of this read-only milestone.
