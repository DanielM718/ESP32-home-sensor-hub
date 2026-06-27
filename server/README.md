# Home Sensor Raspberry Pi Backend

Production Raspberry Pi backend for the environmental monitoring system.

This project is deployed to Raspberry Pi OS Lite 64-bit. It is generated from a
Mac development workstation, but installation commands and service management
are intended to run only on the Raspberry Pi.

## Scope

This repository directory owns only the Raspberry Pi backend:

- Mosquitto MQTT broker configuration
- Python MQTT-to-InfluxDB bridge
- InfluxDB OSS v2 setup guidance
- Grafana provisioning
- Flask dashboard and REST API
- Chart.js frontend
- systemd services
- Tailscale access documentation
- native Raspberry Pi deployment scripts

The ESP32 sensor node and master gateway firmware are external to this backend.
This backend subscribes to MQTT messages already published by the gateway and
does not implement ESP-NOW, publish sensor data, or modify gateway protocol.

## Target Architecture

```text
Battery ESP32-C3 nodes
        |
      ESP-NOW
        |
Master ESP32 gateway
        |
 Wi-Fi + MQTT publish
        |
Raspberry Pi 4
        |
 Mosquitto MQTT broker
        |
 Python bridge
        |
 InfluxDB OSS v2
        |
 +-------------------+-------------------+
 |                   |                   |
Grafana        Flask REST API       future services
                  |
              Chart.js UI
```

## MQTT Contract

Sensor node updates are expected at:

```text
home/sensors/<node_id>
```

Example payload:

```json
{
  "node_id": 1,
  "sequence": 1523,
  "temperature_c": 24.8,
  "humidity": 41.6,
  "battery_mv": 4058,
  "status_flags": 0
}
```

Future air-quality updates are expected at:

```text
home/air/<location>
```

Example payload:

```json
{
  "co2": 721,
  "pm1": 1.1,
  "pm25": 2.8,
  "pm4": 3.5,
  "pm10": 5.2,
  "voc_index": 88,
  "nox_index": 12,
  "temperature_c": 24.5,
  "humidity": 42.3
}
```

The bridge will validate incoming JSON before writing to InfluxDB. Invalid
messages are logged and ignored.

## Planned REST API

The Flask service reads historical and latest data from InfluxDB only. It does
not read directly from MQTT.

- `GET /api/latest`
- `GET /api/readings`
- `GET /api/nodes`

The frontend will be served at:

```text
http://sensor-pi.local:8080
http://<raspberry-pi-ip>:8080
```

Remote access is through Tailscale only.

## Repository Layout

```text
server/
  backend/          Python services and tests
  frontend/         Flask templates and Chart.js assets
  config/           Mosquitto, InfluxDB, and Grafana config
  scripts/          Raspberry Pi setup and verification scripts
  systemd/          service units for Pi deployment
  docs/             deployment and operations documentation
  requirements.txt  Python dependencies for the Pi virtual environment
  .env.example      example deployment environment
```

## Security Defaults

- Python services run as the dedicated `home-sensor` Linux user.
- Python dependencies are installed into `server/backend/.venv` on the Pi.
- Secrets live in `server/backend/.env` and are not committed.
- Mosquitto anonymous access is disabled.
- Services do not run as root.
- Remote access is through Tailscale, not direct internet exposure.
- Production services use Python logging rather than `print()`.

## Official Documentation References

This backend is planned against current official documentation:

- Tailscale Linux install: <https://tailscale.com/kb/1031/install-linux>
- Mosquitto configuration: <https://mosquitto.org/man/mosquitto-conf-5.html>
- Mosquitto password files: <https://mosquitto.org/man/mosquitto_passwd-1.html>
- InfluxDB OSS v2 install: <https://docs.influxdata.com/influxdb/v2/install/>
- InfluxDB Python client: <https://docs.influxdata.com/influxdb/v2/api-guide/client-libraries/python/>
- Grafana Debian install: <https://grafana.com/docs/grafana/latest/setup-grafana/installation/debian/>
- Grafana provisioning: <https://grafana.com/docs/grafana/latest/administration/provisioning/>
- Flask deployment: <https://flask.palletsprojects.com/en/stable/deploying/>
- Chart.js: <https://www.chartjs.org/docs/latest/>
- Eclipse Paho MQTT Python client: <https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html>
- systemd service units: <https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html>

## Milestone Status

This repository is being built incrementally. Milestone 1 establishes the
structure, dependency list, environment contract, and baseline documentation.
Milestone 2 adds the base native deployment scripts and systemd unit templates.
Milestone 3 adds Mosquitto broker config, MQTT users/ACL scripts, and MQTT
operations documentation. Milestone 4 adds InfluxDB OSS v2 install/setup
scripts, schema documentation, and token verification. Milestone 5 adds the
Python MQTT-to-InfluxDB bridge. Milestone 6 adds the Flask REST API.
Milestone 7 adds the Chart.js frontend dashboard. Milestone 8 adds Grafana
provisioning and example dashboards. Milestone 9 adds Tailscale deployment and
remote-access security guidance. The final milestone adds the integration
review and deployment walkthrough.

## Base Installer

After copying this repository to the Raspberry Pi, the base installer is:

```bash
cd /path/to/sensor_home/server
sudo ./install.sh
```

The installer is Raspberry Pi/Linux-only. It must not be run on the Mac
development machine.

## Deployment Guide

Start with the clean install walkthrough:

- [Architecture](docs/ARCHITECTURE.md)
- [Clean Raspberry Pi OS Lite Deployment](docs/CLEAN_INSTALL.md)
- [Repository Tree](docs/REPOSITORY_TREE.md)
- [Operations](docs/OPERATIONS.md)
- [Security](docs/SECURITY.md)
- [Final Integration Review](docs/FINAL_REVIEW.md)

The optional Docker Compose mirror is documented in [DOCKER.md](docs/DOCKER.md).
