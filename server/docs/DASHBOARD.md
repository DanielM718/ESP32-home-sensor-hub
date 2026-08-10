# Frontend Dashboard

The Flask dashboard is served at `http://sensor-pi.local:8080` (or the Pi's
address on port 8080). It uses vanilla JavaScript and the locally installed
Chart.js bundle; it reads only the Flask API, never MQTT directly.

## Three sections

The top-level navigation is hash-backed and does not reload the page:

- `#monitoring` contains current readings, source/range filters, a reusable
  measurement selector, and one historical chart.
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
source. This comes from the fields actually returned by the latest InfluxDB
query, not a sensor-type template. Selecting sources immediately rebuilds the
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

## CSV formats

Wide is the default. It writes one logical timestamp/source sample per row:

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

## Polling

The Monitoring tab refreshes live values every seven seconds. The Active
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
