# Architecture

This backend owns only the Raspberry Pi side of the system. ESP32 firmware and
ESP-NOW behavior are external.

## Data Flow

```text
Battery ESP32-C3 nodes -- ESP-NOW --> Master ESP32 gateway --+
                                                              |
USB-powered SEN66 -------- direct Wi-Fi/MQTT JSON ------------+
                                                              |
                                                              v
                                                  Mosquitto on Raspberry Pi
        |
        | authenticated subscribe
        v
Python bridge
        |
        | validated writes
        v
InfluxDB OSS v2
        |
        +----------------------+----------------------+
        |                      |                      |
        v                      v                      v
Grafana analytics       Flask REST API          Future services
                               |
                               v
                         Chart.js dashboard
```

## Runtime Processes

- `mosquitto.service`: MQTT broker, authenticated, no anonymous clients.
- `influxdb.service`: time-series storage.
- `grafana-server.service`: primary analytics UI.
- `home-sensor-bridge.service`: Python MQTT-to-InfluxDB bridge.
- `home-sensor-dashboard.service`: Flask API and dashboard via Gunicorn.
- `tailscaled.service`: Tailnet access for remote administration.

## Ports

- `1883`: MQTT on LAN for the ESP32 gateway and direct-MQTT stations.
- `8086`: InfluxDB local/LAN administration; not public.
- `3000`: Grafana LAN/Tailscale.
- `8080`: Flask dashboard LAN/Tailscale.
- `22`: optional SSH or Tailscale SSH.

Do not expose these services directly to the public internet. Remote access is
through Tailscale.

## Storage Contract

InfluxDB bucket:

```text
environment
```

Measurements:

- `environment_reading`
- `air_quality_reading`

The MQTT bridge validates incoming messages before writing. The Flask API and
Grafana read only from InfluxDB.
