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

These names match the SEN66 firmware payload exactly. The bridge intentionally
does not accept alternate spellings such as `co2_ppm`, `pm1_0`, `pm2_5`, or
`pm4_0`, because silently supporting two schemas would make field-name mistakes
harder to detect. CO2, VOC Index, and NOx Index are integers. PM values are
`µg/m³`, temperature is `°C`, and humidity is `% RH`.

## InfluxDB Writes

The bridge writes:

- `environment_reading`
- `air_quality_reading`

Each reading uses the Pi receive time as the InfluxDB timestamp because the MQTT
payload contract does not include a trusted sensor timestamp.

`AirQualityReading.fields` writes all nine values into one
`air_quality_reading` point. The existing `SensorReading` path and
`home/sensors/+` subscription are independent and unchanged.

## Full SEN66 Round-Trip Test

The repository includes `examples/sen66-full.json` and a verification script
that publishes it, then polls the Flask API until all nine values appear:

```bash
MQTT_PUBLISH_PASSWORD='<gateway-password>' \
  /opt/home-sensor/server/scripts/verify_sen66.sh
```

The default synthetic topic is `home/air/sen66_test`. Override it with
`SEN66_TEST_LOCATION=<slug>`. A failure directs you to the bridge and dashboard
logs; it does not fall back to the physical sensor path.

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
