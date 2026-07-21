# InfluxDB Schema

Target: InfluxDB OSS v2 organization `home`.

## Retention

- `environment_live`: 72-hour retention for high-resolution SEN66 samples.
- `environment`: retention `0` (infinite) for environmental-node readings,
  15-minute SEN66 aggregates, sparse SEN66 events, and legacy SEN66 raw data.

The split is additive: setup does not change the existing `environment` bucket's
retention or rewrite its history. See
[`SEN66_AIR_QUALITY.md`](../../docs/SEN66_AIR_QUALITY.md) for the migration,
volume estimate, and query behavior.

## Measurements

### `environment_reading`

Used for ESP32-C3 temperature, humidity, and battery sensor nodes.

Tags:

- `node_id`
- `topic`
- `sensor_type`

Fields:

- `sequence`
- `temperature_c`
- `humidity`
- `battery_mv`
- `status_flags`

Line protocol shape:

```text
environment_reading,node_id=1,topic=home/sensors/1,sensor_type=environment temperature_c=24.8,humidity=41.6,battery_mv=4058i,status_flags=4i,sequence=1523i
```

`status_flags` is stored as the unmasked integer received from MQTT. Known
battery bits are `BIT2` measurement valid, `BIT3` low, and `BIT4` shutdown.
Historical points may omit `status_flags`; points without `BIT2` also omit
`battery_mv`, representing unavailable status/voltage without fabricating a
zero measurement. No schema or historical-data migration is required because
InfluxDB fields are additive and optional per point.

### `air_quality_reading` (`environment_live`)

Used for direct-MQTT room-level air-quality stations such as the SEN66 node.

Tags:

- `location`
- `topic`
- `sensor_type`
- `node_id` when supplied

Fields:

- `co2`
- `pm1`
- `pm25`
- `pm4`
- `pm10`
- `voc_index`
- `nox_index`
- `temperature_c`
- `humidity`
- `sample_valid`
- optional `sraw_voc`, `sraw_nox`
- optional `sequence`, `status_flags`, `schema_version`, `boot_id`,
  `sensor_uptime_s`, `reset_reason`, and `firmware_version`

Line protocol shape:

```text
air_quality_reading,location=printer_room,topic=home/air/printer_room,sensor_type=air_quality,node_id=100 co2=721i,pm1=1.1,pm25=2.8,pm4=3.5,pm10=5.2,voc_index=88i,nox_index=12i,temperature_c=24.5,humidity=42.3,sample_valid=true,sequence=42i,boot_id=2712847316i
```

Invalid samples still write `sample_valid=false` plus valid metadata, while
missing or invalid measured fields are omitted. They count toward aggregate
coverage diagnostics but can never appear as zero or healthy values.

### `air_quality_15m` (`environment`)

One UTC-aligned point per 15-minute window and station. It stores sample counts,
expected count, coverage, partial-window state, means, pollutant maxima, selected
p95 values, VOC minimum, and optional raw-gas statistics. Timestamp is the
window start. Missing statistics are omitted.

### `air_quality_event` (`environment`)

One sparse point per threshold or rapid-rise episode. Tags include `location`,
`event_type`, `metric`, and stable station tags. Completion upserts the trigger
point with peak, end, duration, sample count, state, threshold provenance, and
baseline context. Hysteresis and cooldown prevent per-sample event spam.

## Cardinality Rules

Keep tags low-cardinality:

- good tags: node IDs, room/location slugs, stable topic names, sensor type
- bad tags: sequence numbers, timestamps, raw status text, battery voltage

Additional sensor types should add new measurements or fields without changing
the existing MQTT payload contract. Sequence numbers, boot IDs, timestamps,
raw readings, and event state remain fields rather than tags.
