# Bambu Lab X2D read-only integration

Verified and implemented: **2026-08-12**. The checked-out repository and the
running Raspberry Pi were inspected without restarting or changing any service.
The printer was idle and no test print was started.

Tracked Print Time, the Bambu Lab maintenance catalog, and maintenance
notifications were added on **2026-08-15** from the first-party X2D wiki. That
work changed the repository only: nothing was deployed, no service was
restarted, and no Home Assistant or printer setting was touched.

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

## First-class historical telemetry

The observer projects a curated subset of each normalized `PrinterState` into
`printer_telemetry`. It writes one `component_type=printer`,
`component_id=main` point and, when values are actually observed, one
`component_type=ams` point for each stable `AmsUnitState.ams_id`. It does not
query Home Assistant from the dashboard or export path.

Normal-poll points use the finite-retention `environment_live` bucket. At the
existing permanent sample cadence (normally five minutes), the same logical
measurement is written to the permanent `environment` bucket. Thus 1h, 6h,
and 24h dashboard views use high-resolution live data with display
downsampling, while the 7d view uses durable five-minute samples. Raw/1-minute
exports are honest live-tier queries and warn when part of the requested range
has expired; coarser Bambu exports can combine labeled live-derived and
durable-derived intervals. Durable samples are never described as raw.

The sensor source catalog is reconstructed from both recent live samples and
permanent telemetry. A temporarily offline printer or AMS stays selectable,
retains its last-seen/capability record, and remains usable for historical
charts and exports. Missing upstream numeric values are omitted instead of
being fabricated as zero.

The exact tags and fields are documented in
[`schema.md`](../config/influxdb/schema.md). Identity is bounded to
`printer_id`, `component_type`, stable `component_id`, and normalized `source`.
Job/session IDs, filenames, filament metadata, arbitrary Home Assistant entity
IDs, and timestamps are not tags. `ams_inventory_json` remains whole-state
inventory metadata and is never parsed for telemetry charts or CSV files.

### Dashboard and Active Monitoring

The **Bambu / Printer** tab includes a capability-driven Historical Telemetry
chart with 1h, 6h, 24h, and 7d ranges. AMS humidity, AMS temperature, and
chamber temperature are the initial selections; bed/nozzle targets and
temperatures, fans, Wi-Fi, progress, remaining time, and observed status fields
remain selectable. Every AMS is rendered from its backend source identity, so
adding a second unit does not require JavaScript changes.

Printer and AMS are also ordinary manual Active Monitoring sources alongside
SHT41 and SEN66 sources. Sessions select an interval over readings the observer
already stored in InfluxDB; they do not start another printer recorder. Numeric
fields may be downsampled with means. Boolean fields are raw-only and are not
averaged.

Automatic environmental monitoring retains its safety rule: the configured
SEN66 must be online when a print begins. If it is not, the automatic
environmental interval is skipped and is not replaced with a printer-only
session. That gate does not affect the observer: printer thermal and AMS
telemetry continue to persist while idle, while SEN66 is offline, and after an
automatic session is skipped.

### Mixed exports

The existing export job API accepts printer and AMS sources together with
environment and air-quality sources. For example, one job can request AMS 1
humidity/temperature, X2D chamber temperature, room temperature/humidity, and
SEN66 CO2/VOC/PM2.5. Long CSV rows include `sensor_type`, stable `source_id`,
unit, `data_tier`, and explicit `printer_id`/`ams_id` when Bambu sources are
selected. Wide CSV uses stable source rows and understandable field columns.
Legacy exports without Bambu sources retain their prior columns.

Example durable AMS humidity query:

```flux
from(bucket: "environment")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "printer_telemetry")
  |> filter(fn: (r) => r.printer_id == "x2d")
  |> filter(fn: (r) => r.component_type == "ams" and r.component_id == "ams_1")
  |> filter(fn: (r) => r._field == "ams_humidity")
  |> aggregateWindow(every: 30m, fn: mean, createEmpty: false)
```

Example durable chamber-temperature query:

```flux
from(bucket: "environment")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "printer_telemetry")
  |> filter(fn: (r) => r.printer_id == "x2d")
  |> filter(fn: (r) => r.component_type == "printer" and r.component_id == "main")
  |> filter(fn: (r) => r._field == "chamber_temperature_c")
  |> aggregateWindow(every: 30m, fn: mean, createEmpty: false)
```

This addition remains read-only. It adds no Home Assistant service call,
printer/AMS command, credential response, or client-visible entity ID.

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

## Tracked Print Time

**Tracked Print Time** is the canonical cumulative runtime metric and the
headline number on the dashboard. It is the sum of *known actual* print
intervals:

```text
interval = ended_at - started_at
```

Rules:

- A print counts when both timestamps exist and `ended_at >= started_at`,
  regardless of whether it completed, failed, was cancelled, or was aborted:
  the machine physically operated for that interval.
- Slicer `costTime`, slicer estimates, remaining-time predictions, and an
  expected finish are never used. A print with a missing or invalid interval
  stays unknown and is counted only in `tracked_unknown_interval_job_count`.
- Deduplication reuses the existing canonical reconciliation. A cloud task
  already reconciled to a local session contributes nothing extra; if that
  local session has no usable interval, the reconciled cloud interval is used
  once instead and provenance becomes `bambu_cloud_history_reconciled`. There
  is no second runtime ledger.
- `tracked_history_complete` is always `false`. Bambu Cloud history has an
  unknown account/API retention boundary, local observation only starts at
  observer deployment, and no authoritative printer lifetime counter exists.
  `tracked_history_completeness_reasons` states this in the payload.

Tracked Print Time is therefore **not** printer lifetime hours, and the
dashboard never labels it as such. `printer_reported_lifetime_hours`,
`ha_bambulab_estimated_usage_hours`, `locally_observed_print_hours`, and
`maintenance_effective_lifetime_hours` keep their previous meanings.

A rolling utilization block accompanies it:
`rolling_tracked_print_hours`, `rolling_tracked_history_days`, and
`rolling_tracked_print_hours_per_day` over a **30-day window**. The window
length and the 7-day minimum history are local policy
(`rolling_window_source = local_dashboard_policy`); only the tier thresholds
come from Bambu Lab.

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

Maintenance is generic and now ships a manufacturer catalog in addition to any
configured task. A configured task with the same `task_id` overrides a catalog
entry, and `[maintenance_engine] manufacturer_tasks_enabled = false` disables
the catalog entirely. Supported trigger kinds:

| Trigger kind | Meaning |
|---|---|
| `threshold` | The pre-existing generic engine: operating hours, completed prints, calendar days, `any`/`all` |
| `calendar_months` | A fixed manufacturer cadence in whole calendar months |
| `usage_tiered_calendar_months` | Calendar months selected by the manufacturer usage tier |
| `event_after_task` | Becomes due when a prerequisite task is completed after this task's last completion |
| `manual_inspection` | Condition-based manufacturer guidance with no published interval; advisory only, never due or overdue |

Calendar cadences use real calendar months (15 January + 1 month = 15
February), not invented 30-day blocks. Each task stores its stable ID, name,
description, enabled state, notes, cadence text, source, source URL, source
revision, and the warning-source label.

### Manufacturer source

All numeric intervals below come from the first-party page
[Cleaning and Maintenance Recommendations for the X2D](https://wiki.bambulab.com/en/x2d/maintenance/periodic-maintenance),
wiki revision `2026-04-22`, retrieved `2026-08-15`. The remaining X2D wiki
maintenance pages (belt tension, cold pull, extruder cleaning, auxiliary
extruder cleaning, and the `replace-*` service guides) were reviewed and
publish no periodic interval. No third-party mirror is used, and intervals
published for other Bambu Lab models are never applied to the X2D.

### Scheduled tasks

| Task ID | Manufacturer cadence | Trigger |
|---|---|---|
| `x2d_xy_axis_clean_lubricate` | 1 month at >= 5 h/day, 2 months at 1-5 h/day, 3 months below 1 h/day | `usage_tiered_calendar_months` |
| `x2d_z_axis_deep_maintenance` | 3 / 4 / 5 months for the same tiers | `usage_tiered_calendar_months` |
| `x2d_live_view_camera_cleaning` | Every 6 months | `calendar_months` |
| `x2d_full_calibration_after_axis_service` | Immediately after XY or Z axis service | `event_after_task` |

### Advisory tasks (deliberately not scheduled)

Bambu Lab publishes these without an automatable number, so no interval is
invented. They appear as `advisory` with the manufacturer wording shown:

| Task ID | Manufacturer wording |
|---|---|
| `x2d_chamber_interior_cleaning` | After extended use |
| `x2d_build_plate_cleaning` | Cleaning it regularly is recommended |
| `x2d_main_extruder_cleaning` | After prolonged use |
| `x2d_activated_carbon_filter` | When heavily contaminated |
| `x2d_silicone_nozzle_wiper` | If damaged or deformed |
| `x2d_auxiliary_ptfe_tube` | When the clamped end is worn |
| `x2d_filament_cutter_blade` | Every 8-12 rolls of regular filament, 6-10 rolls of high-wear filament |

The filament cutter cadence *is* numeric, but its unit is consumed filament
rolls. This deployment has no reliable roll counter: print weights are slicer
estimates and spool sizes are unknown. Guessing a roll count would be an
invented schedule, so the rule is displayed for manual tracking instead.
Likewise, Bambu Lab says to shorten camera cleaning for highly volatile
materials such as ABS but publishes no shortened number, so material history is
not used as an automated trigger anywhere.

### Heavy-use mode

Bambu Lab defines the usage tiers explicitly by average daily printing time, so
they are automated from Tracked Print Time:

| Mode | Manufacturer condition |
|---|---|
| `heavy_use` | >= 5 printing hours/day |
| `normal` | 1-5 printing hours/day |
| `low_use` | < 1 printing hour/day |

`maintenance_mode`, `maintenance_mode_reason`, and
`rolling_tracked_print_hours_per_day` are exposed on every usage payload. With
less than 7 days of tracked history the mode is `normal` with reason
`insufficient_tracked_history` rather than a guess. The averaging window and
that minimum are local policy; the thresholds are the manufacturer's. Only
`usage_tiered_calendar_months` tasks change with the mode.

### Baseline required

The dashboard cannot know whether maintenance was performed before this feature
existed, so a scheduled task with no local completion history is
`baseline_required`: never due, never overdue, and with no next-due date. The
UI says so and offers `Mark completed today` per task and
`Mark all maintenance completed today` behind a confirmation dialog. Event
tasks and advisory tasks need no baseline.

### Local warning lead time

`due_soon` uses a local lead time (7 days for XY axis, 14 days for Z axis and
the camera). This is a dashboard courtesy, labelled `warning_source =
local_dashboard_policy` on every task, and is never presented as a Bambu Lab
recommendation. The interval itself always carries the manufacturer source.

### Local-only completion boundary

`POST /api/printer/maintenance/<task>/complete` and
`POST /api/printer/maintenance/complete-all` require `confirm=true` and only
append local SQLite audit events. They do not call HA, MQTT, Bambu Cloud, or
the printer; they never reset a printer-side counter or change a printer
setting. Prior completions are never overwritten. The dashboard repeats this
boundary in the section text, confirmation dialog, button label, and response.
Butters does not expose maintenance completion.

## Maintenance notifications

The repository had no outbound notification service, so this milestone adds the
durable layer and a notifier interface rather than a competing subsystem.

`maintenance_notification_state` holds one row per subject
(`maintenance_task:<id>` or `maintenance_mode:<printer_id>`) with its last
observed state. `maintenance_notification_events` is an append-only log of
transitions with `event_type`, `previous_state`, `new_state`, a payload, and a
delivery status.

Emitted event types:

- `maintenance_due_soon`, `maintenance_due`, `maintenance_overdue`
- `maintenance_returned_to_ok`, only after a real problem state
- `heavy_use_mode_entered`, `heavy_use_mode_exited`

Deduplication is edge-triggered. The observer re-evaluates every
`[maintenance_engine] evaluation_seconds` (default 300); an unchanged state
appends nothing, so fast polling cannot spam. Because the last state is
persisted, a service restart does not resend. A progression
`ok -> due_soon -> due -> overdue` emits exactly one event per transition, each
task and the usage tier deduplicate independently, and recording a completion
re-evaluates immediately so the lifecycle resets and later re-arms.
`baseline_required` and `advisory` are not notifiable states.

Delivery uses a `MaintenanceNotifier` protocol with one method,
`deliver(event) -> status`. The shipped `LoggingMaintenanceNotifier` writes a
structured service-log line and marks the event `logged`; a notifier that
raises leaves the event `pending` for redelivery. Nothing is sent off the Pi
and no new credential or external service is introduced. Connecting Home
Assistant, Telegram, or mobile push means implementing `deliver` against that
transport, providing its secret through the existing root-controlled
`/etc/home-sensor/printer.env`, and passing the notifier to
`dispatch_notifications`. Until then, alerts are visible through
`GET /api/printer/maintenance/events` and the dashboard.

## Schema and migration

The printer database gains additive, idempotent, restart-safe changes only. No
print history, cloud import, or completion record is deleted or rewritten:

- `maintenance_tasks` gains `trigger_kind` (default `threshold`),
  `interval_months`, `interval_months_low_use`, `interval_months_normal_use`,
  `interval_months_heavy_use`, `prerequisite_task_ids`, `cadence`,
  `source_url`, `source_revision`, and `warning_source`. Each is added with
  `ALTER TABLE ... ADD COLUMN` only when absent, and every default preserves
  the previous behaviour of an existing row.
- `maintenance_notification_events` and `maintenance_notification_state` are
  new `CREATE TABLE IF NOT EXISTS` tables with their own indexes.

Tracked Print Time adds no table: it is derived from the existing
`print_sessions` and `cloud_print_history` rows and their reconciliation.

## Automatic Active Monitoring

The observer integrates with the existing `MonitoringExportStore`; it does not
create a second collection pipeline. The SEN66 is already stored once at high
resolution. Manual and printer-triggered intervals may overlap as metadata
windows without duplicating sensor writes.

On preparing/printing, the coordinator first resolves the configured SEN66
location with the dashboard's same `last_seen` and stale-threshold semantics.
Only an `online` station permits a unique `printer_session_id` to create one
`trigger_source=printer` monitoring session. If it is stale, offline, unknown,
or the availability query fails, no empty session is created; a durable
`printer_monitoring_status` row records `skipped`, the reason, last seen, and
sensor status. A skipped print is not silently started later if SEN66 returns.

Pause, resume, duplicate events, browser closure, and restart preserve an
existing association. If SEN66 is lost during an existing print, the interval
remains intact and its durable state becomes `degraded`; recovery returns it to
`running` without creating a second session. The print observer and printer
session continue normally in every case. The observer synchronizes the latest
session every poll to close crash windows.
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
- `GET /api/printer/usage`
- `GET /api/printer/maintenance`
- `GET /api/printer/maintenance/events?limit=...&pending=...`
- `GET /api/printer/environment-summary?session_id=...`
- `POST /api/printer/maintenance/<task>/complete` — local audit only
- `POST /api/printer/maintenance/complete-all` — local audit only

The page order is Current Print, Printer Usage, Maintenance, Printer/AMS
details, Print History, and Environmental Association. Print history keeps its
per-print Duration column; it is simply secondary to usage and maintenance.

The dashboard has the same LAN/Tailscale trust boundary as existing monitoring
mutations. No printer-control endpoint exists.

## Butters integration notes

Every field Butters already reads is unchanged: the `usage` object keeps
`locally_observed_print_hours`, `locally_observed_completed_print_count`,
`printer_reported_lifetime_hours`, `ha_bambulab_estimated_usage_hours`, and
`maintenance_effective_lifetime_hours`; every maintenance task keeps `name`,
`enabled`, `due`, `overdue`, and `warning`. All additions are additive and no
field was renamed or removed. The one deliberate behaviour change is that a
task with no local completion history now reports `state = baseline_required`
with `due`/`overdue` false instead of immediately reporting overdue, and the
former `state = "warning"` string is now `state = "due_soon"` while the
`warning` boolean is unchanged.

Stable fields a later Butters milestone should consume:

| Purpose | Field |
|---|---|
| "How long has the printer run?" | `usage.tracked_print_hours`, `usage.tracked_print_seconds` |
| Job counts | `usage.tracked_job_count`, `tracked_completed_count`, `tracked_failed_or_cancelled_count` |
| History bounds and honesty | `usage.tracked_first_print_at`, `tracked_last_print_at`, `tracked_history_complete`, `tracked_history_provenance` |
| Recent utilization | `usage.rolling_tracked_print_hours_per_day`, `usage.rolling_window_days` |
| Usage tier | `usage.maintenance_mode`, `usage.maintenance_mode_reason` |
| "What maintenance is coming?" | `summary.overall_state`, `summary.next_task`, `summary.overdue_count`, `summary.due_count`, `summary.due_soon_count`, `summary.baseline_required_count` |
| Per-task detail | `tasks[].state`, `cadence`, `next_due_at`, `remaining_days`, `manufacturer_source_url` |
| Alerts | `GET /api/printer/maintenance/events` |

Butters must not gain a maintenance-completion skill, and this milestone adds
no Butters code.

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

The maintenance catalog is seeded by the observer at startup, so the
manufacturer tasks appear only after `home-sensor-printer-observer.service` is
restarted on a deployment that already has this code. Existing print history,
cloud imports, and completion records survive that restart untouched. Seeding
is idempotent, so a repeated restart changes nothing.

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
