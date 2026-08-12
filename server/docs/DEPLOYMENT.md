# Deployment Model

The primary deployment target is a native Raspberry Pi OS Lite 64-bit
installation. This repository generates the files needed for deployment, but
commands are intended to run on the Raspberry Pi, not on the Mac development
machine.

## Native Deployment Responsibilities

The native deployment will:

- create the `home-sensor` Linux user
- create `server/backend/.venv`
- install Python dependencies inside that virtual environment
- configure Mosquitto with authentication
- configure InfluxDB OSS v2
- install systemd units for the Python bridge and dashboard
- provision Grafana dashboards and datasource definitions where practical
- document Tailscale-only remote access

Milestone 2 adds the base installer and service templates. The installer is
intended to run only on the Raspberry Pi:

```bash
cd /path/to/copied/sensor_home/server
sudo ./install.sh
```

The installer performs base setup only:

- verifies it is running on Linux
- installs base Raspberry Pi OS packages required for Python deployment
- creates the `home-sensor` service user
- copies the server project to `/opt/home-sensor/server` when needed
- creates `server/backend/.venv`
- installs Python dependencies into that virtual environment
- installs the Chart.js browser bundle for the frontend
- creates `server/backend/.env` from the example if missing
- installs systemd unit files
- creates and preserves `/var/lib/home-sensor` plus its `exports` directory

It does not configure Mosquitto, InfluxDB, Grafana, or Tailscale yet. Later
milestones add those files and instructions.

Milestone 3 adds Mosquitto package/configuration scripts:

```bash
sudo /opt/home-sensor/server/scripts/install_mosquitto.sh
sudo /opt/home-sensor/server/scripts/create_mqtt_users.sh
```

Run those on the Raspberry Pi after the base installer and before starting the
Python bridge.

Milestone 4 adds InfluxDB package/setup scripts:

```bash
sudo /opt/home-sensor/server/scripts/install_influxdb.sh
sudo /opt/home-sensor/server/scripts/setup_influxdb.sh
sudo /opt/home-sensor/server/scripts/verify_influxdb.sh
```

Run these on the Raspberry Pi before starting the Python bridge or dashboard.

Milestone 8 adds Grafana package/provisioning scripts:

```bash
sudo /opt/home-sensor/server/scripts/install_grafana.sh
sudo /opt/home-sensor/server/scripts/provision_grafana.sh
sudo /opt/home-sensor/server/scripts/verify_grafana.sh
```

Run these after InfluxDB setup has populated `INFLUXDB_READ_TOKEN` in
`/opt/home-sensor/server/backend/.env`.

Milestone 9 adds Tailscale install/verification scripts:

```bash
sudo /opt/home-sensor/server/scripts/install_tailscale.sh --hostname sensor-pi
/opt/home-sensor/server/scripts/verify_tailscale.sh
```

Use Tailscale for remote access. Do not configure router port forwarding or
Tailscale Funnel for this backend.

## Runtime Paths

The default deployment root is:

```text
/opt/home-sensor/server
```

The Python virtual environment is:

```text
/opt/home-sensor/server/backend/.venv
```

The secret environment file is:

```text
/opt/home-sensor/server/backend/.env
```

Persistent application data is outside the deployment root:

```text
/var/lib/home-sensor/monitoring.sqlite3
/var/lib/home-sensor/exports/
```

The installer creates/preserves these paths as `home-sensor:home-sensor` with
restrictive permissions. `rsync` upgrades never target them. The monitored
SQLite database, completed exports, `.env`, virtual environment, InfluxDB data,
and Grafana data are not erased by a normal installer run.

## Installer Options

```bash
sudo ./install.sh --project-root /opt/home-sensor/server --service-user home-sensor
```

Useful flags:

- `--no-enable-services`: install unit files without enabling them
- `--no-frontend-assets`: skip Chart.js browser bundle download
- `--start-services`: start services after installing unit files

Do not use `--start-services` until Mosquitto, InfluxDB, and the backend
environment file are configured.

## Python Environment

The installer uses Python's `venv` module to create:

```text
/opt/home-sensor/server/backend/.venv
```

Packages are installed with the virtual environment's `pip`, never with
`sudo pip` and never into the system Python environment.

The generated base package script installs these Raspberry Pi OS/Debian
packages:

- `ca-certificates`
- `curl`
- `python3`
- `python3-pip`
- `python3-venv`
- `rsync`

Official reference:

- <https://docs.python.org/3/library/venv.html>

## Hostname And Access

LAN access:

```text
http://sensor-pi.local:8080
http://<raspberry-pi-ip>:8080
```

Remote access must use Tailscale. Do not forward ports from the public internet
to Mosquitto, InfluxDB, Grafana, or Flask.

## Redeploy The SEN66 Data-Pipeline Update

The active native deployment is `/opt/home-sensor/server`. The repository's
`install.sh` is the authoritative deployment method: it copies the `server/`
tree into that path while preserving `backend/.env` and `backend/.venv`, updates
the virtual environment from `requirements.txt`, and installs the four Python
systemd units. The unit files confirm these service names and runtime split:

- `home-sensor-bridge.service`: MQTT subscriptions and InfluxDB writes
- `home-sensor-dashboard.service`: Gunicorn, Flask API, and static dashboard
- `home-sensor-export-worker.service`: persistent SQLite/InfluxDB CSV worker
- `home-sensor-printer-observer.service`: optional GET-only HA/cloud printer
  observer, conditioned on its root-controlled configuration files
- `mosquitto.service`, `influxdb.service`, and `grafana-server.service`: separate
  platform services; the Grafana dashboard is reprovisioned after this update

From the repository root on the development machine, copy the current server
tree to the Pi:

```bash
ssh pi@sensor-pi.local 'mkdir -p /tmp/home-sensor-server-update'
rsync -a \
  --exclude 'backend/.env' \
  --exclude 'backend/.venv' \
  server/ pi@sensor-pi.local:/tmp/home-sensor-server-update/
```

Then install from the copy on the Pi. `--no-frontend-assets` keeps the existing
Chart.js bundle because this update does not change that vendor dependency:

```bash
ssh pi@sensor-pi.local
cd /tmp/home-sensor-server-update
sudo ./install.sh \
  --project-root /opt/home-sensor/server \
  --no-frontend-assets
```

No Python dependency version changed, but the installer still runs the exact
project-supported dependency update through
`/opt/home-sensor/server/scripts/bootstrap_python.sh`. If files were instead
updated directly inside the active path, run that step explicitly:

```bash
sudo /opt/home-sensor/server/scripts/bootstrap_python.sh
```

Create the bounded live bucket and replace the application tokens with scopes
for both buckets. The existing long-term bucket and data are preserved:

```bash
sudo env \
  INFLUXDB_ADMIN_PASSWORD='<existing-admin-password>' \
  INFLUXDB_ADMIN_TOKEN='<existing-admin-token>' \
  /opt/home-sensor/server/scripts/setup_influxdb.sh \
    --bucket environment --retention 0 \
    --live-bucket environment_live --live-retention 72h
sudo /opt/home-sensor/server/scripts/provision_grafana.sh
```

Both application services must restart because the bridge now owns live writes,
aggregation, event detection, and restart recovery:

```bash
sudo systemctl restart home-sensor-bridge.service home-sensor-export-worker.service home-sensor-dashboard.service
sudo systemctl status home-sensor-bridge.service home-sensor-export-worker.service home-sensor-dashboard.service --no-pager
```

Do not restart Mosquitto or InfluxDB. Follow the affected application logs while running
the MQTT test:

```bash
sudo journalctl -u home-sensor-dashboard.service -f
sudo journalctl -u home-sensor-bridge.service -f
sudo journalctl -u home-sensor-export-worker.service -f
```

In a separate terminal, watch the broker and run the full synthetic test:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 \
  -u home_sensor_bridge -P '<bridge-password>' \
  -t 'home/air/#' -v

MQTT_PUBLISH_PASSWORD='<gateway-password>' \
  /opt/home-sensor/server/scripts/verify_sen66.sh
```

Confirm the API response directly:

```bash
curl --silent --show-error http://127.0.0.1:8080/api/latest \
  | python3 -m json.tool
curl --silent --show-error \
  'http://127.0.0.1:8080/api/readings?range=1h&sensor_type=air_quality&location=sen66_test' \
  | python3 -m json.tool
/opt/home-sensor/server/scripts/verify_api.sh
```

Open the dashboard over the LAN at `http://sensor-pi.local:8080` or
`http://<raspberry-pi-ip>:8080`.

### Rollback

The default installer copies files into `/opt/home-sensor/server`, so rollback
the source Git checkout and deploy it again rather than running Git commands in
the active deployment directory. From the source checkout, inspect the recent
history and create a revert commit for the bad deployment:

```bash
cd /path/to/sensor_home
git status
git log --oneline -5
git revert --no-edit <bad-commit>
```

Copy and install the reverted `server/` tree with the same commands above, then
restart both application services:

```bash
sudo systemctl restart home-sensor-bridge.service home-sensor-export-worker.service home-sensor-dashboard.service
sudo systemctl status home-sensor-bridge.service home-sensor-export-worker.service home-sensor-dashboard.service --no-pager
sudo journalctl -u home-sensor-bridge.service -u home-sensor-export-worker.service -u home-sensor-dashboard.service \
  --since '10 minutes ago' --no-pager
```

The complete compatibility, retention, verification, and firmware deployment
checklist is in [`SEN66_AIR_QUALITY.md`](SEN66_AIR_QUALITY.md).

## Upgrade For Active Monitoring And Exports

Deploy with the established installer rather than copying individual Python
files. From this source checkout on the Pi:

```bash
sudo server/install.sh \
  --project-root /opt/home-sensor/server \
  --no-frontend-assets
```

If `/opt/home-sensor/server/backend/.env` predates this feature, the defaults
already point at `/var/lib/home-sensor`, but add the documented variables from
`.env.example` so operations are explicit. Inspect before restart:

```bash
sudo systemd-analyze verify \
  /etc/systemd/system/home-sensor-dashboard.service \
  /etc/systemd/system/home-sensor-export-worker.service
sudo ls -ld /var/lib/home-sensor /var/lib/home-sensor/exports
```

Only the new worker and dashboard need restart for this feature; the MQTT bridge,
Mosquitto, InfluxDB, Grafana, sensor firmware, and publishing rates are
unchanged:

```bash
sudo systemctl restart home-sensor-export-worker.service home-sensor-dashboard.service
sudo systemctl status home-sensor-export-worker.service home-sensor-dashboard.service --no-pager
sudo journalctl -u home-sensor-export-worker.service -u home-sensor-dashboard.service \
  --since '10 minutes ago' --no-pager
curl -fsS http://127.0.0.1:8080/api/health
curl -fsS http://127.0.0.1:8080/api/monitoring/sessions | python3 -m json.tool
curl -fsS http://127.0.0.1:8080/api/exports | python3 -m json.tool
```

On rollback, do not delete the SQLite database or export directory. Older code
will ignore them, and redeploying this feature will reuse completed jobs/files.

## Deploy The Read-Only X2D Observer And Printer Dashboard

This remains a separate approval boundary. Confirm that no print is active
before the first deployment, take backups of `monitoring.sqlite3` and any
existing `printer.sqlite3`, and use the normal installer to copy the reviewed
server tree. The installer installs (but does not start unless requested) the
`home-sensor-printer-observer.service` unit alongside the existing three units.

Install the pre-deployment-generated `printer.toml`, whose entity IDs were
matched directly against the fresh HA registry. Do not hand-edit or guess the
entity prefix. Keep the printer ID non-sensitive. Add only user-chosen or
currently sourced maintenance intervals.

Create `/etc/home-sensor/printer.env` only through the reviewed credential
installer after the server tree is copied:

```bash
sudo /usr/bin/python3 \
  /opt/home-sensor/server/scripts/configure_printer_credentials.py
```

The script reads the Home Assistant long-lived access token from a hidden
terminal prompt and verifies it with one bounded local `GET /api/`. It copies
the existing ha-bambulab cloud token and device ID directly from HA's protected
config-entry storage. None of the three values enters argv, the process
environment, shell history, stdout, or the deployment report. It atomically
installs a root-owned, `home-sensor`-group-readable mode-`0640` environment
file. Never display that file in logs or a deployment transcript.

The dashboard environment must explicitly use the actual SEN66 location:

```text
PRINTER_DB_PATH=/var/lib/home-sensor/printer.sqlite3
PRINTER_ENVIRONMENT_LOCATION=office
PRINTER_BASELINE_MINUTES=30
PRINTER_RECOVERY_MINUTES=120
```

After the reviewed files are installed, initialize the observer first so the
shared SQLite schema exists, then restart the two application consumers:

```bash
sudo systemctl enable --now home-sensor-printer-observer.service
sudo systemctl restart home-sensor-export-worker.service home-sensor-dashboard.service
```

Do not restart or reconfigure Home Assistant, Mosquitto, InfluxDB, Grafana,
Docker, containerd, Tailscale, or the sensor bridge. Verify the read-only API,
automatic-monitoring metadata, bounded process resources, and absence of HA
service calls in observer logs. Do not induce a print for testing.

Rollback stops/disables the observer, restores the prior `/opt/home-sensor/server`
tree and prior application unit files, reloads systemd, restores the two SQLite
backups if a full data rollback is required, and restarts only the dashboard and
export worker. Retain the new SQLite files by default: older code ignores the
additional tables/columns, and preserving them avoids losing maintenance audit
events or imported provenance.
