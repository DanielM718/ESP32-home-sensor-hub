# ESP32 Master Gateway

This ESP-IDF project receives ESP-NOW packets from sensor nodes and publishes
decoded readings to a Mosquitto MQTT broker on the Raspberry Pi.

## Configuration

Use ESP-IDF v6.0.1. ESP-MQTT is an external managed component in ESP-IDF v6, so
the project declares `espressif/mqtt` in `main/idf_component.yml`.

Create a local credentials header from the example:

```sh
cp main/wifi_cred.example.h main/wifi_cred.h
```

Edit `main/wifi_cred.h` locally:

```c
#define WIFI_SSID "your-2.4ghz-ssid"
#define WIFI_PASS "your-wifi-password"

#define MQTT_BROKER_HOST "192.168.1.10"
#define MQTT_BROKER_PORT 1883
#define MQTT_CLIENT_ID "esp32-master-gateway"
#define MQTT_USERNAME ""
#define MQTT_PASSWORD ""
#define MQTT_QOS 1
```

`main/wifi_cred.h` is ignored by git. Do not commit real Wi-Fi or MQTT
credentials.

The sensor nodes and gateway must use the same 2.4 GHz Wi-Fi channel for
ESP-NOW. The gateway logs the connected Wi-Fi channel at startup.

## MQTT Output

For each valid SHT41 ESP-NOW packet, the gateway publishes one compact JSON
payload.

Topic:

```text
home/sensors/<node_id>
```

Example:

```text
home/sensors/1
```

Payload:

```json
{"packet_type":"sht41","node_id":1,"sequence":1523,"temperature_c":24.80,"humidity":41.60,"battery_mv":4058,"status_flags":0}
```

The current SHT41 wire packet is:

```c
typedef struct __attribute__((packed)) {
    uint32_t node_id;
    uint32_t sequence;
    float temp_c;
    float rh;
    uint16_t battery_mv;
    uint32_t status_flags;
} sensor_packet_t;
```

The gateway validates the received packet length before decoding this struct.

## Raspberry Pi Broker Test

From the Raspberry Pi, subscribe to gateway readings with:

```sh
mosquitto_sub -h localhost -p 1883 -t 'home/sensors/#' -v
```

From another machine on the same network, replace `localhost` with the Pi's IP
or hostname:

```sh
mosquitto_sub -h 192.168.1.10 -p 1883 -t 'home/sensors/#' -v
```

If the broker requires authentication, pass the configured MQTT username and
password:

```sh
mosquitto_sub -h 192.168.1.10 -p 1883 -u '<username>' -P '<password>' -t 'home/sensors/#' -v
```

The Raspberry Pi backend can subscribe to these MQTT topics and write readings
to InfluxDB.

## Runtime Behavior

The ESP-NOW receive callback does not decode or publish packets directly. It
copies valid received frames into a FreeRTOS queue. A gateway task receives from
that queue, dispatches the packet to a decoder, builds JSON, and publishes to
MQTT.

If MQTT is disconnected, publishes are skipped and logged. ESP-NOW receive
continues running.

## Future Packet Types

Packet handling is centralized in `main/gateway_packets.c`. The SHT41 handler
matches by exact packet length because the current packet has no explicit type
or version field.

Future packet formats should include explicit packet type/version metadata and
add a new handler entry in `PACKET_HANDLERS`.

A future SEN66 air quality packet could publish to a topic such as:

```text
home/air/printer_room
```

Example payload:

```json
{"packet_type":"sen66","node_id":100,"sequence":42,"co2":721,"pm1":1.10,"pm25":2.80,"pm4":3.50,"pm10":5.20,"voc_index":88,"nox_index":12,"temperature_c":24.50,"humidity":42.30,"status_flags":0}
```

Do not change the existing SHT41 packet layout when adding future packet types.
