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

The optional X2D observer is a separate non-critical branch:

```text
X2D -> existing HA Bambu integration -> GET-only allow-listed observer
                                           |
                                           +-> printer.sqlite3 current/session state
                                           +-> InfluxDB printer history
                                           +-> Flask/Grafana/Butters read paths
```

It is never part of environmental MQTT ingestion. See
[`BAMBU_X2D.md`](BAMBU_X2D.md).

## Runtime Processes

- `mosquitto.service`: MQTT broker, authenticated, no anonymous clients.
- `influxdb.service`: time-series storage.
- `grafana-server.service`: primary analytics UI.
- `home-sensor-bridge.service`: Python MQTT-to-InfluxDB bridge.
- `home-sensor-dashboard.service`: Flask API and dashboard via Gunicorn.
- `home-sensor-export-worker.service`: single-heavy-job CSV worker with
  transactional SQLite claiming and restart recovery.
- `home-sensor-printer-observer.service`: optional, separately configured
  read-only Home Assistant observer; not enabled until real entity discovery.
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

InfluxDB buckets:

```text
environment_live (72-hour high-resolution SEN66 data)
environment (long-term data)
```

Measurements:

- `environment_reading`
- `air_quality_reading` (live bucket; legacy copies may remain long-term)
- `air_quality_15m` (long-term)
- `air_quality_event` (long-term)
- `printer_state` (live), `printer_state_5m` and `print_session` (long-term)

The MQTT bridge validates incoming messages, writes live data, and derives
aggregates/events before long-term writes. The Flask API and Grafana read only
from InfluxDB. See [`SEN66_AIR_QUALITY.md`](SEN66_AIR_QUALITY.md).

## Monitoring And Export Flow

```text
dashboard/API request
        |
        v
monitoring.sqlite3 (session + queued job metadata)
        |
        | transactional claim / lease heartbeat
        v
home-sensor-export-worker.service
        |
        +--> bounded InfluxDB raw or stored-aggregate queries
        +--> /var/lib/home-sensor/exports/<id>.csv.part
        `--> atomic rename to <id>.csv, then completed metadata
```

Gunicorn runs two synchronous workers, so export execution is deliberately not
started inside Flask. Polling reads metadata only. A session deadline is also
reconciled by the export worker, so the browser is never responsible for
completion or automatic-job creation. A unique `monitoring_session_id` on the
job table and one transaction for session completion/job insertion guarantee
one automatic export per session.

InfluxDB remains the reading source of truth. SQLite stores no sensor samples;
it contains workflow metadata only. CSV files and the SQLite database live
under `/var/lib/home-sensor`, outside `/opt/home-sensor/server`.
