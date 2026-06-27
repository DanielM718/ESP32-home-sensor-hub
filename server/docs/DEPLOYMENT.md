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
