# MQTT Contract

Mosquitto runs on the Raspberry Pi and receives messages from the master ESP32
gateway. The Python bridge subscribes to broker topics and writes validated
messages to InfluxDB.

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

Future air-quality stations:

```text
home/air/<location>
```

The bridge will subscribe to:

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
  "co2": 721,
  "pm1": 1.1,
  "pm25": 2.8,
  "pm4": 3.5,
  "pm10": 5.2,
  "voc_index": 88,
  "nox_index": 12,
  "temperature_c": 24.5,
  "humidity": 42.3
}
```

The bridge will ignore unknown fields for storage compatibility and log malformed
JSON, missing required fields, and out-of-range values.

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

Two MQTT users are expected:

- `home_sensor_gateway`: write-only access to `home/sensors/+` and `home/air/+`
- `home_sensor_bridge`: read-only access to `home/sensors/+` and `home/air/+`

The bridge username must match `MQTT_USERNAME` in `server/backend/.env`.
The gateway username and password must be configured on the external ESP32
gateway.

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

Expected result: the subscriber receives the JSON payload. The bridge user should
not be able to publish, and the gateway user should not need read permissions.

The Python bridge consumes the same topics. See `server/docs/BRIDGE.md` for
validation and InfluxDB write behavior.

## Network Exposure

The generated broker listener binds to `0.0.0.0:1883` so the ESP32 gateway can
reach the Pi on the LAN. Do not configure router port forwarding to this port.
If the Pi is ever placed on a public IP, replace the listener address with a
specific LAN interface address or add firewall rules before starting Mosquitto.
