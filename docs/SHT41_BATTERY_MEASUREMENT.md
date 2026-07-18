# SHT41 Node Battery-Voltage Measurement

The battery-powered SHT41 node reports calibrated whole-cell voltage through
the existing `battery_mv` packet field. The external divider described here is
required: the XIAO ESP32-C3 battery connection powers and charges the board but
does not provide a directly readable software battery-voltage signal.

## Wiring

Disconnect and remove the 18650 before modifying any holder or board wiring.
Wire the divider as follows:

```text
Battery positive ---- 1 MΩ ----+---- XIAO A0 / D0 / GPIO2
                               |
                              1 MΩ
                               |
                              GND

XIAO A0 / D0 / GPIO2 ---- 100 nF ceramic ---- GND
```

The official Seeed XIAO ESP32-C3 pin table identifies D0 (also used as A0) as
GPIO2 and ADC1 channel 2. The official board schematic shows D0 connected to
ESP32-C3 GPIO2. ESP-IDF v6.0.1's ESP32-C3 ADC mapping independently identifies
GPIO2 as ADC unit 1, channel 2.

Before connecting the divider midpoint to A0:

1. Verify battery polarity.
2. Measure the battery directly with a multimeter.
3. Measure the divider midpoint; it should be approximately half the direct
   battery voltage.
4. Insulate exposed resistor leads and splices with heat-shrink.
5. Add strain relief to the midpoint measurement lead.

A raw Li-ion cell can reach approximately 4.2 V. The equal 1 MΩ / 1 MΩ divider
reduces that to approximately 2.1 V at the ADC input. Firmware converts the
calibrated midpoint voltage back to whole-battery voltage.

## Divider Load and ADC Filtering

The two series resistors total 2 MΩ. At 4.2 V the divider draws approximately
2.1 µA, which is approximately 1.5 mAh over a 30-day month. The 100 nF ceramic
capacitor improves settling and noise performance for this high-impedance
source. Firmware additionally waits 10 ms, discards four conversions, then
averages 32 calibrated conversions before scaling the result.

## Packet Semantics

The packed SHT41 packet layout remains:

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

- `battery_mv` is the whole-battery voltage in millivolts, not the divided
  voltage present at the ADC pin.
- `STATUS_BATTERY_OK` (`BIT2`) means calibrated ADC acquisition and conversion
  succeeded.
- `STATUS_BATTERY_LOW` (`BIT3`) means a valid calibrated reading was below the
  3400 mV warning threshold.
- `STATUS_BATTERY_SHUTDOWN` (`BIT4`) means the conservative shutdown condition
  was confirmed and the transmitted packet is the final packet before
  indefinite deep sleep.
- `battery_mv == 0` without `STATUS_BATTERY_OK` means the value is unavailable
  or acquisition failed. It does not mean the battery is physically at zero
  volts.
- The firmware does not estimate battery percentage.

## Gateway and Server Semantics

The master gateway verifies the packed wire size is 22 bytes and publishes the
entire `uint32_t status_flags` value to MQTT without masking or reconstructing
it. The server stores the raw integer as the InfluxDB `status_flags` field and
decodes the three battery booleans with independent bitwise AND operations.
Unknown future bits therefore survive the complete data path.

The REST API exposes `status_flags`, `battery_measurement_ok`, `battery_low`,
and `battery_shutdown`. Historical records without `status_flags` expose these
values as unavailable (`null`) rather than assuming a valid zero flag. The
dashboard does not display `battery_mv` as a measurement unless the valid bit
is set; it displays a warning for the low bit and a critical state for the
shutdown bit.

Normal stale-node detection continues after shutdown. A stale node whose final
packet contained `STATUS_BATTERY_SHUTDOWN` is explicitly labeled as stopped
after battery shutdown, while a node that vanished without that bit remains an
unexplained stale node.

The conversion uses 64-bit integer arithmetic and nearest-millivolt rounding:

```text
midpoint_mv = rounded average of 32 calibrated ADC results
battery_mv = midpoint_mv × (R_top + R_bottom) / R_bottom
           = midpoint_mv × (1,000,000 + 1,000,000) / 1,000,000
```

The result is clamped only to the packet field's `UINT16_MAX` limit. A
successfully converted unusual reading is preserved and logged. Firmware warns
above 4300 mV because that suggests a wiring or calibration problem.

## Low-Battery Shutdown

The thresholds intentionally leave margin above the cell manufacturer's
absolute discharge limit:

- 3400 mV is the low-battery warning threshold.
- 3200 mV is this project's conservative software shutdown threshold.
- 2500 mV is the EVE ICR18650/26V manufacturer's specified discharge cutoff.
  It is used only for critical logging and documentation; it is not the normal
  firmware shutdown target.

Two consecutive valid calibrated readings at or below 3200 mV are required.
The confirmation counter is retained in RTC memory across the normal 15-minute
deep-sleep cycles and saturates at two. A reading above 3200 mV resets the
counter. A failed ADC acquisition, unavailable calibration, or zero
`battery_mv` value does not increment the counter or initiate shutdown, even if
the calibration API returned success.

After the second qualifying reading, the node still attempts one final SHT41
measurement and ESP-NOW packet. That packet contains the measured whole-battery
voltage plus `STATUS_BATTERY_LOW` and `STATUS_BATTERY_SHUTDOWN`. After the send
attempt completes or times out, firmware disables all configured wakeup sources
and enters deep sleep without enabling the normal 15-minute timer.

Indefinite deep sleep is not a physical battery disconnect and the board still
draws residual current. Recharge or replace the battery, then power-cycle or
reset the node to resume operation. Firmware measurement and shutdown are not a
substitute for physical cell over-discharge protection.

## Calibration and Validation

The firmware uses ESP-IDF v6.0.1's ADC oneshot driver with 12 dB attenuation,
default bit width, and the ESP32-C3 curve-fitting calibration scheme. It does
not fall back to an uncalibrated raw-count conversion if calibration is
unavailable.

ADC and resistor tolerances can create measurement error. After assembly,
compare the reported whole-battery voltage with a direct multimeter reading at
several cell voltages. The resistor values remain named as
`BATTERY_R_TOP_OHMS` and `BATTERY_R_BOTTOM_OHMS` so measured resistor values can
be entered if justified. Do not add an empirical correction factor without
actual measurements.

## Authoritative References

- [ESP-IDF release-v6.0 ADC oneshot driver](https://docs.espressif.com/projects/esp-idf/en/release-v6.0/esp32c3/api-reference/peripherals/adc/adc_oneshot.html)
- [ESP-IDF v6.0.1 ADC calibration driver](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-reference/peripherals/adc_calibration.html)
- [Seeed Studio XIAO ESP32-C3 hardware overview and pin map](https://wiki.seeedstudio.com/XIAO_ESP32C3_Getting_Started/#hardware-overview)
- [Seeed Studio XIAO ESP32-C3 schematic](https://files.seeedstudio.com/wiki/XIAO_WiFi/Resources/XIAO_ESP32C3_v1.3_SCH_260116.pdf)
- [ESP-IDF v6.0 ESP32-C3 sleep modes](https://docs.espressif.com/projects/esp-idf/en/v6.0/esp32c3/api-reference/system/sleep_modes.html)
- [EVE 18650/26V official product specifications](https://www.evemall.eu/consumer-battery/cylindrical-ncm-cell/18650-26v)
- [EVE ICR18650/26V manufacturer product specification, PBRI-ICR18650/26V-D06-37](https://www.nkon.nl/en/amfile/file/download/file/556/product/5745/)
