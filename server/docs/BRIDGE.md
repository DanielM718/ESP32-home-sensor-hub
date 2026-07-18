# MQTT To InfluxDB Bridge

The bridge is a Python service started by:

```text
home-sensor-bridge.service
```

Runtime entrypoint:

```bash
cd /opt/home-sensor/server/backend
/opt/home-sensor/server/backend/.venv/bin/python -m bridge.mqtt_bridge
```

## Responsibilities

- Connect to Mosquitto with the bridge MQTT user.
- Subscribe to `home/sensors/+` and `home/air/+`.
- Validate every JSON payload before storage.
- Convert valid messages into typed readings.
- Write readings to InfluxDB OSS v2.
- Log invalid messages and skip them.

The bridge never publishes sensor data and never reads directly from ESP-NOW.

## Validation Rules

Sensor topics must be:

```text
home/sensors/<node_id>
```

The topic node ID must match payload `node_id`.

Required sensor fields:

- `node_id`
- `sequence`
- `temperature_c`
- `humidity`
- `battery_mv`

`status_flags` is required from current gateways but optional at the bridge
boundary for historical compatibility. When present, it must be an unsigned
32-bit integer and is written to InfluxDB unchanged; the bridge never masks
unknown bits. When absent, the InfluxDB point omits `status_flags` instead of
inventing zero.

The bridge writes `battery_mv` only when `status_flags & (1 << 2)` is nonzero.
If the valid bit is clear or status is unavailable, temperature, humidity, and
sequence are still stored, but no battery-voltage field is written. This
prevents a placeholder zero from becoming a physical zero-volt measurement.

Air-quality topics must be:

```text
home/air/<location>
```

The location must be a stable slug containing letters, numbers, `_`, or `-`.

Required air-quality fields:

- `co2`
- `pm1`
- `pm25`
- `pm4`
- `pm10`
- `voc_index`
- `nox_index`
- `temperature_c`
- `humidity`

## InfluxDB Writes

The bridge writes:

- `environment_reading`
- `air_quality_reading`

Each reading uses the Pi receive time as the InfluxDB timestamp because the MQTT
payload contract does not include a trusted sensor timestamp.

## Delivery Behavior

The Paho client uses callback API v2, QoS from `MQTT_QOS`, and manual
acknowledgement.

- Valid messages are acknowledged after successful InfluxDB write.
- Invalid messages are logged and acknowledged so they do not redeliver forever.
- Write failures are allowed to fail the callback and restart the service via
  systemd instead of silently dropping data.

## Configuration

Key environment variables in `server/backend/.env`:

```text
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_USERNAME=home_sensor_bridge
MQTT_PASSWORD=...
MQTT_QOS=1
MQTT_MAX_PAYLOAD_BYTES=4096
MQTT_SENSOR_TOPIC=home/sensors/+
MQTT_AIR_TOPIC=home/air/+
INFLUXDB_URL=http://127.0.0.1:8086
INFLUXDB_ORG=home
INFLUXDB_BUCKET=environment
INFLUXDB_WRITE_TOKEN=...
```

Official references:

- Paho MQTT Python client: <https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html>
- InfluxDB Python client: <https://docs.influxdata.com/influxdb/v2/api-guide/client-libraries/python/>
