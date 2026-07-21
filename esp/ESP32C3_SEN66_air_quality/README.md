# ESP32-C3 SEN66 Air-Quality Station

ESP-IDF v6.0.1 firmware for a USB-powered Seeed XIAO ESP32-C3 connected to a
Sensirion SEN66 over I2C. Unlike the battery SHT41 nodes, this station connects
to Wi-Fi and publishes directly to Mosquitto. It does not use ESP-NOW or pass
through the master gateway.

```text
SEN66 -> I2C -> XIAO ESP32-C3 -> Wi-Fi/MQTT -> Raspberry Pi
                                             home/air/<location>
```

The Raspberry Pi MQTT bridge is the data-contract authority. It validates the
payload, assigns receive time, writes bounded live samples, and builds long-term
15-minute aggregates and event records.

## Hardware

- Controller: Seeed XIAO ESP32-C3
- Sensor: Sensirion SEN66
- I2C address: `0x6B` (7-bit)
- Maximum I2C clock: 100 kHz
- Default SDA: XIAO D4 / GPIO6
- Default SCL: XIAO D5 / GPIO7

| XIAO ESP32-C3 | SEN66 |
| --- | --- |
| 3V3 | VDD |
| GND | GND |
| D4 / GPIO6 | SDA |
| D5 / GPIO7 | SCL |

The SEN66 has substantial current peaks. Use a stable 3.3 V supply path capable
of the module's documented average and peak current, and check whether the
specific cable or breakout already supplies I2C pull-ups. See the
[Sensirion SEN6x datasheet](https://sensirion.com/media/documents/FAFC548D/693FBB15/PS_DS_SEN6x.pdf)
for electrical limits.

## Local Configuration

Create the ignored local header:

```sh
cp main/app_config.example.h main/app_config.h
```

Edit `main/app_config.h` and set:

- `APP_LOCATION`: stable topic slug, for example `office` or `printer_room`
- `APP_WIFI_SSID` and `APP_WIFI_PASSWORD`
- `APP_MQTT_BROKER_HOST` and `APP_MQTT_BROKER_PORT`
- `APP_MQTT_CLIENT_ID`
- `APP_MQTT_USERNAME` and `APP_MQTT_PASSWORD`
- GPIOs if the wiring differs from GPIO6/GPIO7

`APP_LOCATION` may contain only letters, digits, `_`, and `-`, with a maximum
length of 64 characters. It becomes the final topic segment. Keep every device
client ID unique.

`main/app_config.h` is ignored by Git. Never add Wi-Fi credentials, MQTT
passwords, tokens, or machine-specific paths to tracked files.

The committed example deliberately contains placeholder credentials. A build
without `app_config.h` succeeds for verification, but firmware logs that
network publishing is disabled.

## MQTT Contract

Topic:

```text
home/air/<location>
```

Example payload:

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
  "humidity": 42.3,
  "sraw_voc": 24100,
  "sraw_nox": 19300,
  "packet_type": "sen66",
  "schema_version": 2,
  "firmware_version": "2.1.0",
  "node_id": 100,
  "boot_id": 2712847316,
  "sequence": 42,
  "sensor_uptime_s": 3600,
  "reset_reason": 1,
  "status_flags": 479
}
```

Current firmware normally supplies all nine primary fields. The bridge requires
integer CO2, VOC index, and NOx index values when available, so
the firmware rounds the sensor's VOC/NOx decimal indices to the nearest integer.
Particulate matter is in `ug/m3`, temperature in degrees Celsius, relative
humidity in percent, and CO2 in ppm. Raw gas values are unsigned diagnostic
ticks, not concentrations. The bridge stores the applicable metadata for
deduplication, warm-up, reset, and diagnostics.

The firmware does not create a timestamp. The bridge uses Raspberry Pi receive
time, which avoids depending on an unsynchronized ESP32 clock.

SEN66 unknown sentinels, non-finite values, and out-of-range values are logged
and published as JSON `null` while valid uptime/status metadata remains present.
The server records such a packet as invalid without converting it to zero. This
is especially relevant during startup: measured CO2 is unavailable for roughly
22–24 seconds and NOx for roughly 10–11 seconds.

## Runtime Behavior

1. Initialize NVS, Wi-Fi station mode, and the MQTT client.
2. Reconnect Wi-Fi and MQTT automatically after a disconnect.
3. Wait for the SEN66 power-up interval, initialize I2C, and probe `0x6B`.
4. Start continuous measurement and poll the data-ready flag every second.
5. Read all nine values plus optional raw gas ticks and verify every Sensirion
   CRC-8 byte.
6. Publish the newest sample at a separately configured interval, default five
   seconds.
7. Reinitialize the sensor after repeated I2C or read failures.

Sensor measurement continues even while MQTT is unavailable. Samples are not
buffered for later delivery; a disconnected interval is logged and skipped.
QoS 1 is the default.

Diagnostic `status_flags` are included as metadata:

| Bit | Meaning |
| --- | --- |
| 0 | I2C initialized |
| 1 | Continuous measurement started |
| 2 | Data-ready flag set |
| 3 | Measurement read and CRC checks succeeded |
| 4 | Device-status register read succeeded |
| 5 | Device-status register was nonzero |
| 6 | Wi-Fi connected |
| 7 | MQTT connected |
| 8 | MQTT publish attempted |

Bit 5 makes the server mark the packet invalid and create/update the
`sensor_invalid` event. Other metadata is stored with the live point.

The complete source-backed interpretation, warm-up, aggregation, retention, and
event contract is in
[`server/docs/SEN66_AIR_QUALITY.md`](../../server/docs/SEN66_AIR_QUALITY.md).

## Build

Activate ESP-IDF v6.0.1, then build from this directory:

```sh
. /Users/<you>/.espressif/v6.0.1/esp-idf/export.sh
idf.py set-target esp32c3
idf.py build
```

The project declares `espressif/mqtt` as a managed component. The first build
may resolve it through the ESP-IDF Component Manager. `sdkconfig`, `build/`,
`managed_components/`, and dependency locks are generated locally and ignored.

To discard stale configuration from another target or ESP-IDF release:

```sh
idf.py fullclean
idf.py set-target esp32c3
idf.py reconfigure
idf.py build
```

Tracked `sdkconfig.defaults` selects ESP32-C3, 4 MB flash, and the ESP-IDF large
single-app partition. Do not rely on an old ignored `sdkconfig` after changing
targets.

## Flash and Monitor

List likely macOS serial devices before and after connecting the board:

```sh
ls /dev/cu.* /dev/tty.*
```

For the board present during the July 2026 investigation, the port was
`/dev/tty.usbmodem2101` (with matching `/dev/cu.usbmodem2101`). Port numbers can
change after reconnecting or entering the bootloader.

```sh
idf.py -p /dev/tty.usbmodem2101 flash
idf.py -p /dev/tty.usbmodem2101 monitor
```

Exit the ESP-IDF monitor with `Ctrl-]`.

If no USB modem port appears, try another data-capable USB cable, disconnect
other serial devices, and enter the XIAO bootloader using its BOOT/RESET
procedure. That is a separate hardware-enumeration problem from the VS Code
error below.

## VS Code USB Serial Error

Observed error:

```text
I am unable to detect usb serial devices. The "path" argument must be of type string. Received undefined
{"code":"ERR_INVALID_ARG_TYPE"}
```

This error occurs in the ESP-IDF VS Code extension before flashing or firmware
execution. During investigation:

- the OS and ESP-IDF serial helper both detected `/dev/cu.usbmodem2101`;
- the working SHT41 and gateway workspaces supplied complete ESP-IDF paths,
  Python environment, target, clangd, and OpenOCD settings;
- the SEN66 workspace supplied only target/flash/port values, leaving the
  extension without the resolved path needed by its serial-list display;
- the extension then fell back to a nonexistent ESP-IDF path and passed an
  undefined value to its path handling.

It is therefore editor/toolchain metadata, not SEN66 firmware, I2C wiring,
CMake, `sdkconfig`, or a launch task. This project does not need custom
`launch.json` or `tasks.json` files for normal build/flash/monitor commands.

The ignored local `.vscode/settings.json` now mirrors the known-working SHT41
workspace configuration, with SEN66's own build directory, the `esp32c3`
target, the ESP32-C3 OpenOCD configuration, UART flashing, and automatic serial
port detection.

Recover the ESP-IDF extension in this order:

1. Open this SEN66 folder as the VS Code workspace.
2. Run `Developer: Reload Window` once so the extension reloads the corrected
   workspace settings.
3. Reconnect the board and run `ESP-IDF: Select Port to Use`; choose the current
   `/dev/tty.usbmodem*` device, and choose UART when prompted.
4. Run `ESP-IDF: Doctor Command` and confirm that `IDF_PATH`, Python, target,
   and serial port are populated.
5. Run `ESP-IDF: Full Clean Project`, `ESP-IDF: Reconfigure Project`, and
   `ESP-IDF: Build Your Project`.

The command-line build and flash commands above bypass stale VS Code metadata
and remain the most direct recovery path.

## Broker Verification

On the Raspberry Pi, subscribe with the bridge user or another account that has
read access:

```sh
mosquitto_sub -h 127.0.0.1 -p 1883 \
  -u home_sensor_bridge -P '<bridge-password>' -t 'home/air/+' -v
```

After a valid message arrives, the bridge writes `air_quality_reading` fields
to InfluxDB. The Flask dashboard exposes current CO2 and PM2.5 and historical
CO2/PM2.5 charts; Grafana queries the same air-quality measurement.

## Source Layout

```text
main/
  main.c                 startup, validation, measurement loop, JSON payload
  sen66.c/.h             SEN66 I2C commands, scaling, CRC, unknown sentinels
  mqtt_transport.c/.h    Wi-Fi lifecycle, MQTT reconnect, publish wrapper
  app_config.example.h   tracked non-secret configuration template
  idf_component.yml      ESP-MQTT managed-component dependency
```
