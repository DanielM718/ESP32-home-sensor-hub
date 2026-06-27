# InfluxDB Configuration

This directory documents the InfluxDB OSS v2 schema and deployment defaults for
the Raspberry Pi backend.

Generated scripts:

- `scripts/install_influxdb.sh`: installs the InfluxDB server and `influx` CLI on the Pi
- `scripts/setup_influxdb.sh`: initializes org, bucket, and scoped tokens
- `scripts/verify_influxdb.sh`: checks service/CLI availability and bucket access

Runtime secrets are written to:

```text
/opt/home-sensor/server/backend/.env
```

Do not commit generated tokens.
