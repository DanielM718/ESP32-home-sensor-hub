# InfluxDB Design

This backend targets InfluxDB OSS v2.

Official references:

- <https://docs.influxdata.com/influxdb/v2/install/>
- <https://docs.influxdata.com/influxdb/v2/api-guide/client-libraries/python/>
- <https://docs.influxdata.com/influxdb/v2/reference/cli/influx/setup/>
- <https://docs.influxdata.com/influxdb/v2/reference/cli/influx/bucket/create/>
- <https://docs.influxdata.com/influxdb/v2/reference/cli/influx/bucket/list/>
- <https://docs.influxdata.com/influxdb/v2/reference/cli/influx/auth/create/>
- <https://docs.influxdata.com/influxdb/v2/write-data/best-practices/schema-design/>

## Default Settings

```text
Organization: home
Bucket: environment
URL: http://127.0.0.1:8086
Retention: 0 (infinite)
```

Scoped application tokens are stored in `server/backend/.env` on the Raspberry
Pi:

- `INFLUXDB_WRITE_TOKEN`: write-only token for the MQTT bridge
- `INFLUXDB_READ_TOKEN`: read-only token for Flask and Grafana
- `INFLUXDB_TOKEN`: compatibility alias for the write token

The InfluxDB admin token is used only during setup and should be stored in a
password manager, not committed.

## Raspberry Pi Setup

After the base installer has copied the project to `/opt/home-sensor/server`,
install InfluxDB on the Pi:

```bash
sudo /opt/home-sensor/server/scripts/install_influxdb.sh
```

Then initialize the org, bucket, and scoped tokens:

```bash
sudo /opt/home-sensor/server/scripts/setup_influxdb.sh
```

The setup script prompts for an admin password if `INFLUXDB_ADMIN_PASSWORD` is
not exported. If `INFLUXDB_ADMIN_TOKEN` is not exported, the script generates
one and prints it once so it can be stored in a password manager.

The script passes setup values to the `influx` CLI. Run it from a trusted local
Pi shell, not over an untrusted shared terminal session.

Use explicit settings when needed:

```bash
sudo /opt/home-sensor/server/scripts/setup_influxdb.sh \
  --url http://127.0.0.1:8086 \
  --org home \
  --bucket environment \
  --retention 0
```

Verify the setup:

```bash
sudo /opt/home-sensor/server/scripts/verify_influxdb.sh
```

## Measurements

Initial measurements:

- `environment_reading`
- `air_quality_reading`

Tags are used for low-cardinality dimensions:

- `node_id`
- `topic`
- `sensor_type`
- `location`

Fields are used for measured values:

- `temperature_c`
- `humidity`
- `battery_mv`
- `status_flags`
- `sequence`
- `co2`
- `pm1`
- `pm25`
- `pm4`
- `pm10`
- `voc_index`
- `nox_index`

This schema keeps future sensor types and rooms additive.

For `environment_reading`, `status_flags` is the raw unsigned SHT41 status
integer. Its battery bits are `BIT2` measurement valid, `BIT3` low battery, and
`BIT4` confirmed shutdown. The field may be absent on historical points. The
bridge writes `battery_mv` only for packets with `BIT2` set, so absence means
unavailable; a missing field is not a zero-volt measurement. Unknown status
bits remain stored in the raw integer. Historical queries pair `battery_mv`
with the same point's `status_flags` and include it only when a bitwise `BIT2`
test succeeds; existing rows are not rewritten.

See also:

- `server/config/influxdb/schema.md`
- `server/config/influxdb/README.md`
- `server/docs/BRIDGE.md`
- `server/docs/API.md`
- `server/docs/GRAFANA.md`
