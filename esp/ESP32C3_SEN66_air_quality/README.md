# ESP32C3 SEN66 Air Quality Node

ESP-IDF v6.0.1 firmware for a Seeed XIAO ESP32-C3 connected to a Sensirion SEN66 air quality sensor over I2C. The node starts SEN66 continuous measurement once at boot, reads values periodically, and sends compact ESP-NOW packets to the existing ESP32 master gateway.

This project is intentionally independent from the existing SHT41 node. It uses a distinct packet type and does not change the SHT41 packet layout.

## Reference Documents

- ESP-IDF v6.0.1 I2C master driver: https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32c3/api-reference/peripherals/i2c.html
- ESP-IDF v6.0.1 ESP-NOW: https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32c3/api-reference/network/esp_now.html
- ESP-IDF v6.0.1 Wi-Fi API: https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32c3/api-reference/network/esp_wifi.html
- ESP-IDF v6.0.1 FreeRTOS notes: https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32c3/api-reference/system/freertos_idf.html
- Seeed XIAO ESP32-C3 getting started: https://wiki.seeedstudio.com/XIAO_ESP32C3_Getting_Started/
- Sensirion SEN66 product page and downloads: https://sensirion.com/products/catalog/SEN66
- Sensirion SEN6x datasheet: https://sensirion.com/media/documents/FAFC548D/693FBB15/PS_DS_SEN6x.pdf

## Hardware

- MCU: Seeed XIAO ESP32-C3
- Sensor: Sensirion SEN66
- Sensor interface: I2C
- SEN66 I2C address: `0x6B`, 7-bit
- Default SDA: XIAO D4 / GPIO6
- Default SCL: XIAO D5 / GPIO7
- Default I2C speed: 100 kHz

Wiring:

| XIAO ESP32-C3 | SEN66 |
| --- | --- |
| 3.3 V supply | VDD |
| GND | GND |
| D4 / GPIO6 | SDA |
| D5 / GPIO7 | SCL |

Use pull-ups appropriate for your SEN66 breakout/cable and bus length. Many sensor boards already include them; avoid duplicating very strong pull-ups without checking the effective resistance.

## SEN66 Power

The SEN66 node is USB powered. The SEN66 is a continuous air-quality module;
this firmware keeps it in measurement mode and does not deep sleep between
readings. It does not use the SHT41 node's battery-divider circuit or initialize
an ADC for battery monitoring.

Per the Sensirion SEN6x datasheet, SEN63C/SEN65/SEN66 use a 3.15 V to 3.6 V VDD range. For SEN66 measurement mode after startup, size the supply for roughly 90 mA typical, 110 mA max average current, and up to 350 mA peak current pulses. Use a stable 3.3 V rail and verify that the XIAO board, regulator, USB supply, cable, and any carrier board can support that load.

The SEN66 packet has no `battery_mv` field, so no battery value is added merely
for symmetry with the SHT41 packet. The node does not report a valid battery
voltage. If a downstream unified schema supplies a zero battery value for this
node, interpret it as unavailable or not applicable, not as a measured zero,
and do not associate it with `STATUS_BATTERY_OK`.

## Local Configuration

Private deployment values are kept out of source control. Copy the template:

```sh
cp main/app_config.example.h main/app_config.h
```

Edit `main/app_config.h`:

- `APP_NODE_ID`: logical node id used by the gateway and MQTT payload.
- `APP_ESPNOW_CHANNEL`: Wi-Fi channel used for ESP-NOW. This must match the gateway/router channel.
- `APP_ESPNOW_PEER_MAC`: ESP32 master gateway STA MAC address.
- `APP_I2C_SDA_GPIO`: default `GPIO_NUM_6`.
- `APP_I2C_SCL_GPIO`: default `GPIO_NUM_7`.
- `APP_I2C_FREQ_HZ`: default `100000U`; do not exceed the SEN66 100 kbit/s limit.
- `APP_MEASUREMENT_INTERVAL_MS`: default `5000U`.
- `APP_MEASUREMENT_TASK_STACK_SIZE`: default `4096U`.
- `APP_MEASUREMENT_TASK_PRIORITY`: default `5U`.
- `APP_ESPNOW_SEND_TIMEOUT_MS`: default `500U`.

`main/app_config.h` is ignored by this project. The example MAC address is all zeros by design; the firmware rejects it and runs with ESP-NOW disabled until a real unicast gateway STA MAC is supplied.

## Build, Flash, Monitor

Use ESP-IDF v6.0.1.

```sh
cd esp/ESP32C3_SEN66_air_quality
idf.py set-target esp32c3
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

Use the serial port that matches your local XIAO ESP32-C3.

`sdkconfig.defaults` pins the target to ESP32-C3, selects 4 MB flash for the XIAO ESP32-C3, and uses ESP-IDF's large single-app partition table. If you already have a generated local `sdkconfig` from before those defaults were added, regenerate or reconfigure it so the defaults take effect.

## Runtime Behavior

On boot, the firmware:

1. Initializes NVS for Wi-Fi/ESP-NOW.
2. Initializes Wi-Fi STA mode and ESP-NOW.
3. Validates and adds the gateway peer.
4. Waits 100 ms after SEN66 power-up.
5. Initializes the ESP-IDF v6 I2C master bus and SEN66 I2C device.
6. Probes the SEN66 at address `0x6B`.
7. Sends `Start Continuous Measurement` (`0x0021`).
8. Polls `Get Data Ready` (`0x0202`) for the first sample.
9. Starts a periodic FreeRTOS task that reads, logs, and sends samples.

The task uses `vTaskDelayUntil()` with `APP_MEASUREMENT_INTERVAL_MS`, defaulting to 5 seconds.

## SEN66 I2C Protocol

Implemented commands:

| Command | ID | Use |
| --- | --- | --- |
| Start Continuous Measurement | `0x0021` | Start measurement at boot |
| Stop Measurement | `0x0104` | Driver helper only; not used in normal loop |
| Get Data Ready | `0x0202` | Check for a new sample |
| Read Measured Values SEN66 | `0x0300` | Read PM, RH/T, VOC, NOx, CO2 |
| Read Device Status | `0xD206` | Add device-status flags to logs/packet |
| Read And Clear Device Status | `0xD210` | Driver helper |
| Get Product Name | `0xD014` | Probe helper |
| Get Serial Number | `0xD033` | Driver helper |
| Get Version | `0xD100` | Driver helper |
| Device Reset | `0xD304` | Driver helper |

Each 16-bit data word is transferred MSB first and followed by an 8-bit CRC. Commands are not followed by a separate CRC byte. The driver validates every received word and appends CRC to every written data word.

CRC settings:

- Name: Sensirion CRC-8 for SEN6x word checksums
- Polynomial: `0x31`
- Initial value: `0xFF`
- Reflected input/output: no
- Final XOR: `0x00`

## Measurement Timing

The SEN6x datasheet lists a 1 second sampling interval and about 1.1 seconds until the first measurement result after starting continuous measurement. This firmware polls for the first data-ready flag for up to 3 seconds, then the periodic task keeps polling every 5 seconds by default.

Some channels can legitimately report unknown during startup. The firmware preserves official unknown sentinels and sets per-field status bits:

- Unsigned fields unknown: `0xFFFF`
- Signed fields unknown: `0x7FFF`
- NOx can be unknown for roughly the first 10 to 11 seconds after power-on or device reset.
- SEN66 CO2 can be unknown for roughly the first 5 to 6 seconds after measurement start.

## ESP-NOW Packet

SEN66 packets use a distinct packet type and do not change the existing SHT41 packet format.

```c
#define SENSOR_PACKET_TYPE_SEN66 0x6601

typedef struct __attribute__((packed)) {
    uint16_t packet_type;
    uint32_t node_id;
    uint32_t sequence;
    uint16_t co2_ppm;
    uint16_t pm1_ug_m3_x10;
    uint16_t pm25_ug_m3_x10;
    uint16_t pm4_ug_m3_x10;
    uint16_t pm10_ug_m3_x10;
    int16_t voc_index_x10;
    int16_t nox_index_x10;
    int16_t temperature_c_x200;
    int16_t humidity_rh_x100;
    uint32_t status_flags;
} sen66_packet_t;
```

The packet is 32 bytes. ESP-NOW v1 payloads are limited to 250 bytes, so this packet is intentionally compact and has substantial headroom. The gateway should validate both `packet_type == SENSOR_PACKET_TYPE_SEN66` and `sizeof(sen66_packet_t)` before parsing.

## Gateway Decode Rules

The master gateway should decode little-endian ESP32 struct fields from the received packed payload, then convert scaled values:

| JSON field | Packet field | Conversion |
| --- | --- | --- |
| `packet_type` | `packet_type` | `0x6601` maps to `"sen66"` |
| `node_id` | `node_id` | direct |
| `sequence` | `sequence` | direct |
| `co2` | `co2_ppm` | direct, unless CO2 unknown flag is set |
| `pm1` | `pm1_ug_m3_x10` | divide by 10.0 |
| `pm25` | `pm25_ug_m3_x10` | divide by 10.0 |
| `pm4` | `pm4_ug_m3_x10` | divide by 10.0 |
| `pm10` | `pm10_ug_m3_x10` | divide by 10.0 |
| `voc_index` | `voc_index_x10` | divide by 10.0 |
| `nox_index` | `nox_index_x10` | divide by 10.0 |
| `temperature_c` | `temperature_c_x200` | divide by 200.0 |
| `humidity` | `humidity_rh_x100` | divide by 100.0 |
| `status_flags` | `status_flags` | direct hex/integer |

Treat values as unavailable if their corresponding unknown status bit is set, or if the raw sentinel is present:

- PM and CO2: `0xFFFF`
- RH/T, VOC, NOx: `0x7FFF`

The existing master gateway may currently assume text payloads or the SHT41 packet shape. Its SEN66 update should switch on payload length and `packet_type` before publishing MQTT. Do not reinterpret the old SHT41 packet as a SEN66 packet.

## Status Flags

The transmitted `status_flags` field uses these bits:

| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `SEN66_PACKET_STATUS_I2C_READY` | I2C bus/device setup completed |
| 1 | `SEN66_PACKET_STATUS_MEASUREMENT_STARTED` | Continuous measurement command succeeded |
| 2 | `SEN66_PACKET_STATUS_DATA_READY` | Data-ready flag was true for this interval |
| 3 | `SEN66_PACKET_STATUS_MEASUREMENT_READ_OK` | Measured values read and CRC checks passed |
| 4 | `SEN66_PACKET_STATUS_DEVICE_STATUS_READ_OK` | Device status register read succeeded |
| 5 | `SEN66_PACKET_STATUS_DEVICE_STATUS_NONZERO` | Device status register was nonzero |
| 6 | `SEN66_PACKET_STATUS_ESPNOW_READY` | ESP-NOW initialized and peer added |
| 7 | `SEN66_PACKET_STATUS_ESPNOW_SEND_ATTEMPTED` | This packet was passed to ESP-NOW send |
| 10 | `SEN66_PACKET_STATUS_READ_ERROR` | SEN66 data-ready or measured-value read failed |
| 11 | `SEN66_PACKET_STATUS_CRC_ERROR` | Read failed because a SEN66 CRC was invalid |
| 12 | `SEN66_PACKET_STATUS_PM1_UNKNOWN` | PM1.0 field is unknown |
| 13 | `SEN66_PACKET_STATUS_PM25_UNKNOWN` | PM2.5 field is unknown |
| 14 | `SEN66_PACKET_STATUS_PM4_UNKNOWN` | PM4.0 field is unknown |
| 15 | `SEN66_PACKET_STATUS_PM10_UNKNOWN` | PM10 field is unknown |
| 16 | `SEN66_PACKET_STATUS_HUMIDITY_UNKNOWN` | Humidity field is unknown |
| 17 | `SEN66_PACKET_STATUS_TEMPERATURE_UNKNOWN` | Temperature field is unknown |
| 18 | `SEN66_PACKET_STATUS_VOC_UNKNOWN` | VOC index field is unknown |
| 19 | `SEN66_PACKET_STATUS_NOX_UNKNOWN` | NOx index field is unknown |
| 20 | `SEN66_PACKET_STATUS_CO2_UNKNOWN` | CO2 field is unknown |

Bits 8 and 9 are local send-result flags used in logs after the ESP-NOW callback:

- `SEN66_PACKET_STATUS_ESPNOW_SEND_OK`
- `SEN66_PACKET_STATUS_ESPNOW_SEND_FAILED`

Those two are not reliable as transmitted packet state because success/failure is known only after the packet has already been handed to ESP-NOW.

## Expected MQTT Output

The master gateway should eventually publish SEN66 readings to:

```text
home/air/printer_room
```

Payload shape:

```json
{
  "packet_type": "sen66",
  "node_id": 100,
  "sequence": 42,
  "co2": 721,
  "pm1": 1.1,
  "pm25": 2.8,
  "pm4": 3.5,
  "pm10": 5.2,
  "voc_index": 88,
  "nox_index": 12,
  "temperature_c": 24.5,
  "humidity": 42.3,
  "status_flags": 0
}
```

## Repository Hygiene

Ignored local/generated files:

- `build/`
- `managed_components/`
- `dependencies.lock`
- `sdkconfig`
- `sdkconfig.old`
- `main/app_config.h`

The private gateway MAC address belongs in `main/app_config.h`, not in tracked source.

## Known Assumptions

- The gateway STA MAC address and channel are known locally.
- ESP-NOW encryption is not enabled yet; this matches the current simple sensor-node pattern.
- The SEN66 is powered from a stable 3.3 V rail with enough average and peak current capacity.
- The gateway parser will be updated later to recognize the SEN66 packet wrapper.
- The node id `100` is a template default and should be changed if it conflicts with another deployed node.
- The firmware logs the raw SEN66 device status register, but the gateway MQTT schema currently carries only the node-level `status_flags`.
