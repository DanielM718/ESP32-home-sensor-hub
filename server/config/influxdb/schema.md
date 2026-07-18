# InfluxDB Schema

Target: InfluxDB OSS v2 bucket `environment` in organization `home`.

## Retention

Default retention is `0`, which means infinite retention in InfluxDB v2 CLI
commands. This preserves all historical environmental readings until you decide
to add downsampling or a bounded retention period.

For small home sensor deployments, this is acceptable initially. If sensor count
or sample frequency grows significantly, use a finite retention period and add a
downsampled long-term bucket in a future migration.

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

### `air_quality_reading`

Used for future room-level air-quality stations.

Tags:

- `location`
- `topic`
- `sensor_type`

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

Line protocol shape:

```text
air_quality_reading,location=printer_room,topic=home/air/printer_room,sensor_type=air_quality co2=721i,pm1=1.1,pm25=2.8,pm4=3.5,pm10=5.2,voc_index=88i,nox_index=12i,temperature_c=24.5,humidity=42.3
```

## Cardinality Rules

Keep tags low-cardinality:

- good tags: node IDs, room/location slugs, stable topic names, sensor type
- bad tags: sequence numbers, timestamps, raw status text, battery voltage

Additional sensor types should add new measurements or fields without changing
the existing MQTT payload contract.
