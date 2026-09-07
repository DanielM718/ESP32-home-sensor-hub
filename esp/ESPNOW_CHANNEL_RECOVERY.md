# ESP-NOW channel recovery

## Why recovery is required

ESP-NOW and infrastructure Wi-Fi share one 2.4 GHz radio. The gateway is a
Wi-Fi station carrying MQTT, so its radio follows the access point's current
channel and must not hop away to search for nodes. The former SHT41 firmware
forced channel 1 on every wake. When the access point and gateway were on
another channel, such as channel 6, ESP-NOW delivery was stale or intermittent.

This firmware makes the battery node responsible for reacquisition. The
gateway never calls `esp_wifi_set_channel()` and never leaves its associated
access-point channel.

## Node state machine

On each normal wake the node measures the sensor as before, initializes Wi-Fi
and ESP-NOW, and tries its last known gateway channel. A confirmed ESP-NOW
sensor send ends the communication cycle. If the send fails, or if no valid
cache exists after the first flash, the node performs a finite discovery scan.

The scan tries the cached channel first when one exists, then 1, 6, and 11,
then every remaining US 2.4 GHz channel through 11 with duplicates removed.
It sends one discovery request per candidate channel. Every ESP-NOW send wait
is limited to 500 ms, every discovery response wait is limited to 300 ms, and
the channel-settle delay is 10 ms. The list contains at most 11 channels, so an
outage cannot leave the node continuously awake.

When a validated response arrives, the node selects the reported channel and
sends the unchanged sensor packet. Only a confirmed sensor send records the
channel as working. Failure across the complete list returns to deep sleep.

## Discovery wire protocol version 1

Discovery is separate from the legacy 22-byte `sensor_packet_t`. Both control
structures are packed, use fixed-width integers, reserve bytes as zero, and
have compile-time size assertions. Multi-byte fields use the native
little-endian representation shared by the deployed ESP32 and ESP32-C3.

Request (16 bytes):

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 4 | magic `0x45534e43` |
| 4 | 1 | protocol version `1` |
| 5 | 1 | packet type `DISCOVERY_REQUEST` (`1`) |
| 6 | 2 | reserved, zero |
| 8 | 4 | node ID |
| 12 | 4 | random request nonce |

Response (20 bytes):

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 4 | magic `0x45534e43` |
| 4 | 1 | protocol version `1` |
| 5 | 1 | packet type `DISCOVERY_RESPONSE` (`2`) |
| 6 | 2 | reserved, zero |
| 8 | 4 | matching node ID |
| 12 | 4 | matching request nonce |
| 16 | 1 | gateway's current primary Wi-Fi channel |
| 17 | 3 | reserved, zero |

Requests are unicast to the already configured gateway STA MAC. The gateway
responds using its existing broadcast peer, avoiding dynamic unicast peers and
peer leaks. A node accepts a response only if it came from its fixed gateway
MAC (`d8:bc:38:e5:78:8c`) and all magic, version, type, size, reserved-byte,
node-ID, nonce, and channel checks pass. This is strict source and transaction
validation, not cryptographic authentication; encryption/authentication is
outside this channel-discovery feature.

The gateway reads `esp_wifi_get_channel()` for every valid request and reports
that current value. Recognized control frames, including malformed frames with
the discovery magic, never enter the legacy sensor-to-MQTT decoder.

## Cache, backoff, and battery behavior

The channel is held in `RTC_DATA_ATTR` for fast persistence across the normal
15-minute deep sleep. NVS provides recovery after a power cycle. NVS values are
range-checked, and flash is written only after a confirmed sensor send when the
working channel differs from the persisted value.

Successful communication clears the retained failure count and restores the
normal 15-minute interval. Consecutive failed wakes sleep for 1 minute, 2
minutes, 5 minutes, and then 15 minutes for every further failure. Semaphore
waits, response waits, and channel enumeration are all bounded. Existing
low-battery shutdown behavior still takes precedence.

The sensor packet layout, node-ID configuration, optional battery monitoring,
status flags, and MQTT output are unchanged. No router credentials are stored
on a node. OTA is explicitly deferred.

## One-time manual flash and validation

Current deployed nodes need one physical flash of this firmware. After that,
future access-point channel changes are recovered automatically and do not
require another channel-specific reflash.

Use node 1 as the first test unit: the checked-in source identifies it as
`NODE_ID 1` with `BATTERY_MONITOR_ENABLED 0`, so this test does not alter the
battery-enabled node's calibration/shutdown behavior. Confirm those two values
against the physical unit before flashing. Do not renumber deployed nodes.

1. Confirm from the gateway/AP logs or router status that the gateway is
   currently associated on channel 6.
2. Build the gateway for `esp32` and the selected node for `esp32c3`. Review
   the configured target before each flash. Do not reuse one target's build
   directory for the other target.
3. Flash the gateway recovery firmware first during an approved maintenance
   window. Confirm its STA MAC is `d8:bc:38:e5:78:8c`, it rejoins Wi-Fi, logs
   the current AP channel, and resumes legacy MQTT publishing.
4. Flash only the selected test node. Observe serial output showing no valid
   cache, bounded discovery, gateway discovery on channel 6, a confirmed
   sensor send, an NVS cache update, and normal 15-minute sleep.
5. Reset or power-cycle that node once and confirm it loads the cached channel,
   sends successfully without a full scan, and returns to normal sleep.
6. If access-point channel control is available in a controlled maintenance
   window, change the 2.4 GHz channel. Do not assume a Spectrum-managed device
   exposes this setting. Confirm the gateway reconnects and logs the new
   channel, then wake the test node. Its cached send should fail, discovery
   should find the new channel, and the report should arrive without reflashing.
7. Confirm the MQTT topic/payload is unchanged and that no discovery packet
   appears as a sensor MQTT message. Restore any temporary AP setting.
8. Only after the test node passes should the other nodes be flashed one at a
   time with each node's existing `NODE_ID` and battery-monitor setting.

If validation fails, restore both device types from baseline commit
`7560842162e14e9435faaf17f1c82a4bde0ff2da`; that rollback restores the old
fixed-channel behavior and therefore requires setting the old node channel to
the AP channel before building.
