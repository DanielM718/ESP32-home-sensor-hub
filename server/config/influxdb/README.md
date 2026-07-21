# InfluxDB Configuration

This directory documents the InfluxDB OSS v2 schema and deployment defaults for
the Raspberry Pi backend.

Generated scripts:

- `scripts/install_influxdb.sh`: installs the InfluxDB server and `influx` CLI on the Pi
- `scripts/setup_influxdb.sh`: initializes the org, long-term/live buckets, and
  scoped tokens
- `scripts/verify_influxdb.sh`: checks service/CLI availability and access to
  both buckets

Runtime secrets are written to:

```text
/opt/home-sensor/server/backend/.env
```

Do not commit generated tokens.

See [`schema.md`](schema.md) and
[`../../docs/SEN66_AIR_QUALITY.md`](../../docs/SEN66_AIR_QUALITY.md) for the
72-hour live tier, 15-minute aggregate schema, sparse events, and additive
migration behavior.
