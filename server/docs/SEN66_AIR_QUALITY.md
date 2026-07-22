# SEN66 Interpretation, Aggregation, and Retention

This document is the source of truth for the SEN66 path implemented in this
repository. It distinguishes official categories and guideline comparisons from
manufacturer interpretations and dashboard heuristics.

## Repository Audit and Pre-change Baseline

Before this change:

- SEN66 firmware was controlled by
  `esp/ESP32C3_SEN66_air_quality/main/main.c`, `sen66.c`, `sen66.h`,
  `mqtt_transport.c`, `mqtt_transport.h`, and `app_config.example.h`.
- The module ran continuous measurement internally, but the host read it once
  every 5 seconds through `APP_MEASUREMENT_INTERVAL_MS`.
- Every successful read was also published immediately, so polling and MQTT
  publishing were both 5 seconds (17,280 packets per station per day).
- The topic was `home/air/<location>`. Schema v1 carried all nine readings plus
  `packet_type`, `schema_version`, `firmware_version`, `node_id`, `sequence`, and
  `status_flags`.
- `server/backend/bridge/topic_router.py` validated packets and
  `bridge/mqtt_bridge.py` sent every valid packet through `app/influx.py`.
- Every packet became one `air_quality_reading` point in the `environment`
  bucket. Tags were `location`, `topic`, and `sensor_type`; fields were `co2`,
  `pm1`, `pm25`, `pm4`, `pm10`, `voc_index`, `nox_index`, `temperature_c`, and
  `humidity`.
- The database write rate therefore matched MQTT: one long-retained point every
  5 seconds per SEN66 station.
- `server/scripts/setup_influxdb.sh` created one bucket with retention `0`
  (infinite). There was no live/aggregate retention split.
- Flask cards and graphs were controlled by `server/frontend/templates/index.html`,
  `server/frontend/static/app.js`, and `styles.css`; API queries were in
  `server/backend/app/queries.py`. The Grafana panel was in
  `server/config/grafana/dashboards/home-sensor-environment.json`.
- No sensor uptime, boot identifier, boot reason, or reliable first-seen sensor
  time reached the server. A dashboard restart or MQTT reconnect could not
  legitimately be treated as a sensor restart.
- The SEN66 I2C command supports SRAW_VOC and SRAW_NOx, but the local driver did
  not expose command `0x0405`.
- Interpretation belonged in a shared backend policy module, not JavaScript or
  Jinja threshold chains. Aggregation belonged in the MQTT bridge because it
  already observes every packet and is easier to test and redeploy than
  firmware or an Influx task.
- Existing `air_quality_reading` points must remain readable. Influx fields are
  additive, so no destructive data migration is necessary.

## Implemented Architecture

The sensor, transport, live store, and long-term store now have separate jobs:

1. The SEN66 remains in continuous measurement and is read every 1 second.
2. Routine MQTT remains compact at 5 seconds. The live Flask dashboard polls the
   API every 7 seconds.
3. MQTT packets are written as `air_quality_reading` to `environment_live`, which
   has 72-hour retention.
4. The bridge retains every accepted packet in its current in-memory aligned
   window; it does not downsample before calculating statistics.
5. At each UTC 00/15/30/45-minute boundary, it writes one
   `air_quality_15m` point to the existing long-term `environment` bucket.
6. Threshold crossings and rapid rises use a separate long-term
   `air_quality_event` measurement.
7. One-hour graphs use bounded live data at 1-minute display windows. Longer
   graphs use verified stored 15-minute aggregates; legacy raw data is backfilled
   before deployment rather than scanned as a runtime fallback.

This retains MQTT, InfluxDB OSS v2, Flask, Grafana, systemd, and the Raspberry Pi
installer. Existing long-term points are not deleted or rewritten.

## Primary Sources and Exact Interpretations

### PM2.5 and PM10

Source: U.S. Environmental Protection Agency, **40 CFR Part 58 Appendix G** as
revised in the PM NAAQS final rule published March 6, 2024. The official table
uses 24-hour concentration and requires PM2.5 truncation to 0.1 µg/m³ and PM10
truncation to integer µg/m³. The dashboard does not calculate an AQI number.

<https://www.epa.gov/system/files/documents/2024-04/2024-pm-naaqs-fr-published.pdf>

| EPA category | PM2.5, 24-hour µg/m³ | PM10, 24-hour µg/m³ | Dashboard severity |
|---|---:|---:|---|
| Good | 0.0–9.0 | 0–54 | good |
| Moderate | 9.1–35.4 | 55–154 | moderate |
| Unhealthy for sensitive groups | 35.5–55.4 | 155–254 | poor |
| Unhealthy | 55.5–125.4 | 255–354 | very poor |
| Very unhealthy | 125.5–225.4 | 355–424 | very poor |
| Hazardous | 225.5+ | 425+ | hazardous |

Two different results are displayed:

- **Current-level context** places the latest valid sample beside the EPA
  boundaries. Status type: `dashboard_heuristic`; official category: false.
  It is explicitly provisional because an instantaneous indoor sample is not a
  24-hour regulatory monitor result.
- **Estimated EPA 24-hour category** is available only with at least 75% sample
  coverage, 20 hours of span, and 72 of the expected 96 aligned windows.
  Category names and boundaries are official; the result remains an indoor
  low-cost-sensor estimate, not an official regulatory AQI.

The rolling response exposes average, coverage percentage, expected and valid
sample counts, included-window count, oldest/newest timestamps, span, and an
explicit insufficient-data reason. Means are weighted by valid sample count,
which approximates time weighting for the regular 5-second stream.

Additional source: World Health Organization, **WHO Global Air Quality
Guidelines**, September 22, 2021. The PM2.5 24-hour guideline is 15 µg/m³ and the
PM10 24-hour guideline is 45 µg/m³ (99th percentile, 3–4 exceedance days/year).
WHO annual values—5 and 15 µg/m³ respectively—are documented but are never
mixed into the 24-hour display.

<https://www.who.int/publications/i/item/9789240034228>

### PM1.0 and PM4.0

No separate U.S. EPA AQI or WHO quantitative category is applied. PM1.0 is shown
as the very-fine subset of PM2.5 and PM4.0 as the SEN66-reported respirable
fraction. Status type: `informational_only`; official category: false. Raw trend,
15-minute mean, and maximum remain available.

### Carbon dioxide

ASHRAE's **Position Document on Indoor Carbon Dioxide, Ventilation and Indoor
Air Quality**, revised February 12, 2025, says Standard 62.1 does not establish
a universal indoor CO2 limit and that CO2 is, at best, an occupancy/ventilation
indicator when occupants are the dominant source. CDC/NIOSH's ventilation FAQ
describes below 800 ppm as one potential baseline target for good ventilation.

- 800 ppm or below: ventilation appears effective
- 801–1000 ppm: ventilation watch
- 1001–1500 ppm: ventilation recommended
- above 1500 ppm: strong ventilation recommended

These are conservative **dashboard heuristic** bands, not ASHRAE categories or
toxicity limits. Current, 15-minute mean, 15-minute maximum, and trend are shown.

<https://www.ashrae.org/file%20library/about/position%20documents/pd_indoorcarbondioxide_2022.pdf>

<https://www.cdc.gov/niosh/ventilation/faq/index.html>

Direct-exposure context is separate. CDC/NIOSH and OSHA list 5,000 ppm as an
occupational time-weighted value; NIOSH lists 30,000 ppm as a 15-minute STEL and
40,000 ppm as IDLH. A current value or 15-minute maximum is not mislabeled as an
8-hour TWA.

<https://www.cdc.gov/niosh/idlh/124389.html>

<https://www.osha.gov/annotated-pels/table-z-1>

### VOC Index

Source: Sensirion **SEN6x Datasheet D1 v0.92**, December 2025, and **What is
Sensirion's VOC Index?**, April 2022.

- dimensionless range: 1–500
- adaptive recent background: approximately 100
- below 100: less VOC activity than the learned recent background
- near 100: close to learned recent background
- above 100: increased activity relative to recent background
- Sensirion example device action: 150

The application bands below 150 are conservative presentation heuristics. They
are not exposure categories. The index is not ppm, ppb, µg/m³, or direct TVOC;
it cannot identify a chemical. Its gain adapts using roughly 24 hours of recent
history, so a sustained polluted environment can gradually become part of the
baseline. Raw index, peak, trend, difference from 100, difference from the prior
15-minute window, and duration at/above 150 are more important than the band.

| VOC Index | Display interpretation | Type |
|---:|---|---|
| 1–69 | Below learned recent background | dashboard presentation heuristic |
| 70–130 | Near learned recent background | dashboard presentation heuristic |
| 131–149 | Increased relative VOC activity | dashboard presentation heuristic |
| 150–500 | Sensirion example action level reached | manufacturer example threshold |

<https://sensirion.com/media/documents/FAFC548D/693FBB15/PS_DS_SEN6x.pdf>

<https://sensirion.com/media/documents/02232963/6294E043/Info_Note_VOC_Index.pdf>

### NOx Index

Source: the same Sensirion datasheet and **What is Sensirion's NOx Index?**,
April 2022.

- dimensionless range: 1–500
- normal baseline: near 1, not 100
- above 1: increasing oxidizing-gas activity
- Sensirion example device action: 20

The intermediate dashboard bands are heuristics, not regulatory exposure
categories. The value is not NO2 concentration and is never compared with EPA
or WHO NO2 concentration limits.

| NOx Index | Display interpretation | Type |
|---:|---|---|
| 1 | Near normal NOx Index baseline | manufacturer-defined baseline |
| 2–9 | NOx-related activity detected | dashboard presentation heuristic |
| 10–19 | Elevated relative NOx activity | dashboard presentation heuristic |
| 20–500 | Sensirion example action level reached | manufacturer example threshold |

<https://sensirion.com/media/documents/9F289B95/6294DFFC/Info_Note_NOx_Index.pdf>

### Temperature and relative humidity

Temperature uses a simple 20–26 °C comfort cue derived for this dashboard from
the multi-factor methods in ANSI/ASHRAE Standard 55-2023. Below/within/above are
comfort labels, never hazard claims. Clothing, activity, air speed, radiant
temperature, humidity, season, and preference limit this simplification.

<https://www.ashrae.org/technical-resources/bookstore/standard-55-thermal-environmental-conditions-for-human-occupancy>

EPA residential moisture guidance recommends below 60% RH and ideally 30–50%:

- below 30%: below ideal range
- 30–50%: within ideal range
- above 50% but below 60%: above ideal, below moisture-control ceiling
- 60% or above: above moisture-control recommendation

<https://www.epa.gov/mold/brief-guide-mold-moisture-and-your-home>

These are not filament-storage requirements. The SEN66's own recommended
operating conditions are 10–40 °C and 20–80% RH; short-term absolute operating
limits are -10–50 °C and 0–90% RH, non-condensing.

## Warm-up, Invalid, and Stale Data

Schema v2 publishes `boot_id`, `sensor_uptime_s`, and `reset_reason`. Sensor
uptime resets when firmware starts or when the driver reinitializes the SEN66,
so the server does not infer startup from a dashboard restart or MQTT reconnect.

Sensirion specifies:

- PM typical stable start: 30 seconds
- VOC raw event detection: under 60 seconds; VOC Index specifications: under 1 hour
- NOx raw event detection: under 300 seconds; NOx Index specifications: under 6 hours
- normal measured NOx value is unavailable for the first 10–11 seconds
- measured CO2 is unavailable for roughly 22–24 seconds after measurement start

VOC and NOx remain visible during their qualification periods, with a warm-up or
adaptation warning. Gas events are suppressed before Sensirion's reliable-event
times. A SEN66 reading is stale after 20 seconds by default—four missed expected
publishes. Missing, null, NaN, infinite, negative, out-of-range, invalid, and
stale values are unavailable, never zero or good.

When the sensor supplies a partial or out-of-range measured record, schema v2
publishes the affected JSON values as `null` with its uptime/status metadata.
This lets the bridge count and event the invalid sample without inventing a
number. A total transport/I2C failure still becomes stale if no packet can be
published. Firmware status bit 5 (nonzero SEN66 device-status register) also
marks the sample invalid and drives the same stateful `sensor_invalid` event.

## Raw Gas Diagnostics

Firmware now reads command `0x0405`. `sraw_voc` and `sraw_nox` are unsigned raw
ticks (0–65,535; 65,535 means unavailable), not concentrations. The dashboard
hides them under Advanced SEN66 diagnostics. Aggregates store optional mean,
minimum, and maximum. No conversion is performed.

## Long-term Aggregate Schema

Measurement: `air_quality_15m` in `environment`. Timestamp is `window_start` in
UTC. Tags: `location`, `topic`, `sensor_type`, and `node_id` when supplied.

Metadata fields:

- `window_start`, `window_end`
- `sample_count`, `valid_sample_count`, `invalid_sample_count`
- `expected_sample_count`, `data_coverage`, `is_partial`

Statistics:

- temperature and RH: mean
- CO2, PM1.0, PM2.5, PM4.0, PM10, NOx Index: mean and maximum
- VOC Index: mean, minimum, and maximum
- optional SRAW_VOC/SRAW_NOx: mean, minimum, and maximum
- p95: CO2, PM2.5, PM10, VOC Index, and NOx Index

Maxima preserve brief spikes that a mean would hide. Missing fields are omitted;
they are not zero-filled. Invalid packets increase `invalid_sample_count` but do
not enter statistics. A schema-v2 `(boot_id, sequence)` LRU prevents QoS
duplicates. Legacy packets without a boot ID are not de-duplicated by sequence
alone because their sequence resets can otherwise be mistaken for duplicates.
Unique out-of-order samples still in the active window are accepted; samples for
a closed window are logged and ignored.

The Pi receive clock is the timestamp authority because packets do not carry a
trusted wall-clock timestamp. A backward clock step into a closed window is
therefore handled as late; large forward steps close the observed window with
the resulting low coverage. Keep Pi time synchronized before starting services.

On SIGTERM the bridge writes the active window (`is_partial=true` only when its
UTC boundary has not elapsed) and upserts active event peak/count/last-observed
state without closing the episode. On startup
it first restores every event whose permanent point is still `state=active`,
then reads the previous 30 minutes from `environment_live`, reconstructs current
and just-completed windows, replays event transitions, and upserts the same
aggregate/event timestamps. A trigger older than the live recovery window
therefore remains one continuous event. This makes partial/restart behavior
deterministic while keeping live storage bounded. After an ungraceful power loss,
the trigger/start identity is still recovered, but a peak and sample count that
occurred before the bounded replay horizon and after the last persisted active
snapshot cannot be reconstructed; normal completion and graceful SIGTERM keep
the full tracked state.

## Event Schema and Thresholds

Measurement: `air_quality_event` in `environment`. Trigger and completion update
the same measurement/tag/timestamp identity, so sustained conditions do not
create one point per sample. Fields include threshold, clear threshold, trigger,
peak, baseline where available, preceding-window value, start/end, duration,
sample count, last-observed time, evaluated window, severity, source framework,
status type, and state. VOC also stores difference from 100 and from the
preceding window.

| Event | Trigger | Clear | Basis |
|---|---:|---:|---|
| VOC action | 150 | 130 | Sensirion example trigger; clear is hysteresis heuristic |
| NOx action | 20 | 15 | Sensirion example trigger; clear is hysteresis heuristic |
| PM2.5 current level | 35.5 µg/m³ | 30 | EPA boundary used provisionally |
| PM10 current level | 155 µg/m³ | 140 | EPA boundary used provisionally |
| CO2 ventilation | 1000 ppm | 900 | dashboard ventilation heuristic |
| VOC rapid rise | +50 | +25 | dashboard change heuristic |
| NOx rapid rise | +10 | +5 | dashboard change heuristic |
| PM2.5 rapid rise | +15 µg/m³ | +7.5 | dashboard change heuristic |
| PM10 rapid rise | +30 µg/m³ | +15 | dashboard change heuristic |
| CO2 rapid rise | +200 ppm | +100 | dashboard change heuristic |

Cooldown is 15 minutes for pollution rules. `sensor_invalid` is a separate
trigger/clear event with a 60-second cooldown. Absolute crossings evaluate the
latest accepted five-second publish; rapid-rise deltas compare consecutive
accepted publishes (normally five seconds apart), not a concentration rate or
regulatory averaging period.
Sparse event markers are points and are not connected as if peaks persisted.

## Retention and Compatibility

- `environment_live`: 72 hours; high-resolution `air_quality_reading`
- `environment`: infinite by default; `air_quality_15m`, `air_quality_event`,
  existing environmental nodes, and all pre-migration SEN66 history

The setup script creates the live bucket and new scoped tokens. It does not
delete or move existing measurements, and it refuses an unexpected retention
policy unless the operator explicitly requests repair. The maintained
`migrations.backfill_air_quality_15m` utility reconciles legacy raw history before
long-range queries switch entirely to aggregates. The migration boundary can
contain one partial-coverage window; coverage metadata makes that visible.

After verified backfill, one-hour queries read only `environment_live` and
longer queries read only `environment/air_quality_15m`. Legacy permanent raw
data is ignored by the application and remains available for rollback until a
separate, bounded cleanup is explicitly approved. See `docs/INFLUXDB.md` for the
dry-run, write, verify, cleanup, and rollback commands.

## Data-volume Estimate per SEN66 Station

The storage engine's exact on-disk overhead, compression, WAL behavior, tag
cardinality, and compaction are deployment-specific, so these are point and field
value estimates rather than exact bytes.

| Path | Day | 30-day month | Year | Retention |
|---|---:|---:|---:|---|
| Before: raw 5-second long-term points | 17,280 | 518,400 | 6,307,200 | infinite |
| Proposed live 5-second points | 17,280 | bounded | bounded | 72 hours (51,840 points steady-state) |
| Proposed 15-minute long-term points | 96 | 2,880 | 35,040 | infinite |
| Example events at 5/day | 5 | 150 | 1,825 | infinite |

Base long-term point count falls about **99.44%**. Before, nine primary fields
produced about 155,520 field values/day. A 15-minute aggregate has about 30 core
fields (up to 36 with raw-gas statistics), or about 2,880–3,456 field values/day:
roughly **97.8–98.1% fewer long-term field values**, before events. Live writes
remain responsive but no longer accumulate indefinitely.

## Configuration

Backend `.env` settings:

```text
INFLUXDB_BUCKET=environment
INFLUXDB_LIVE_BUCKET=environment_live
INFLUXDB_LIVE_RETENTION=72h
SEN66_EXPECTED_PUBLISH_SECONDS=5
SEN66_STALE_AFTER_SECONDS=20
SEN66_24H_MINIMUM_COVERAGE_PERCENT=75
SEN66_RECOVERY_LOOKBACK_MINUTES=30
```

Interpretations and event thresholds live in
`server/backend/app/air_quality_policy.py`. Aggregation mechanics live in
`server/backend/bridge/air_quality_pipeline.py`.

## Test and Build

From the repository root:

```bash
python3 -m venv /tmp/sensor-home-test-venv
/tmp/sensor-home-test-venv/bin/pip install -r server/requirements.txt
PYTHONPATH=server/backend /tmp/sensor-home-test-venv/bin/python \
  -m unittest discover -s server/backend/tests -v
python3 -m unittest discover -s home-assistant/discovery/tests -v
node --check server/frontend/static/app.js
python3 -m json.tool server/config/grafana/dashboards/home-sensor-environment.json >/dev/null
```

Firmware validation with ESP-IDF installed:

```bash
cd esp/ESP32C3_SEN66_air_quality
idf.py build
```

## Raspberry Pi Migration and Redeployment

Use the existing installer; it preserves `/opt/home-sensor/server/backend/.env`,
the virtual environment, InfluxDB data, Grafana data, Mosquitto configuration,
and service names.

From the development machine:

```bash
ssh pi@sensor-pi.local 'mkdir -p /tmp/home-sensor-server-update'
rsync -a --exclude 'backend/.env' --exclude 'backend/.venv' \
  server/ pi@sensor-pi.local:/tmp/home-sensor-server-update/
ssh pi@sensor-pi.local
cd /tmp/home-sensor-server-update
sudo ./install.sh --project-root /opt/home-sensor/server --no-frontend-assets
```

Create the additional bucket and replace application tokens with scopes for both
buckets. Supply the existing InfluxDB admin credentials; the admin token is not
stored in `backend/.env`:

```bash
sudo env \
  INFLUXDB_ADMIN_PASSWORD='<existing-admin-password>' \
  INFLUXDB_ADMIN_TOKEN='<existing-admin-token>' \
  /opt/home-sensor/server/scripts/setup_influxdb.sh \
    --bucket environment --retention 0 \
    --live-bucket environment_live --live-retention 72h
```

Provision the updated Grafana query and restart the existing application units:

```bash
sudo /opt/home-sensor/server/scripts/provision_grafana.sh
sudo systemctl restart home-sensor-bridge.service home-sensor-dashboard.service
sudo systemctl status home-sensor-bridge.service home-sensor-dashboard.service --no-pager
sudo journalctl -u home-sensor-bridge.service -u home-sensor-dashboard.service \
  --since '15 minutes ago' --no-pager
```

MQTT and end-to-end verification:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 \
  -u home_sensor_bridge -P '<bridge-password>' -t 'home/air/#' -v
MQTT_PUBLISH_PASSWORD='<gateway-password>' \
  /opt/home-sensor/server/scripts/verify_sen66.sh
/opt/home-sensor/server/scripts/verify_influxdb.sh
/opt/home-sensor/server/scripts/verify_api.sh
```

Inspect storage with the read token already in the preserved environment file:

```bash
READ_TOKEN="$(sudo sed -n 's/^INFLUXDB_READ_TOKEN=//p' /opt/home-sensor/server/backend/.env)"

influx query --host http://127.0.0.1:8086 --org home --token "${READ_TOKEN}" \
  'from(bucket:"environment_live") |> range(start:-5m) |> filter(fn:(r) => r._measurement == "air_quality_reading") |> last()'

influx query --host http://127.0.0.1:8086 --org home --token "${READ_TOKEN}" \
  'from(bucket:"environment") |> range(start:-2h) |> filter(fn:(r) => r._measurement == "air_quality_15m") |> sort(columns:["_time"], desc:true) |> limit(n:40)'

influx query --host http://127.0.0.1:8086 --org home --token "${READ_TOKEN}" \
  'from(bucket:"environment") |> range(start:-24h) |> filter(fn:(r) => r._measurement == "air_quality_event") |> sort(columns:["_time"], desc:true)'

curl --silent --show-error http://127.0.0.1:8080/api/latest | python3 -m json.tool
curl --silent --show-error \
  'http://127.0.0.1:8080/api/readings?range=24h&sensor_type=air_quality&location=office' \
  | python3 -m json.tool
```

Wait through the next UTC quarter-hour and confirm a completed aggregate has
`is_partial=false`, nonzero `sample_count`, plausible coverage, means, and maxima.
In the dashboard, verify all nine values, authority labels, source details,
last-update age, warm-up/stale text, 15-minute context, and distinct mean/max/event
legend entries.
