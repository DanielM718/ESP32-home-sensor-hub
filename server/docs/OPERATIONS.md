# Operations

Operational docs will be expanded as services are added. The target operating
model is:

- systemd manages the bridge and dashboard
- logs are viewed with `journalctl`
- Mosquitto receives authenticated gateway messages
- InfluxDB stores all historical data
- Grafana provides primary analytics
- Flask provides a lightweight current-state view and REST API

## Health Checks

Milestone 2 and later milestones will add install and verification scripts.
The final verification path will check:

- service user exists
- virtual environment exists
- environment file permissions are restricted
- Mosquitto auth config is present
- InfluxDB is reachable
- bridge service can connect to MQTT and InfluxDB
- dashboard health endpoint responds locally
- Grafana datasource is configured
- Tailscale status is available

Run the complete verification suite as root:

```bash
sudo /opt/home-sensor/server/scripts/verify_all.sh
```

## Bridge Smoke Test

After Mosquitto and InfluxDB are configured, start the bridge on the Pi:

```bash
sudo systemctl restart home-sensor-bridge.service
sudo journalctl -u home-sensor-bridge.service -f
```

Publish a test payload as the gateway user:

```bash
mosquitto_pub -h 127.0.0.1 -p 1883 -u home_sensor_gateway -P '<gateway-password>' -t 'home/sensors/1' -m '{"node_id":1,"sequence":1,"temperature_c":24.8,"humidity":41.6,"battery_mv":4058,"status_flags":4}'
```

Expected result: the bridge logs a successful write at debug level, or no warning
at info level. The data should appear in the InfluxDB `environment` bucket as an
`environment_reading`.

For the full SEN66 path, first watch the air topics:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 \
  -u home_sensor_bridge -P '<bridge-password>' \
  -t 'home/air/#' -v
```

Then use the checked-in full payload and wait for all nine fields to reach the
API:

```bash
MQTT_PUBLISH_PASSWORD='<gateway-password>' \
  /opt/home-sensor/server/scripts/verify_sen66.sh
```

The script publishes to `home/air/sen66_test` by default. It verifies
temperature, humidity, CO2, all four PM sizes, VOC Index, and NOx Index. Check
both Python services if it fails:

```bash
sudo journalctl -u home-sensor-bridge.service --since '10 minutes ago' --no-pager
sudo journalctl -u home-sensor-dashboard.service --since '10 minutes ago' --no-pager
```

The base verification script is:

```bash
/opt/home-sensor/server/scripts/verify_install.sh
```

It checks the service user, deployment root, virtual environment, `.env`
permissions, and systemd unit installation.

After Mosquitto setup, run:

```bash
/opt/home-sensor/server/scripts/verify_mqtt.sh
```

It checks the broker/client commands, installed config, installed ACL, password
file permissions, and expected MQTT users in the ACL.

After InfluxDB setup, run:

```bash
/opt/home-sensor/server/scripts/verify_influxdb.sh
```

It checks the `influx` and `influxd` commands, InfluxDB ping, systemd service
registration, and access to both the configured long-term and live buckets.
SEN66 high-resolution points appear in `environment_live`; wait through a UTC
quarter-hour to verify `air_quality_15m` in `environment`, or follow the exact
queries in [`SEN66_AIR_QUALITY.md`](SEN66_AIR_QUALITY.md).

For an upgrade with legacy permanent SEN66 raw data, complete the dry-run,
backup, bounded write, verification-only run, and second idempotency dry-run in
[`INFLUXDB.md`](INFLUXDB.md) before considering raw cleanup. Cleanup always
requires a separately reviewed start, stop, predicate, count estimate, verified
aggregate coverage, rollback backup, and explicit approval.

## Dashboard API Smoke Test

After the Flask API milestone is installed on the Pi:

```bash
sudo systemctl restart home-sensor-dashboard.service
curl http://127.0.0.1:8080/api/health
curl http://127.0.0.1:8080/api/latest
curl 'http://127.0.0.1:8080/api/readings?range=24h'
curl http://127.0.0.1:8080/api/nodes
```

Expected result: `/api/health` returns `{"status":"ok"}` and the data endpoints
return JSON from InfluxDB.

If the dashboard briefly reports `backend query failed`, the Flask process is
still responsive enough to return an HTTP 503; this message alone does not mean
the server is hung. Identify the failing endpoint and its duration with:

```bash
for path in api/health api/latest 'api/readings?range=24h' api/nodes api/workflows/options api/status; do
  curl --silent --show-error --output /dev/null \
    --write-out "${path} HTTP %{http_code} in %{time_total}s\n" \
    "http://127.0.0.1:8080/${path}"
done
sudo journalctl -u home-sensor-dashboard.service --since '10 minutes ago' --no-pager
sudo systemctl show home-sensor-dashboard.service \
  --property=ActiveState,SubState,NRestarts,MemoryCurrent,CPUUsageNSec
```

The browser error now includes the endpoint that failed. The journal contains
the underlying InfluxDB exception and traceback. A fast `/api/health` response
with a slow or failing data endpoint indicates an InfluxDB/query problem rather
than a hung Gunicorn process.

The same checks are wrapped in:

```bash
/opt/home-sensor/server/scripts/verify_api.sh
```

The browser dashboard is available at:

```text
http://sensor-pi.local:8080
```

If charts do not render, verify that the Chart.js asset exists:

```bash
ls -l /opt/home-sensor/server/frontend/static/vendor/chart.umd.min.js
```

## Monitoring And Export Operations

The durable runtime paths are:

```text
/var/lib/home-sensor/monitoring.sqlite3
/var/lib/home-sensor/exports/<job-id>.csv
```

The installer creates both directories as `home-sensor:home-sensor` mode
`0700`, preserves existing database/CSV files, and never copies them into or
out of the Git deployment tree. SQLite uses WAL, foreign keys, a five-second
busy timeout, parameterized statements, and a fresh connection per Flask/worker
operation. The two Gunicorn processes can safely read/write metadata while the
separate worker transactionally claims one heavy job at a time.

Schema version 2 expands the resolution constraints to `raw`, `1m`, `5m`,
`15m`, and `1h`. Initialization migrates version 1 transactionally while a
small schema lock serializes dashboard/worker startup; existing sessions,
jobs, and CSVs are preserved. The tables contain:

- `monitoring_sessions`: UUID, name/notes/status, start/scheduled/actual UTC
  times, selected-source/field JSON, resolution/format, unique automatic job
  ID, and created/updated times.
- `export_jobs`: UUID, unique optional monitoring-session ID, name/status,
  requested UTC interval, selected-source/field JSON, resolution/format,
  internal output path, bytes/rows/work-unit progress, phase, warning/source
  result JSON, bounded error, worker/attempt/heartbeat lease metadata, and
  created/started/completed/updated times.

The `export_jobs.monitoring_session_id` uniqueness constraint is the final
database-level guard against duplicate automatic jobs.

Check the worker and API:

```bash
sudo systemctl status home-sensor-export-worker.service --no-pager
sudo journalctl -u home-sensor-export-worker.service --since '30 minutes ago' --no-pager
curl -fsS http://127.0.0.1:8080/api/monitoring/sessions | python3 -m json.tool
curl -fsS http://127.0.0.1:8080/api/exports | python3 -m json.tool
```

The worker polls for queued jobs, claims the oldest with `BEGIN IMMEDIATE` plus
a status-guarded update, and maintains a lease heartbeat. The claim transaction
will not admit another queued job while any job is running or awaiting
cancellation, so even accidentally duplicated worker processes still perform
at most one heavy export at a time. SQLite ownership checks also prevent two
workers from completing the same claim.

Each export starts from the beginning of its exact `[start, stop)` interval,
uses streaming InfluxDB records and bounded time chunks, writes
`<id>.csv.part`, flushes/fsyncs it, atomically renames it to `<id>.csv`, and only
then marks the database row completed. Raw chunks default to one hour and
15-minute-tier chunks to one day. Chunks shrink automatically with station and
field count toward about 50,000 measurement cells per sort (minimum five
minutes raw or one hour aggregate), keeping memory bounded on the Pi. Progress
is updated per meaningful query/chunk rather than per row.

Cancellation is cooperative. A queued job becomes cancelled immediately. A
running job becomes `cancel_requested`; the worker checks while consuming the
stream and between source-type queries/chunks, removes `.part`, and stores
`cancelled`. There is no unsafe thread termination. A completed final file is
never served under its `.part` name.

### Restart Recovery

On SIGTERM the worker stops between bounded operations, removes its partial
file, and atomically requeues its owned running job (or finalizes a pending
cancellation). After a crash/power loss, the restarted worker's periodic
recovery pass finds running leases older than `EXPORT_WORKER_LEASE_SECONDS`
(default five minutes), removes
stale `.part` and any final file created before the completion transaction, and
requeues the job from the beginning. A stale `cancel_requested` job becomes
cancelled. Completed database rows and their final files are untouched. Thus an
interrupted job is never inferred completed and no output is appended twice.

The dashboard, worker, or a session read can reconcile deadlines, but the
unique job/session relationship means repeated polling or multiple browser tabs
cannot create a second automatic job. Restarting Gunicorn never resets stored
session start/deadline state.

### Backup And Restore

For a consistent metadata backup without stopping sensor ingestion, stop only
the dashboard and export worker, copy the database, then restart them. The MQTT
bridge and InfluxDB remain online:

```bash
sudo systemctl stop home-sensor-dashboard.service home-sensor-export-worker.service
sudo cp --preserve=mode,ownership,timestamps \
  /var/lib/home-sensor/monitoring.sqlite3 \
  /var/lib/home-sensor/monitoring.sqlite3.backup-YYYYMMDDTHHMMSSZ
sudo systemctl start home-sensor-export-worker.service home-sensor-dashboard.service
```

Back up completed CSVs with the normal host backup tool while preserving the
`/var/lib/home-sensor/exports` directory. To restore, stop those same two
services, place the reviewed database/exports back at the configured paths,
set ownership `home-sensor:home-sensor`, modes `0600` for the database and
`0700` for the directory, then start worker followed by dashboard. Do not
restore a database and unrelated export directory independently unless missing
downloads are acceptable.

### Cleanup And Retention

There is no invisible automatic expiry. Delete a finished manual job through
`DELETE /api/exports/<id>`; delete a finished monitoring session through
`DELETE /api/monitoring/sessions/<id>`, which also removes its automatic job and
files. Active jobs must first be cancelled and reach a final state. Operators
should periodically review disk use:

```bash
sudo du -sh /var/lib/home-sensor/exports
sudo find /var/lib/home-sensor/exports -maxdepth 1 -type f -printf '%TY-%Tm-%Td %s %f\n' | sort
```

Use the API for normal cleanup. For a manually identified orphan, verify that
its UUID is absent from `/api/exports`, then remove only that exact file; never
recursively erase `/var/lib/home-sensor` during an upgrade.

### Troubleshooting

- `queued` indefinitely: check the export-worker unit/log and its InfluxDB read
  token; do not increase Gunicorn request timeouts.
- `running` with an old heartbeat: restart only the export worker and allow the
  documented lease recovery pass.
- completed but download returns 409: the final file is missing; inspect backup
  state and logs. The API intentionally does not regenerate it.
- header-only completed file: inspect `warnings` and `source_results`; this is a
  valid zero-data outcome, especially for expired raw SEN66 intervals.
- `database is locked`: confirm only the configured services use the database,
  ownership permits WAL sidecars, and storage is local rather than a network
  filesystem.
- unexpected `.part`: check job state and heartbeat before intervening. Stale
  parts belonging to recovered jobs are removed automatically.

## Grafana Smoke Test

After Grafana provisioning:

```bash
sudo systemctl restart grafana-server.service
/opt/home-sensor/server/scripts/verify_grafana.sh
```

Then open:

```text
http://sensor-pi.local:3000
```

Expected result: Grafana loads with the `Home Sensor Environment` dashboard
under the `Home Sensor` folder.

## Tailscale Smoke Test

After Tailscale setup:

```bash
/opt/home-sensor/server/scripts/verify_tailscale.sh
tailscale status
tailscale ip -4
```

From another Tailnet device, open:

```text
http://sensor-pi:8080
http://sensor-pi:3000
```

If MagicDNS is not enabled, use the Pi's Tailscale IP instead of `sensor-pi`.

## systemd Services

Milestone 2 adds:

- `home-sensor-bridge.service`
- `home-sensor-dashboard.service`

The services run as `home-sensor` and load environment variables from:

```text
/opt/home-sensor/server/backend/.env
```

Their working directory is:

```text
/opt/home-sensor/server/backend
```

After the later configuration milestones are complete, typical service commands
on the Raspberry Pi are:

```bash
sudo systemctl daemon-reload
sudo systemctl enable home-sensor-bridge.service home-sensor-dashboard.service
sudo systemctl start home-sensor-bridge.service home-sensor-dashboard.service
sudo journalctl -u home-sensor-bridge.service -f
sudo journalctl -u home-sensor-dashboard.service -f
```

Official systemd references:

- <https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html>
- <https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html>
