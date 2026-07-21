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
the virtual environment from `requirements.txt`, and installs the two Python
systemd units. The unit files confirm these service names and runtime split:

- `home-sensor-bridge.service`: MQTT subscriptions and InfluxDB writes
- `home-sensor-dashboard.service`: Gunicorn, Flask API, and static dashboard
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
sudo systemctl restart home-sensor-bridge.service home-sensor-dashboard.service
sudo systemctl status home-sensor-bridge.service home-sensor-dashboard.service --no-pager
```

Do not restart Mosquitto or InfluxDB. Follow both application logs while running
the MQTT test:

```bash
sudo journalctl -u home-sensor-dashboard.service -f
sudo journalctl -u home-sensor-bridge.service -f
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
sudo systemctl restart home-sensor-bridge.service home-sensor-dashboard.service
sudo systemctl status home-sensor-bridge.service home-sensor-dashboard.service --no-pager
sudo journalctl -u home-sensor-bridge.service -u home-sensor-dashboard.service \
  --since '10 minutes ago' --no-pager
```

The complete compatibility, retention, verification, and firmware deployment
checklist is in [`SEN66_AIR_QUALITY.md`](SEN66_AIR_QUALITY.md).
