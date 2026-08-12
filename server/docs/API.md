# Flask REST API

The Flask app is a lightweight overview API and dashboard host. Environmental
data comes from InfluxDB; optional current printer state comes from the
observer's local SQLite checkpoint. Flask never reads directly from MQTT, Home
Assistant, or the printer.

The root route `/` serves the Chart.js frontend dashboard. API endpoints live
under `/api/`.

Service:

```text
home-sensor-dashboard.service
```

Runtime entrypoint:

```bash
cd /opt/home-sensor/server/backend
/opt/home-sensor/server/backend/.venv/bin/gunicorn --workers 2 --bind 0.0.0.0:8080 'app.web:create_app()'
```

## Endpoints

### `GET /api/health`

Returns process health:

```json
{"status": "ok"}
```

### `GET /api/latest`

Returns the most recent field values per environment node and air-quality
station from the last 30 days of InfluxDB data.

Response shape:

```json
{
  "generated_at": "2026-01-01T12:00:00Z",
  "environment": [
    {
      "id": "1",
      "sensor_type": "environment",
      "node_id": 1,
      "topic": "home/sensors/1",
      "last_seen": "2026-01-01T11:59:00Z",
      "temperature_c": 24.8,
      "humidity": 41.6,
      "battery_mv": 4058,
      "status_flags": 4,
      "battery_measurement_ok": true,
      "battery_low": false,
      "battery_shutdown": false,
      "available_fields": ["temperature_c", "humidity", "battery_mv"],
      "sequence": 1523
    }
  ],
  "air_quality": [
    {
      "id": "printer_room",
      "sensor_type": "air_quality",
      "location": "printer_room",
      "topic": "home/air/printer_room",
      "last_seen": "2026-01-01T11:59:05Z",
      "temperature_c": 24.5,
      "humidity": 42.3,
      "co2": 721,
      "pm1": 1.1,
      "pm25": 2.8,
      "pm4": 3.5,
      "pm10": 5.2,
      "voc_index": 88,
      "nox_index": 12,
      "available_fields": ["temperature_c", "humidity", "co2", "pm1", "pm25", "pm4", "pm10", "voc_index", "nox_index"]
    }
  ],
  "stale_after_seconds": 1800,
  "nodes": [
    {
      "id": "1",
      "sensor_type": "environment",
      "node_id": 1,
      "status": "online",
      "status_flags": 4,
      "battery_measurement_ok": true,
      "battery_low": false,
      "battery_shutdown": false,
      "available_fields": ["temperature_c", "humidity", "battery_mv"]
    }
  ]
}
```

The `nodes` snapshot is derived from the same latest-value query used for the
current readings. This lets the dashboard update current readings and node
status with one InfluxDB query instead of immediately repeating it through
`/api/nodes`. The standalone `/api/nodes` endpoint remains supported.

SEN66 field names are identical across MQTT, InfluxDB, `/api/latest`, and
`/api/readings`: `temperature_c`, `humidity`, `co2`, `pm1`, `pm25`, `pm4`,
`pm10`, `voc_index`, and `nox_index`. Temperature is degrees Celsius, humidity
is percent relative humidity, CO2 is ppm, particulate fields are micrograms per
cubic metre, and VOC/NOx are unitless indices. Older InfluxDB data may omit
fields that were not stored at the time; omitted fields do not fail the request.

Each air-quality station also includes `interpretations`, `overall_status`,
`summary_15m`, `previous_15m`, `rolling_24h`, and `active_events`. Every metric
interpretation exposes category, severity, framework, threshold type, averaging
period, source document/revision/URL, explanation, limitation, update timestamp,
stale state, warm-up state, and whether the category is official or heuristic. Invalid,
stale, and coverage-insufficient values are explicitly unavailable. Raw SRAW
ticks and reset/uptime metadata are diagnostic fields, not concentrations.

### `GET /api/readings`

Returns historical series for charts.

Query parameters:

- `range`: `1h`, `24h`, `7d`, or `30d`; default `24h`
- `sensor_type`: `all`, `environment`, or `air_quality`; default `all`
- `node_id`: optional environment node filter
- `location`: optional air-quality location filter

Examples:

```text
/api/readings?range=1h&sensor_type=environment&node_id=1
/api/readings?range=7d&sensor_type=air_quality&location=printer_room
```

Response shape:

```json
{
  "generated_at": "2026-01-01T12:00:00Z",
  "range": "24h",
  "window": "15m",
  "sensor_type": "all",
  "data_tier": "15m_aggregate",
  "series": [
    {
      "id": "1",
      "sensor_type": "environment",
      "node_id": 1,
      "topic": "home/sensors/1",
      "points": [
        {
          "time": "2026-01-01T11:50:00Z",
          "temperature_c": 24.8,
          "humidity": 41.6,
          "battery_mv": 4058
        }
      ]
    },
    {
      "id": "printer_room",
      "sensor_type": "air_quality",
      "location": "printer_room",
      "topic": "home/air/printer_room",
      "points": [
        {
          "time": "2026-01-01T11:50:00Z",
          "temperature_c": 24.5,
          "humidity": 42.3,
          "co2": 721.0,
          "pm1": 1.1,
          "pm25": 2.8,
          "pm4": 3.5,
          "pm10": 5.2,
          "voc_index": 88.0,
          "nox_index": 12.0
        }
      ]
    }
  ],
  "events": []
}
```

Environment history pivots each raw `battery_mv` together with the
same-timestamp `status_flags` and applies a bitwise `BIT2` test before
downsampling. Battery points with missing status or a clear valid bit are
omitted from `/api/readings`; temperature and humidity history is unaffected.
For `range=1h`, air-quality history is downsampled to one-minute display points
from the bounded live bucket and reports `data_tier=live_1m`. Longer ranges use
only stored UTC-aligned 15-minute statistics and report
`data_tier=15m_aggregate`. Pre-migration raw history must be reconciled with the
maintained backfill before this code is deployed; it is not queried as a runtime
fallback. Mean and maximum fields remain distinct, while sparse event episodes
are returned separately in `events`. A historical point contains only fields
present in that window, so legacy and partially populated data remain valid JSON
and render as gaps rather than invented zeroes.

### Printer endpoints

Printer endpoints are absent from the critical sensor data path and expose no
credentials or raw Home Assistant attributes:

- `GET /api/printer`: normalized current state, or explicit `not_configured` /
  unavailable state.
- `GET /api/printer/sessions?limit=20`: newest sessions; `limit` is restricted
  to `1..100`.
- `GET /api/printer/environment-summary`: observational baseline/print/recovery
  summary over the latest session and high-resolution SEN66 data.

There is no write/control printer endpoint. Details and response fields are in
[`BAMBU_X2D.md`](BAMBU_X2D.md).

### `GET /api/nodes`

Returns node/station status based on latest readings and
`NODE_STALE_AFTER_SECONDS`.

Latest discovery is bounded: SHT41 nodes remain discoverable for seven days
(far longer than their normal 15-minute cadence and 30-minute stale threshold),
while SEN66 stations remain discoverable for 30 minutes (90 times the configured
20-second stale threshold and equal to the restart-recovery horizon). This keeps
recently offline devices visibly stale without scanning the full 72-hour raw
tier per poll.

```json
{
  "generated_at": "2026-01-01T12:00:00Z",
  "stale_after_seconds": 1800,
  "nodes": [
    {
      "id": "1",
      "sensor_type": "environment",
      "node_id": 1,
      "topic": "home/sensors/1",
      "last_seen": "2026-01-01T11:59:00Z",
      "age_seconds": 60,
      "status": "online",
      "battery_mv": 4058,
      "status_flags": 4,
      "battery_measurement_ok": true,
      "battery_low": false,
      "battery_shutdown": false,
      "available_fields": ["temperature_c", "humidity", "battery_mv"],
      "stale_reason": null,
      "sequence": 1523
    }
  ]
}
```

For environment nodes, both `/api/latest` and `/api/nodes` expose the raw
`status_flags` integer plus decoded `battery_measurement_ok`, `battery_low`, and
`battery_shutdown` booleans. Decoding uses independent bitwise tests for
`BIT2`, `BIT3`, and `BIT4`; combined or unknown bits do not prevent known bits
from being recognized.

When the latest packet has no `status_flags`, all four status values are JSON
`null`. When `battery_measurement_ok` is not `true`, `battery_mv` is also
`null`, including a raw placeholder zero with `BIT2` clear. A stale node has
`stale_reason` set to `battery_shutdown` when its final packet carried `BIT4`,
or `no_recent_reading` otherwise. Status is `online` through the configured
stale threshold, `stale` for the next three threshold windows, and `offline`
after four thresholds; shutdown context remains visible in either late state.

`available_fields` is source-specific. It contains only supported measurement
fields actually observed for that node/station by the bounded latest query. A
node that never published `battery_mv` therefore does not advertise battery,
even though another environment node may advertise it.

### `GET /api/status`

Returns a credential-free, read-only Pi status snapshot. The route accepts no
unit name or command parameter. It checks only the backend allow-list:

```text
home-sensor-dashboard.service
home-sensor-bridge.service
home-sensor-export-worker.service
mosquitto.service
influxdb.service
grafana-server.service
```

Each service reports its display/unit names, installed flag, load/active/sub
states, description, state-entered time, approximate uptime, check timestamp,
and fixed read-only `systemctl status` / `journalctl` examples. A missing unit
is returned as `installed=false` and does not fail the response. The payload
also includes hostname, backend health, configured stale thresholds, expected
SEN66 publish interval, raw-retention/max-session duration, and the permanent
15-minute aggregate resolution. It never returns environment files, tokens,
passwords, or arbitrary journal output.

## Active Monitoring

Active Monitoring defines a server-timed interval over the existing InfluxDB
pipeline. It does not subscribe to MQTT, copy readings into SQLite, or change a
sensor's publish rate.

Routes:

```text
POST   /api/monitoring/sessions
GET    /api/monitoring/sessions
GET    /api/monitoring/sessions/<uuid>
POST   /api/monitoring/sessions/<uuid>/stop
DELETE /api/monitoring/sessions/<uuid>
GET    /api/monitoring/sessions/<uuid>/preview
```

Creation accepts `name` (1–120 characters), optional `notes` (up to 2,000
characters), `duration_seconds`, nonempty `sources`/`fields`, `resolution`, and
`csv_format`. The API minimum is 10 seconds for deterministic integration
testing; the dashboard's whole-minute custom control has a practical minimum of
one minute. The maximum is the configured raw-retention horizon, normally 72
hours. Supported resolutions are `raw`, `1m`, `5m`, `15m`, and `1h`; the
options endpoint supplies their human-readable labels.

For active sessions, every non-raw resolution is calculated from retained raw
session readings. Temperature, humidity, gas/index, particulate, CO2, and valid
battery voltage use arithmetic mean. Status flags are not selectable numeric
measurements and are never averaged. A session shorter than its requested
window yields one partial-window mean when samples exist.

The server sets `start_time_utc`, `scheduled_end_time_utc`, status, and final
end. Once the deadline passes, a read, preview, or the worker's reconciliation
pass atomically changes `running` to `completed` and creates exactly one
automatic export. Early stop is idempotent and uses the earlier of server-now
or the deadline. A stopped/completed session returns its associated job:

```json
{
  "id": "f44ad071-24cc-4fbb-8a0e-017083b88a8b",
  "status": "completed",
  "start_time_utc": "2026-08-01T20:00:00.000000Z",
  "scheduled_end_time_utc": "2026-08-01T21:00:00.000000Z",
  "actual_end_time_utc": "2026-08-01T21:00:00.000000Z",
  "effective_end_time_utc": "2026-08-01T21:00:00.000000Z",
  "duration_seconds": 3600,
  "elapsed_seconds": 3600,
  "remaining_seconds": 0,
  "server_time_utc": "2026-08-01T21:00:03.000000Z",
  "preview": null,
  "export": {
    "id": "44da1414-0486-409c-a27a-5598d9316d09",
    "status": "running",
    "rows_written": 42000,
    "is_download_ready": false
  },
  "is_download_ready": false
}
```

The preview endpoint uses bounded count/first/last/recent queries and returns at
most 20 recent measurement values. `row_count_is_approximate=true` makes clear
that the count is measurement values, not necessarily wide CSV rows. Listing
and status endpoints never return CSV contents.

A running session cannot be deleted. A finished session with an active export
must first have that export cancelled and reach `cancelled`; deleting a session
then removes its automatic job and files. Automatic jobs cannot be deleted
directly through `/api/exports`; this preserves the session relationship.

## Historical Exports

Routes:

```text
POST   /api/exports
GET    /api/exports
GET    /api/exports/<uuid>
POST   /api/exports/<uuid>/cancel
DELETE /api/exports/<uuid>
GET    /api/exports/<uuid>/download
```

Creation validates and persists a `queued` job, then returns HTTP 202. It never
runs the InfluxDB export in the request. `start_time` and `end_time` must be ISO
8601 values with `Z` or an explicit offset; they are normalized to UTC. Equal,
reversed, timezone-free, and malformed intervals return HTTP 400 and are never
silently swapped.

Source identities are allowlisted objects:

```json
{"sensor_type":"environment","node_id":1}
{"sensor_type":"air_quality","location":"printer_room"}
```

Supported environment field names are `temperature_c`, `humidity`, and
`battery_mv`. Supported air-quality fields are `temperature_c`, `humidity`,
`co2`, `pm1`, `pm25`, `pm4`, `pm10`, `voc_index`, and `nox_index`. A global
field can be unsupported for some sources. Creation also resolves the current
source capability snapshot: every requested field must have been actually
observed on at least one selected source, and every source identity must be
currently discoverable. This prevents a battery-less node from accepting a
battery-only job while still allowing a mixed selection where only some
sources provide a requested field.

`raw` queries `environment_reading` in `environment` and
`air_quality_reading` in `environment_live`. Historical `1m`, `5m`, and `1h`
exports calculate means from those raw readings. An air-quality interval older
than the raw-retention boundary receives a warning; the worker does not silently
substitute aggregates. Historical `15m` retains the existing behavior: it reads
stored `*_mean` fields from `environment/air_quality_15m`, exposes them under
the ordinary selected field name, and produces no environment-node rows.

Statuses are `queued`, `running`, `cancel_requested`, `completed`, `failed`,
and `cancelled`. Progress includes `current_phase`, rows/bytes, completed/total
work units, persisted warnings, per-source results, timestamps, attempt count,
and server-derived queued/active/total elapsed seconds. The browser advances
visible clocks locally from `server_time_utc`; it does not update SQLite each
second.

Cancellation is immediate for queued jobs. A running job changes to
`cancel_requested`; the worker stops between bounded queries or while consuming
a stream, removes the partial file, and marks it cancelled. Only completed,
failed, or cancelled manual jobs can be deleted. Download is available only for
a completed row whose final `.csv` exists; it streams the existing file and
never regenerates it.

Valid no-data intervals are successful. Missing fields, partial source
coverage, expired raw data, and intervals predating deployment create no fake
timestamps or zero measurements. A total no-data result is a completed,
header-only CSV with zero rows and `zero_data` source results.

### CSV Schemas

Wide is the default:

```text
timestamp_utc,sensor_type,source_id,node_id,location,<selected fields>,data_tier
```

It has one row per timestamp/source, places measurements such as
`temperature_c` and `humidity` in separate columns on that same row, and leaves
unavailable values blank. Source identity columns distinguish multiple nodes.
Aggregate storage suffixes are not leaked into selected column names.

Long / normalized remains available:

```text
timestamp_utc,sensor_type,source_id,node_id,location,field,value,unit,data_tier
```

Both formats use UTF-8, Python's CSV quoting, UTC timestamps, numeric text, and
no `NaN`, `None`, dictionaries, or synthetic empty rows. Units are `degC`,
`percent`, `mV`, `ppm`, `ug/m3`, or `index`. Spreadsheet-formula prefixes are
escaped for text, and the attachment name is a sanitized name plus UTC start
and short job ID. Internal filesystem paths are never returned.

### Workflow Options

`GET /api/workflows/options` returns minimum/maximum monitoring duration,
machine and display units, field labels/groups, allowed formats, and separate
backend-authoritative monitoring/export resolution metadata. Current source
identities continue to come from `/api/nodes` (or the same node snapshot within
`/api/latest`).

## Error Responses

Invalid query parameters return HTTP 400:

```json
{"error": "bad_request", "message": "range must be one of: 1h, 24h, 7d, 30d"}
```

InfluxDB query failures return HTTP 503 with a generic message and detailed logs
in journald.

## Verification On The Pi

After starting `home-sensor-dashboard.service`, run:

```bash
/opt/home-sensor/server/scripts/verify_api.sh
```

Set `API_BASE_URL` to test a different bind address:

```bash
API_BASE_URL=http://sensor-pi.local:8080 /opt/home-sensor/server/scripts/verify_api.sh
```

## Official References

- Flask quickstart and app routing: <https://flask.palletsprojects.com/en/stable/quickstart/>
- Flask with Gunicorn: <https://flask.palletsprojects.com/en/stable/deploying/gunicorn/>
- InfluxDB Python client: <https://docs.influxdata.com/influxdb/v2/api-guide/client-libraries/python/>
- Flux `aggregateWindow`: <https://docs.influxdata.com/flux/v0/stdlib/universe/aggregatewindow/>
