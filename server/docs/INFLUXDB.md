# InfluxDB Design

This backend targets InfluxDB OSS v2.

Official references:

- <https://docs.influxdata.com/influxdb/v2/install/>
- <https://docs.influxdata.com/influxdb/v2/api-guide/client-libraries/python/>
- <https://docs.influxdata.com/influxdb/v2/reference/cli/influx/setup/>
- <https://docs.influxdata.com/influxdb/v2/reference/cli/influx/bucket/create/>
- <https://docs.influxdata.com/influxdb/v2/reference/cli/influx/bucket/list/>
- <https://docs.influxdata.com/influxdb/v2/reference/cli/influx/auth/create/>
- <https://docs.influxdata.com/influxdb/v2/write-data/best-practices/schema-design/>

## Default Settings

```text
Organization: home
Long-term bucket: environment
Live bucket: environment_live
URL: http://127.0.0.1:8086
Long-term retention: 0 (infinite)
Live retention: 72h
```

Scoped application tokens are stored in `server/backend/.env` on the Raspberry
Pi:

- `INFLUXDB_WRITE_TOKEN`: write-only token for the MQTT bridge
- `INFLUXDB_READ_TOKEN`: read-only token for Flask and Grafana
- `INFLUXDB_TOKEN`: compatibility alias for the write token

The InfluxDB admin token is used only during setup and should be stored in a
password manager, not committed.

## Raspberry Pi Setup

After the base installer has copied the project to `/opt/home-sensor/server`,
install InfluxDB on the Pi:

```bash
sudo /opt/home-sensor/server/scripts/install_influxdb.sh
```

Then initialize the org, bucket, and scoped tokens:

```bash
sudo /opt/home-sensor/server/scripts/setup_influxdb.sh
```

The setup script prompts for an admin password if `INFLUXDB_ADMIN_PASSWORD` is
not exported. If `INFLUXDB_ADMIN_TOKEN` is not exported, the script generates
one and prints it once so it can be stored in a password manager.

The script passes setup values to the `influx` CLI. Run it from a trusted local
Pi shell, not over an untrusted shared terminal session.

Use explicit settings when needed:

```bash
sudo /opt/home-sensor/server/scripts/setup_influxdb.sh \
  --url http://127.0.0.1:8086 \
  --org home \
  --bucket environment \
  --retention 0 \
  --live-bucket environment_live \
  --live-retention 72h
```

Existing bucket retention is verified on every setup run. A mismatch stops the
script without changing data. After backup and review, pass
`--repair-existing-retention` to make the explicitly requested correction.

Verify the setup:

```bash
sudo /opt/home-sensor/server/scripts/verify_influxdb.sh
```

## Measurements

Measurements:

- `environment/environment_reading`: long-term SHT41 node samples
- `environment_live/air_quality_reading`: high-resolution SEN66 samples
- `environment/air_quality_15m`: long-term SEN66 aggregates
- `environment/air_quality_event`: long-term sparse event episodes
- `environment/air_quality_reading`: legacy raw SEN66 history, retained only
  until aggregate backfill is verified and a separately approved cleanup occurs

Tags are used for low-cardinality dimensions:

- `node_id`
- `topic`
- `sensor_type`
- `location`

Fields are used for measured values:

- `temperature_c`
- `humidity`
- `battery_mv`
- `status_flags`
- `sequence`
- `co2`
- `pm1`
- `pm25`
- `pm4`
- `pm10`
- `voc_index`
- `nox_index`

One-hour API history reads live raw data and downsamples it to one-minute display
points. The 24-hour, 7-day, and 30-day paths read only `air_quality_15m` after
historical reconciliation. Creating the live bucket and running the backfill do
not delete existing data. The bridge writes both aggregate/event data and the
live tier; dashboard/Grafana credentials can read both buckets.

## Historical Backfill and Verification

The maintained migration uses the same aggregation implementation as the live
bridge. It reads both raw source buckets in bounded aligned batches, ignores the
current incomplete window, compares every logical location/window with existing
aggregates, and is safe to interrupt and rerun.

Run a read-only assessment first:

```bash
cd /opt/home-sensor/server/backend
sudo -u home-sensor .venv/bin/python -m migrations.backfill_air_quality_15m \
  --dry-run --batch-hours 24
```

Use explicit aligned bounds and an optional location when narrowing work:

```bash
sudo -u home-sensor .venv/bin/python -m migrations.backfill_air_quality_15m \
  --dry-run \
  --start 2026-07-19T18:45:00Z \
  --end 2026-07-22T00:00:00Z \
  --location office --batch-hours 6
```

Before the first write, take an InfluxDB backup outside the repository. Then
write only missing windows; add `--repair` only after reviewing reported
incomplete or malformed single-window identities:

```bash
sudo influx backup /var/backups/home-sensor/influx-before-sen66-backfill
sudo -u home-sensor .venv/bin/python -m migrations.backfill_air_quality_15m \
  --write --start START_UTC --end END_UTC --batch-hours 24
sudo -u home-sensor .venv/bin/python -m migrations.backfill_air_quality_15m \
  --verify-only --start START_UTC --end END_UTC --batch-hours 24
```

Verification exits nonzero while any recoverable raw window remains missing or
malformed. A final dry run should report zero writes required. The utility never
queries or writes `air_quality_event`.

## Permanent Legacy Raw Cleanup

Cleanup is optional and never required for API performance. First record the
exact earliest/latest raw timestamps, point estimate, verified aggregate
coverage, and backup path. The only permitted deletion predicate is:

```text
bucket: environment
measurement: air_quality_reading
start: VERIFIED_INCLUSIVE_START_UTC
stop: VERIFIED_EXCLUSIVE_STOP_UTC
predicate: _measurement="air_quality_reading"
```

After explicit operator approval, the bounded command is:

```bash
influx delete --host http://127.0.0.1:8086 --org home \
  --bucket environment \
  --start VERIFIED_INCLUSIVE_START_UTC \
  --stop VERIFIED_EXCLUSIVE_STOP_UTC \
  --predicate '_measurement="air_quality_reading"' \
  --token "$INFLUXDB_ADMIN_TOKEN"
```

Never broaden the predicate or substitute the live bucket. Roll back application
code by restoring the per-deployment source backup and restarting only the
bridge/dashboard. Aggregate backfill writes are additive idempotent upserts; if
database rollback is genuinely required, stop writers and restore the named
pre-backfill InfluxDB backup under a maintenance window rather than deleting a
guess at timestamps.

For `environment_reading`, `status_flags` is the raw unsigned SHT41 status
integer. Its battery bits are `BIT2` measurement valid, `BIT3` low battery, and
`BIT4` confirmed shutdown. The field may be absent on historical points. The
bridge writes `battery_mv` only for packets with `BIT2` set, so absence means
unavailable; a missing field is not a zero-volt measurement. Unknown status
bits remain stored in the raw integer. Historical queries pair `battery_mv`
with the same point's `status_flags` and include it only when a bitwise `BIT2`
test succeeds; existing rows are not rewritten.

See also:

- `server/config/influxdb/schema.md`
- `server/config/influxdb/README.md`
- `server/docs/BRIDGE.md`
- `server/docs/API.md`
- `server/docs/GRAFANA.md`
- `server/docs/SEN66_AIR_QUALITY.md`
