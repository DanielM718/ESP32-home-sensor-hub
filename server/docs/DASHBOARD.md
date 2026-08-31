# Frontend Dashboard

The Flask dashboard is served at `http://sensor-pi.local:8080` (or the Pi's
address on port 8080). It uses vanilla JavaScript and the locally installed
Chart.js bundle; it reads only the Flask API, never MQTT directly.

## Four sections

The top-level navigation is hash-backed and does not reload the page:

- `#monitoring` contains current readings, source/range filters, a reusable
  measurement selector, and one historical chart.
- `#bambu-printer` is a failure-isolated, read-only X2D view ordered Current
  Print, Historical Telemetry, Printer Usage, Maintenance, Printer/AMS details,
  Print History, and Environmental Association.
- `#active-monitoring` contains timed sessions, running/recent session state,
  and persistent Historical Data Export jobs.
- `#status` contains the fixed allow-list systemd status view, node status and
  per-node capabilities, architecture/storage notes, and read-only
  troubleshooting commands.

Tab state is bookmarkable. Native links, tab semantics, arrow-key navigation,
responsive forms, and horizontally scrollable tables keep the interface usable
on desktop and narrow phones.

## Monitoring and charts

Current cards retain temperature, humidity, valid battery voltage and decoded
flags, all nine SEN66 measurements, stale/invalid/warm-up handling, and the
source-backed air-quality interpretations.

Cards are identity-driven, not live-cache-driven. Previously known offline
sources remain selectable and show status plus last-seen time. Stale/offline
values are explicitly labeled last-known and are never presented as current;
their historical charts remain queryable for retained/permanent periods.

The historical measurement checklist is derived from fields actually observed
for the selected source(s). Any combination can share the chart, including
Temperature + Humidity + CO2. Chart.js creates one Y axis per unit, reuses an
axis for measurements with the same unit, and includes source, measurement,
value, unit, and timestamp in tooltips.

Only primary measurement fields are graphed. Long-range stored `*_mean` values
are presented under the ordinary measurement label. Stored maxima, p95 values,
and event records remain available to the backend and retention pipeline but
are not normal chart datasets.

## Source capabilities and workflow forms

`/api/latest` and `/api/nodes` expose `available_fields` for each individual
source. This comes from fields actually observed in InfluxDB, including the
permanent identity inventory, not a sensor-type template. Selecting sources immediately rebuilds the
Active Monitoring and Historical Export measurement checklists. With multiple
sources, each choice says whether all or only some selected sources provide it.
The API repeats this validation when a job is created.

Duration presets run from five minutes through 24 hours. `Custom time` reveals
separate whole-number Hours and Minutes controls; minutes are limited to 0–59,
the pair may not both be zero, and the total may not exceed the raw-retention
limit returned by the backend.

Active Monitoring resolutions are provided by `/api/workflows/options`:

- Raw samples
- 1-minute mean
- 5-minute mean
- 15-minute mean
- 1-hour mean

Active session means are calculated from retained raw readings. Numeric sensor
fields use arithmetic mean; battery voltage averages only battery-valid
samples, and status flags are never averaged. A session shorter than its mean
window produces one partial-window mean when it has data.

## Bambu Historical Telemetry

The Bambu Historical Telemetry panel is backed by structured
`printer_telemetry`, not `ams_inventory_json`. Its 1h, 6h, and 24h ranges use
the high-resolution live tier; 7d uses permanent five-minute samples. Initial
selections are every discovered AMS unit's humidity and temperature plus X2D
chamber temperature. Bed/nozzle temperatures and targets, fan/Wi-Fi/progress
diagnostics, and observed status fields are capability-driven choices. Chart
axes are grouped by unit, and another AMS appears automatically from the source
catalog. A known offline/stale AMS remains selectable for retained history.

Manual Active Monitoring and Historical Export forms use the same backend
source/field catalog for SHT41, SEN66, printer, and AMS sources. Boolean Bambu
status fields are raw-only; numeric fields may be downsampled. Automatic print
environment monitoring still requires its configured SEN66 to be online at
print start. Skipping that automatic interval does not stop independent Bambu
telemetry persistence.

## Printer Usage and Maintenance

Printer Usage leads with **Tracked Print Time** as a single large value
(`209 h 8 m` style) plus tracked/completed/failed counts, first and last tracked
print, the rolling average printing hours per day, the current maintenance
mode, and the contributing history sources. The qualifier is always shown: the
figure is the sum of known actual print-history intervals and Bambu Cloud
history may not represent the printer's complete lifetime. It is never labelled
lifetime hours.

Maintenance opens with an overall state, the next task and its due date, and
counts of due-soon, due, overdue, baseline-required, and advisory tasks, plus
the usage tier and why it applies. Each task card shows the manufacturer
cadence verbatim, the current state, last completion, next due date, remaining
time, source provenance with a link to the Bambu Lab wiki, and a local-only
completion button. Tasks with no local completion history read
`Needs a baseline` instead of appearing overdue, and condition-based tasks read
`advisory` with no invented due date. `Mark all maintenance completed today`
requires a confirmation dialog and only writes local audit records.

## CSV formats

Wide is the default. A legacy environment/SEN66-only export keeps its existing
shape and writes one logical timestamp/source sample per row:

```text
timestamp_utc,sensor_type,source_id,node_id,location,<selected fields>,data_tier
```

Selected fields such as `temperature_c` and `humidity` are separate columns;
missing source fields stay blank. Long / normalized remains available with one
measurement per row:

```text
timestamp_utc,sensor_type,source_id,node_id,location,field,value,unit,data_tier
```

Both selectors show a short explanation in the UI. Sessions and jobs live in
SQLite and are processed by `home-sensor-export-worker.service`, so navigation,
refresh, browser closure, and frontend request timeouts do not cancel them.
When any printer or AMS source is selected, both formats append explicit
`printer_id` and `ams_id` identity columns. Long rows keep units explicit and
all Bambu rows label whether values came from live raw data or durable samples.

## Polling

The Monitoring tab refreshes live values every seven seconds. The Bambu /
Printer tab refreshes its current snapshot every seven seconds and loads
history/maintenance independently with `Promise.allSettled`, so one printer
section cannot reject the others or the sensor dashboard. The Active
Monitoring tab polls session/export metadata every five seconds and bounded
previews every 15 seconds. Hidden tabs do not perform their normal periodic
fetches; activating a tab refreshes it immediately. In-flight guards prevent
overlap, page visibility pauses optional work, and Chart.js is instantiated
once and updated in place.

## Files

```text
server/frontend/templates/index.html
server/frontend/static/styles.css
server/frontend/static/app.js
server/frontend/static/vendor/chart.umd.min.js
```

The Pi installer supplies `chart.umd.min.js`; use
`scripts/install_frontend_assets.sh` if that local asset is missing.
