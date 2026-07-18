# Flask REST API

The Flask app is a lightweight overview API and dashboard host. It reads from
InfluxDB only and never reads directly from MQTT.

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
      "sequence": 1523
    }
  ],
  "air_quality": [],
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
      "battery_shutdown": false
    }
  ]
}
```

The `nodes` snapshot is derived from the same latest-value query used for the
current readings. This lets the dashboard update current readings and node
status with one InfluxDB query instead of immediately repeating it through
`/api/nodes`. The standalone `/api/nodes` endpoint remains supported.

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
    }
  ]
}
```

Environment history pivots each raw `battery_mv` together with the
same-timestamp `status_flags` and applies a bitwise `BIT2` test before
downsampling. Battery points with missing status or a clear valid bit are
omitted from `/api/readings`; temperature and humidity history is unaffected.

### `GET /api/nodes`

Returns node/station status based on latest readings and
`NODE_STALE_AFTER_SECONDS`.

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
or `no_recent_reading` otherwise. The primary `status` remains `stale` in both
cases so stale-node detection is not suppressed.

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
