# MQTT Contract

Mosquitto runs on the Raspberry Pi and receives messages from the master ESP32
gateway and direct-MQTT stations such as the SEN66 node. The Python bridge
subscribes to broker topics and writes validated messages to InfluxDB.

Official references:

- Mosquitto configuration: <https://mosquitto.org/man/mosquitto-conf-5.html>
- Mosquitto password files: <https://mosquitto.org/man/mosquitto_passwd-1.html>
- Mosquitto publish test client: <https://mosquitto.org/man/mosquitto_pub-1.html>
- Mosquitto subscribe test client: <https://mosquitto.org/man/mosquitto_sub-1.html>

## Topics

Temperature and humidity nodes:

```text
home/sensors/<node_id>
```

Air-quality stations:

```text
home/air/<location>
```

The bridge subscribes to:

```text
home/sensors/+
home/air/+
```

## Sensor Payload

```json
{
  "node_id": 1,
  "sequence": 1523,
  "temperature_c": 24.8,
  "humidity": 41.6,
  "battery_mv": 4058,
  "status_flags": 4
}
```

Required fields:

- `node_id`: integer
- `sequence`: integer
- `temperature_c`: number
- `humidity`: number from 0 to 100
- `battery_mv`: integer

Optional compatibility field:

- `status_flags`: optional unsigned 32-bit integer for historical compatibility;
  current SHT41 gateways always publish it

Known SHT41 bits are:

- `BIT2` (`4`): `STATUS_BATTERY_OK`
- `BIT3` (`8`): `STATUS_BATTERY_LOW`
- `BIT4` (`16`): `STATUS_BATTERY_SHUTDOWN`

Consumers decode these with bitwise AND, so combinations `12` and `28` retain
the measurement-valid bit. Unknown bits are allowed and preserved. A
`battery_mv` value is valid only when `BIT2` is set. Zero with `BIT2` clear, or
any battery value in a historical message without `status_flags`, is
unavailable rather than a measured zero-volt battery.

## Air-Quality Payload

```json
{
  "packet_type": "sen66",
  "schema_version": 2,
  "firmware_version": "2.1.0",
  "node_id": 100,
  "sequence": 42,
  "boot_id": 2712847316,
  "sensor_uptime_s": 3600,
  "reset_reason": 1,
  "status_flags": 255,
  "co2": 721,
  "pm1": 1.1,
  "pm25": 2.8,
  "pm4": 3.5,
  "pm10": 5.2,
  "voc_index": 88,
  "nox_index": 12,
  "sraw_voc": 24100,
  "sraw_nox": 19300,
  "temperature_c": 24.5,
  "humidity": 42.3
}
```

Current firmware publishes all nine primary fields. CO2, VOC index, and NOx
index are integers; PM values, temperature, and humidity are JSON numbers. A
missing, null, non-finite, or out-of-range primary field produces an invalid
sample with the bad field omitted rather than a fabricated zero. Malformed JSON
or an invalid topic is rejected. The bridge assigns Raspberry Pi receive time;
stations do not include a trusted wall-clock timestamp.

Firmware schema v2 intentionally emits JSON `null` for an unavailable measured
value while retaining uptime, boot, sequence, reset, and status metadata. The
bridge persists that as `sample_valid=false` and omits the unavailable numeric
field.

The fields and units are:

| MQTT / InfluxDB / API field | Reading | Unit |
| --- | --- | --- |
| `temperature_c` | Temperature | °C |
| `humidity` | Relative humidity | % RH |
| `co2` | Carbon dioxide | ppm |
| `pm1` | PM1.0 | µg/m³ |
| `pm25` | PM2.5 | µg/m³ |
| `pm4` | PM4.0 | µg/m³ |
| `pm10` | PM10 | µg/m³ |
| `voc_index` | VOC Index | index |
| `nox_index` | NOx Index | index |

`packet_type`, `schema_version`, `firmware_version`, `node_id`, `sequence`,
`status_flags`, `boot_id`, `sensor_uptime_s`, and `reset_reason` are current
SEN66 firmware metadata. The bridge stores the applicable metadata for
deduplication, warm-up, reset, and diagnostics. `sraw_voc` and `sraw_nox` are
optional 0–65,534 diagnostic ticks (65,535 is the unavailable sentinel), not
concentrations. The measurement keys are `co2`, `pm1`, `pm25`, `pm4`, and `pm10`,
not alternate spellings such as `co2_ppm`, `pm1_0`, `pm2_5`, or `pm4_0`.

The SEN66 is polled every second while MQTT publishes the newest sample every
five seconds by default. `APP_SENSOR_POLL_INTERVAL_MS` and
`APP_MQTT_PUBLISH_INTERVAL_MS` configure those independent cadences.

## Broker Security

Milestone 3 provides the concrete Mosquitto configuration. The defaults are:

- anonymous clients disabled
- username/password authentication enabled
- ACLs limited to the expected topic prefixes and client roles
- no public internet exposure

## Generated Files

```text
server/config/mosquitto/home-sensor.conf
server/config/mosquitto/home-sensor.acl
server/scripts/install_mosquitto.sh
server/scripts/create_mqtt_users.sh
```

`home-sensor.conf` installs to:

```text
/etc/mosquitto/conf.d/home-sensor.conf
```

`home-sensor.acl` installs to:

```text
/etc/mosquitto/acl.d/home-sensor.acl
```

The password file is generated on the Pi and is not committed:

```text
/etc/mosquitto/passwd
```

## Users And ACLs

Three MQTT users are expected:

- `home_sensor_gateway`: write-only access to `home/sensors/+` and `home/air/+`
- `home_sensor_bridge`: read-only access to `home/sensors/+` and `home/air/+`
- `home_assistant`: read-only access to both sensor families and read/write
  access to `homeassistant/#` for discovery, birth, and derived health topics

The bridge username must match `MQTT_USERNAME` in `server/backend/.env`.
The publishing username and password must be configured on the ESP32 gateway
and on direct-MQTT stations.
The Home Assistant password is stored only in `/opt/home-assistant/.env` and in
Home Assistant's private configuration entry. It is not shared with firmware or
the bridge.

To add only the Home Assistant account to an existing password file without
rotating gateway or bridge credentials:

```bash
sudo /opt/home-sensor/server/scripts/create_mqtt_users.sh --home-assistant-only
```

See `home-assistant/README.md` at the repository root for the isolated
deployment, ACL installation, and discovery validation procedure.

## Raspberry Pi Setup

After copying the project to `/opt/home-sensor/server` on the Pi:

```bash
sudo /opt/home-sensor/server/scripts/install_mosquitto.sh
sudo /opt/home-sensor/server/scripts/create_mqtt_users.sh
```

If you use custom MQTT usernames, pass the same values to both scripts:

```bash
sudo /opt/home-sensor/server/scripts/install_mosquitto.sh --gateway-user my_gateway --bridge-user my_bridge
sudo /opt/home-sensor/server/scripts/create_mqtt_users.sh --gateway-user my_gateway --bridge-user my_bridge
```

`create_mqtt_users.sh` prompts for passwords using `mosquitto_passwd`.
After creating the bridge password, set the same value in:

```text
/opt/home-sensor/server/backend/.env
```

Then restart the broker:

```bash
sudo systemctl restart mosquitto.service
sudo systemctl status mosquitto.service --no-pager
```

Run the MQTT verification script:

```bash
sudo /opt/home-sensor/server/scripts/verify_mqtt.sh
```

## Manual Validation On The Pi

Subscribe as the bridge user:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 -u home_sensor_bridge -P '<bridge-password>' -t 'home/sensors/+'
```

Publish a test sensor update as the gateway user:

```bash
mosquitto_pub -h 127.0.0.1 -p 1883 -u home_sensor_gateway -P '<gateway-password>' -t 'home/sensors/1' -m '{"node_id":1,"sequence":1,"temperature_c":24.8,"humidity":41.6,"battery_mv":4058,"status_flags":4}'
```

Watch every SEN66 station topic in a separate terminal:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 \
  -u home_sensor_bridge -P '<bridge-password>' \
  -t 'home/air/#' -v
```

Publish a complete fake SEN66 reading using the exact firmware field names:

```bash
mosquitto_pub -h 127.0.0.1 -p 1883 \
  -u home_sensor_gateway -P '<gateway-password>' \
  -t 'home/air/sen66_test' -q 1 \
  -m '{"packet_type":"sen66","schema_version":1,"firmware_version":"test","node_id":100,"sequence":1,"status_flags":0,"temperature_c":24.5,"humidity":42.1,"co2":612,"pm1":1.2,"pm25":2.4,"pm4":3.1,"pm10":4.8,"voc_index":87,"nox_index":2}'
```

For an automated MQTT → bridge → InfluxDB → API check, run:

```bash
MQTT_PUBLISH_PASSWORD='<gateway-password>' \
  /opt/home-sensor/server/scripts/verify_sen66.sh
```

Expected result: the subscriber receives the JSON payload. The bridge user should
not be able to publish, and the gateway user should not need read permissions.

The Python bridge consumes the same topics. See `server/docs/BRIDGE.md` for
validation and InfluxDB write behavior.

## Network Exposure

The generated broker listener binds to `0.0.0.0:1883` so the ESP32 gateway can
reach the Pi on the LAN. Do not configure router port forwarding to this port.
If the Pi is ever placed on a public IP, replace the listener address with a
specific LAN interface address or add firewall rules before starting Mosquitto.
