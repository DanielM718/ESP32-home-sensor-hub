# Bambu Lab X2D read-only integration

Verified and implemented: **2026-08-12**. The checked-out repository and the
running Raspberry Pi were inspected without restarting or changing any service.
The printer was idle and no test print was started.

## Verified live topology

```text
Bambu Cloud metadata and task history
                    +
X2D encrypted local MQTT live state
                    |
         greghesp/ha-bambulab 2.2.22
                    |
     Home Assistant 2026.7.3 (integration layer)
                    |
       GET-only home-sensor observer
          +---------+-----------+
          |                     |
 printer.sqlite3          InfluxDB state points
 sessions/history/        live 72h + permanent 5m
 usage/maintenance
          |
 dashboard + Butters + existing Active Monitoring
```

The Home Assistant config entry has cloud authentication, a local host/access
code, and `local_mqtt=true`. The current MQTT connection entity says `local`,
MQTT encryption is on, Developer LAN Mode is off, and firmware-update support
in the integration is disabled. This is the intended hybrid design: cloud
metadata/history plus direct local MQTT live state. It does not enable LAN-Only
Mode, Developer LAN Mode, or disconnect Bambu Cloud, Handy, or Studio.

The installed integration is `greghesp/ha-bambulab` `2.2.22`, 130 files and
7,103,721 bytes. HACS is neither installed nor configured. Home Assistant
reported these devices:

- X2D, hardware `AP04`, firmware `01.02.00.00`
- one AMS 2 Pro, hardware `N3F05`, firmware `05.00.22.19`
- two external spool endpoints, one per X2D toolhead

The integration exposes control-capable services, buttons, lights, switches,
and commands. This project does not call them. It reads only `/api/states` and
the cloud task-list GET endpoint. The chamber light and camera entities found by
discovery are deliberately not mapped as actions.

## Actual X2D entity coverage

Discovery found **77 enabled Bambu entities**: 59 sensors, 14 binary sensors,
two images, one light, and one switch. Serial-bearing entity prefixes remain
redacted. The suffixes below are the actual mappings for this printer.

| Area | Actual entity suffixes and availability |
|---|---|
| Availability and state | `online`, `print_status`, `current_stage`, `print_progress`, `remaining_time`, `current_layer`, `total_layer_count` |
| Job | `task_name`, `gcode_filename`, `print_type`, `print_bed_type`, `print_weight`, `print_length`, `start_time`, `end_time` |
| Job IDs/result | no task/job-ID entity; terminal result is derived from confirmed `print_status`; cloud task IDs are stored separately |
| Toolheads | `left_nozzle_temperature/target_temperature/type/size`, `right_nozzle_temperature/target_temperature/type/size`; the generic nozzle entities duplicate the active tool and are not required |
| Bed/chamber | `bed_temperature`, `bed_target_temperature`, `chamber_temperature`, `chamber_target_temperature` |
| Fans | `cooling_fan_speed`, `aux_fan_speed`, `chamber_fan_speed`, `heatbreak_fan_speed` |
| Active tool/material | `tool_module`, `active_tray`; material/name/color/slot are inferred only from the explicitly mapped active-tray attributes |
| Connectivity | `mqtt_connection_mode`, `mqtt_encryption`, `hybrid_mqtt_control_blocked`, `developer_lan_mode`, `wi_fi_signal` |
| Other read-only metadata | `door`, `extruder_filament`, `hms_errors`, `print_error`, `firmware`, `sd_card_status`, `speed_profile`, `airduct_mode`, `cover_image`, `pick_image` |
| Deliberately excluded sensitive/control surface | `serial_number`, `ip_address`, chamber light, camera switch, and all HA services/buttons |

At discovery the printer was online and idle: stage `idle`, last print status
`finish`, progress 100%, layer 238/238, no active tray, local encrypted MQTT,
Wi-Fi -46 dBm, and both 0.4 mm hardened-steel nozzles present. An `end_time`
during a live print is treated as an expected finish, not an observed actual end.
The observer uses terminal-state confirmation time for local session completion
unless a genuinely observed actual completion timestamp exists.

### AMS 2 Pro and spool coverage

The AMS 2 Pro exposes active/drying flags, humidity and humidity index,
temperature, remaining drying time, and four tray entities. Tray attributes
include slot, name, material type, color, active/empty state, and remaining
percentage when the spool supplies it. At discovery the four materials were
three PLA spools and one PETG spool; remaining percentages were available for
three Bambu spools. Specific colors and inventory are operational data and are
shown in the dashboard, not documented as configuration constants.

Both external-spool endpoints expose an active flag and a spool entity with the
same safe material/name/color attributes. AMS drying state is observational;
this project cannot start, stop, or configure drying.

### Idle state-change cadence

Home Assistant recorder rows represent state changes, not every MQTT report, so
these measurements are lower bounds on transport updates. During the idle
snapshot, changed temperature values were recorded with median gaps of about
7.8 seconds for the left/active nozzle and 8.9 seconds for the right nozzle.
Bed changes had a 38.0-second median, AMS temperature about 635 seconds, Wi-Fi
about 160 seconds, and image timestamp entities about 300 seconds. Most enum,
target, fan, AMS tray, and binary states had only their initial row because the
idle value did not change. No naturally occurring print was present, so loaded
print cadence was not measured.

No Bambu statistics ID existed in `statistics_meta` at discovery, even though
some entity metadata declares state classes. Do not assume HA long-term
statistics until rows are empirically present.

## Normalization and live sessions

`PrinterState` is protocol-independent and keeps unavailable values as `None`.
It carries the exact dual-nozzle, bed/chamber, fan, connection, tool, AMS,
usage, expected-finish, firmware, and job metadata listed above. Each inferred
value has provenance; general AMS inventory is never silently called the active
print material.

The centralized state map is:

| Normalized | Upstream values |
|---|---|
| `offline` | availability false or stale |
| `idle` | `idle`, `ready` |
| `preparing` | `init`, `initializing`, `prepare`, `preparing`, `slicing` |
| `printing` | `running`, `printing` |
| `paused` | `pause`, `paused` |
| `finishing` | `finishing`, `cooling` |
| `completed` | `finish`, `finished`, `complete`, `completed` |
| `failed` | `failed`, `error` |
| `cancelled` | `cancel`, `canceled`, `cancelled`, `stopped` |
| `unknown` | missing or future unmapped values |

SQLite enforces one active local session per printer. Preparation creates a
session; pause/resume preserves it; duplicate observations are idempotent;
offline/unknown observations never close it; reconnect/restart resumes it; and
terminal states require two confirmations by default. A stable job-ID change
closes the old session as unknown, but this X2D currently exposes no HA task-ID
entity. Local source is `locally_observed`; `home_assistant` remains the state
transport provenance.

Locally observed usage is the wall-clock interval from session start through
confirmed end, including preparation, pauses, and finishing. Only confirmed
`completed`, `failed`, and `cancelled` outcomes count toward local usage hours;
unknown outcomes do not. “Completed print count” counts only completed results.

## Lifetime usage provenance

Three values remain separate:

- `printer_reported_lifetime_hours`: currently unavailable (`null`). Neither
  the X2D entity set nor ha-bambulab exposes an authoritative printer counter.
- `ha_bambulab_estimated_usage_hours`: the integration's `total_usage` entity.
  Source review shows that v2.2.22 seeds this from its `usage_hours` config
  option and increments locally. The configured seed is 0.0 hours, so this is
  not represented as printer-reported lifetime usage.
- `locally_observed_print_hours`: confirmed local `PrintSession` duration from
  observer deployment onward.

`maintenance_effective_lifetime_hours` is a high-water calculation. At the
first upstream observation it stores both upstream value and accumulated local
duration. Thereafter it takes the greater of the latest upstream estimate and
the initial upstream position plus new local duration. It never adds the same
session to both counters. If an authoritative printer counter becomes available
later, it takes precedence with the same high-water rule and preserved
provenance.

Cloud-history interval hours are reported separately and are never added to
local or maintenance lifetime hours.

## Bambu Cloud history

The GET-only endpoint used by ha-bambulab is
`/v1/user-service/my/tasks`. The [OpenBambuAPI cloud HTTP reference](https://github.com/Doridian/OpenBambuAPI/blob/main/cloud-http.md)
documents `deviceId`, `after`, and `limit`; the implementation uses bounded
100-record pages, a 15-second timeout, a 16 MiB response bound, and a default
1,000-record safety limit.

The live account returned **49 device-filtered tasks**, oldest start
2026-05-23 19:25:15 UTC and newest start 2026-08-11 13:57:52 UTC. There were 39
status-2 completed tasks and 10 status-3 aborted-or-failed tasks. Status 3 does
not reliably distinguish cancelled from failed, so it is stored as
`aborted_or_failed`. All 49 records had start/end timestamps, slicer `costTime`,
weight, length, plate index, mode, device model, cover metadata, and AMS detail
mappings. Bed type was present for 47; design title for 44; only one had a
non-empty plate name. Materials are obtained from AMS mappings: PLA and PETG
were present, with nozzle IDs 0 and 1. Modes were cloud file, LAN file,
auto-repeat, and cloud slice.

The sum of known start/end intervals is about 209.134 hours. This is not a
lifetime total. The oldest result may be an API/account retention boundary, and
the 49 tasks cannot prove completeness. `costTime`, weight, and length are
slicer/task estimates; actual duration uses end minus start only. Repeating the
import upserts the same `(printer_id, cloud_id)` row. Missing cloud IDs receive
a stable content digest; missing timestamps remain null.

Cloud records are stored separately from the live state machine so incomplete
history cannot create an active session. Reconciliation first uses exact cloud
ID versus local job ID, then a bounded title/time-overlap match. A reconciled
item appears once in canonical history: local timestamps/result win, while
cloud material/plate/cover/task metadata and both provenance labels remain.

Credentials, serial, host, access code, and signed cover URLs are not stored in
the history database or returned by the API. Only `cover_available` is exposed;
the dashboard does not leak a HA token or cloud URL to display an image.

## Maintenance engine

Maintenance is generic and configuration-driven. The project ships **no
numeric manufacturer interval**. A task may use operating hours, completed
print count, calendar days, or an `any`/`all` combination. It stores:

- stable task ID, name, description, enabled state, notes, and source/provenance
- interval and warning threshold for each enabled trigger
- accumulated value, remaining value, next threshold, and ok/warning/overdue
- last completion time, usage position, and completed-print position

`POST /api/printer/maintenance/<task>/complete` requires `confirm=true` and
only appends a local SQLite audit event. It does not call HA, MQTT, or the
printer. Prior completions are never overwritten. The dashboard repeats this
boundary in the section text, confirmation dialog, button label, and response.
Butters does not expose maintenance completion.

Intervals must cite an operator choice or a current source in `source`/`notes`.
Bambu cleaning/lubrication guidance without a numeric interval is not converted
into an invented hour schedule.

## Automatic Active Monitoring

The observer integrates with the existing `MonitoringExportStore`; it does not
create a second collection pipeline. The SEN66 is already stored once at high
resolution. Manual and printer-triggered intervals may overlap as metadata
windows without duplicating sensor writes.

On preparing/printing, a unique `printer_session_id` creates one
`trigger_source=printer` monitoring session. Pause, resume, duplicate events,
browser closure, restart, and temporary HA/MQTT loss preserve that association.
The observer synchronizes the latest session every poll to close crash windows.
After terminal confirmation it records the exact print end, schedules the
configured recovery end (120 minutes by default), and lets the existing export
worker close/export the interval. The monitoring database's unique index makes
start handling restart-safe. Manual sessions retain their existing behavior.

## Environmental association and retention

The actual current SEN66 location is `office`; configuration remains explicit
because room naming can change. For a session with a known interval, the query
uses only raw `environment_live/air_quality_reading` samples:

- 30-minute baseline before start
- exact print start through end
- 120-minute recovery after end

It calculates mean, peak, post mean, and delta for CO2, PM1, PM2.5, PM4, PM10,
VOC index, NOx index, temperature, and humidity, plus the existing VOC recovery
heuristic. Results always say observational association, not causation.

Raw retention is 72 hours. At the discovery instant, three cloud prints were
fully within that window and one crossed its boundary; the other 45 had expired
raw detail. If the full baseline/print interval is past retention, the API says
`raw_samples_expired`. It never substitutes the permanent 15-minute aggregate.
No retention setting is changed by this milestone.

Influx remains cardinality-safe:

| Measurement | Bucket | Retention/use |
|---|---|---|
| `printer_state` | `environment_live` | every observer poll, 72 hours |
| `printer_state_5m` | `environment` | changes plus max 5-minute interval, permanent |
| `print_session` | `environment` | local session updates, permanent |

Job IDs, filenames, material, cloud IDs, and session UUIDs are fields or
relational columns, never Influx tags. Cloud history and maintenance identity
remain in SQLite.

## Dashboard and API

The top-level **Bambu / Printer** tab is failure-isolated from Monitoring,
Active Monitoring, and Status. It has current job/state/stage/progress/times,
layers, material provenance, tool/tray, both nozzle temperatures, bed/chamber,
printer/firmware/connectivity, usage provenance, AMS inventory/drying,
maintenance with local-only completion, canonical print history, and a selected
session's environmental intervals/metrics. Responsive grids collapse for
iPhone-width screens. There are no printer-control buttons.

Routes:

- `GET /api/printer`
- `GET /api/printer/sessions?limit=...`
- `GET /api/printer/sessions/<id>`
- `GET /api/printer/history?limit=...`
- `GET /api/printer/maintenance`
- `GET /api/printer/environment-summary?session_id=...`
- `POST /api/printer/maintenance/<task>/complete` — local audit only

The dashboard has the same LAN/Tailscale trust boundary as existing monitoring
mutations. No printer-control endpoint exists.

## Butters

Butters continues to use bounded dashboard GETs. Its read-only skills now cover
status, current print, both toolhead temperatures, latest-print environment,
usage hours/counts, maintenance due/history, and latest-print duration. The
deterministic router recognizes questions such as “how many hours has the
printer run?”, “what maintenance is overdue?”, and “how long was the last
print?”. Generic sensor skills still reject printer entities, and control verbs
are rejected before tool routing. There is no maintenance-completion voice
skill and no printer-control skill.

## Storage/resource audit

At discovery Home Assistant's recorder database was 207,376,384 bytes with a
4,334,272-byte WAL. The ha-bambulab media root, model/print cache, and timelapse
cache were all zero bytes. The integration directory was about 6.78 MiB.
All 77 entities had at least one recorder row and together produced 1,497 rows
in the inspected 24-hour window. Churn during idle was primarily changed
temperatures and the two five-minute image timestamp entities. No HA history was
deleted and no recorder exclusion is applied. Evidence is currently
insufficient to recommend an exclusion: our observer has not yet run in
production, and loaded-print cadence has not been measured.

The Pi resource snapshot reported `get_throttled=0x80000`. Bit 19 is the
latched historical soft-temperature-limit flag; it is not an undervoltage bit.
The current-condition bits were all clear, so no current throttle condition was
reported.

At a 15-second observer poll, one live state point is 5,760 points/day before
field expansion and expires after 72 hours. Permanent state is at most 288
points/day plus changes. The actual idle-shaped mapping produced 45 fields and
a 2,860-byte line-protocol representation. That is an uncompressed wire upper
estimate of 15.71 MiB/day and 47.13 MiB across the 72-hour live window;
permanent five-minute sampling would be at most about 286.72 MiB/year before
Influx compression. These are projections, not measured on-disk allocation.
SQLite adds one small row per cloud job/session and one row per maintenance
completion. No new large resident service is added: the prepared observer is a
bounded Python process with `MemoryMax=192M` and `CPUQuota=25%`; history refresh
is hourly and Active Monitoring reuses the existing export worker.

## Operations and recovery

Secrets belong only in root-controlled `/etc/home-sensor/printer.env`:

```text
HOME_ASSISTANT_TOKEN=...
BAMBU_CLOUD_TOKEN=...
BAMBU_DEVICE_ID=...
```

Do not paste values into commands, logs, Git, tests, or reports. The non-secret
entity allow-list and task definitions belong in `/etc/home-sensor/printer.toml`
using `server/config/printer.example.toml` as the verified suffix map.

On observer failure, sensor MQTT/Influx ingestion and the dashboard remain
independent. HA/network errors use bounded timeouts and exponential backoff to
five minutes. Influx printer-write errors do not roll back SQLite. Cloud history
errors preserve the last successful import. Active local sessions remain open
during telemetry loss. Restore `printer.sqlite3` and `monitoring.sqlite3`
together when recovering associations; otherwise the idempotent unique keys
rebuild future state without inventing old duration.

Deployment is a separate approval boundary. Repository completion alone does
not authorize copying files, installing the observer unit, populating
configuration, starting/restarting services, provisioning Grafana, changing
Influx retention, or changing Home Assistant.
