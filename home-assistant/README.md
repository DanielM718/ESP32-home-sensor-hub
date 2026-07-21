# Home Assistant automation layer

This directory adds Home Assistant as an isolated, optional consumer of the
existing MQTT stream. It does not replace or reconfigure the Python bridge,
InfluxDB, Grafana, Flask dashboard, or their systemd units.

```text
SHT41 nodes --ESP-NOW--> ESP32 gateway --+
                                             +--> Mosquitto --> Python bridge --> InfluxDB --> Grafana
SEN66 node --------Wi-Fi/MQTT--------------+       |                         `--> Flask dashboard
                                                    `--> Home Assistant --> TP-Link P115 plugs
```

The deployed paths remain separate:

```text
/opt/home-sensor/server   existing native server stack
/opt/home-assistant       Home Assistant Compose project and persistent config
```

Home Assistant uses the official stable container with host networking,
`TZ=America/New_York`, a persistent `/opt/home-assistant/config` bind mount, and
`restart: unless-stopped`. Host networking is required for reliable local Tapo
discovery. Port 8123 is not published with a Docker port mapping because host
networking makes it available directly on the Pi. Do not forward 8123, 1883,
3000, 8080, or 8086 through the router; use Tailscale for remote access.

## Repository audit

The current native deployment uses these unchanged subscriptions:

| Source | Topic | Payload fields used by Home Assistant |
| --- | --- | --- |
| SHT41 through gateway | `home/sensors/<node_id>` | `node_id`, `sequence`, `temperature_c`, `humidity`, `battery_mv`, optional compatibility `status_flags` (current gateway always includes it), and current `packet_type` |
| Direct SEN66 | `home/air/<location>` | `temperature_c`, `humidity`, `co2`, `pm1`, `pm25`, `pm4`, `pm10`, `voc_index`, `nox_index`, plus `packet_type`, `schema_version`, `firmware_version`, `node_id`, `sequence`, and `status_flags` diagnostics |

The Python bridge subscribes to `home/sensors/+` and `home/air/+`, validates the
measurement fields, assigns Pi receive time, and ignores unknown JSON keys. The
Flask dashboard reads InfluxDB rather than changing MQTT data. Native deployment
uses `home-sensor-bridge.service` and `home-sensor-dashboard.service`. The
repository also contains an optional `server/docker-compose.yml` for the
existing stack; this Home Assistant Compose project is independent of it.

Existing server environment variables include logging, Flask/Gunicorn, node
staleness, MQTT host/port/client/QoS/topic/credentials, InfluxDB URL/org/bucket
and tokens, and Grafana credentials. Home Assistant adds only its ignored
`/opt/home-assistant/.env`; no existing `.env` is read or changed.

The topics and JSON keys are stable enough for discovery. Node identity comes
from the integer SHT41 topic/payload ID or the validated SEN66 location slug.
No firmware or source payload change is required.

## Discovery design

The `mqtt-discovery` companion is deliberately separate from the deployed
bridge. It:

- subscribes to the two existing topic filters without consuming messages away
  from any other MQTT subscriber;
- strictly validates required current fields and logs malformed or missing
  values without refreshing health;
- creates retained configs under
  `homeassistant/<component>/<device>/<entity>/config`;
- assigns deterministic configuration topics, `unique_id` values, suggested
  entity IDs, and one Home Assistant device per node/location;
- persists the identities of devices it has seen in
  `/opt/home-assistant/discovery-data/devices.json`;
- republishes known configs once on service connect and when Home Assistant
  publishes its birth message to `homeassistant/status`;
- publishes config only for a new device or changed firmware metadata during
  normal message flow, rather than with every reading;
- derives retained `last_seen` and online/offline state without changing the
  source payload;
- marks a device offline after `SENSOR_STALE_AFTER_SECONDS`; each measurement
  also uses Home Assistant `expire_after` as an independent fallback;
- publishes its own last will so its entities become unavailable if the
  companion stops.

Separate single-component discovery messages are intentional here. Measurement
entities consume the original JSON topics, while connectivity and last-packet
entities consume derived topics and have different availability behavior. All
configs still share stable device identifiers, so Home Assistant groups them.

SHT41 battery voltage is exposed only when status bit 2 (`4`, battery valid) is
set; low and shutdown bits are diagnostics. SEN66 PM1, PM2.5, PM4, PM10, CO2,
temperature, humidity, VOC Index, NOx Index, status, sequence, schema, firmware,
last packet, device warning, and connectivity are exposed. VOC and NOx are
dimensionless indices, so they intentionally do not claim concentration device
classes.

Suggested discovery entity IDs follow the source identity and do not change
when a friendly device name changes:

```text
sensor.sht41_node_<node_id>_temperature
sensor.sht41_node_<node_id>_humidity
binary_sensor.sht41_node_<node_id>_online
sensor.sen66_<location>_carbon_dioxide
sensor.sen66_<location>_pm2_5
binary_sensor.sen66_<location>_online
```

Keep Tapo entity IDs location/function oriented (`switch.printer_ventilation`)
rather than model oriented, because automations should survive plug replacement.

## Prerequisites

The Pi must already have the existing server stack and working MQTT messages.
The install script verifies Linux, Docker Engine, Docker Compose v2, the Docker
daemon, and `rsync`; it does not silently install or upgrade system packages.

Check first:

```bash
docker --version
docker compose version
sudo docker info
command -v rsync
```

If Docker is absent, install Docker Engine and the Compose plugin from Docker's
official Debian/Raspberry Pi instructions, review the firewall implications,
then verify with `sudo docker run --rm hello-world`. Do not use the convenience
script for an unattended production installation.

## One-time MQTT account and ACL

The dedicated `home_assistant` account can read both source families and read or
write only `homeassistant/#`. It cannot publish fake source readings. The same
account is used by Home Assistant and the discovery companion.

If the installed ACL uses the repository's default usernames, install the
updated ACL and create only the new account. The `--home-assistant-only` flag
preserves the existing gateway and bridge passwords:

```bash
sudo install -m 0644 -o root -g root \
  /opt/home-sensor/server/config/mosquitto/home-sensor.acl \
  /etc/mosquitto/acl.d/home-sensor.acl
sudo /opt/home-sensor/server/scripts/create_mqtt_users.sh --home-assistant-only
sudo systemctl restart mosquitto.service
sudo systemctl status mosquitto.service --no-pager
sudo env VERIFY_HOME_ASSISTANT_MQTT=1 /opt/home-sensor/server/scripts/verify_mqtt.sh
```

If the installed ACL has custom usernames or local rules, do not overwrite it.
Back it up and merge only this block:

```text
user home_assistant
topic read home/sensors/+
topic read home/air/+
topic readwrite homeassistant/#
```

Then run the account creation command and restart Mosquitto. A broker restart is
briefly disruptive, so immediately run the original-service checks below.

## Install on the Raspberry Pi

These commands assume the repository root is `/opt/home-sensor`, making the
existing server path `/opt/home-sensor/server`:

```bash
cd /opt/home-sensor
sudo ./home-assistant/scripts/install.sh
sudoedit /opt/home-assistant/.env
```

Set the password entered for `home_assistant`:

```dotenv
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_USERNAME=home_assistant
MQTT_PASSWORD=replace_with_the_real_password
SENSOR_STALE_AFTER_SECONDS=1800
```

Keep `/opt/home-assistant/.env` mode 0600. The first install starts the official
Home Assistant container even if the password is still a placeholder, but it
does not start the discovery companion until a real password exists.

Deploy and validate after saving the credential:

```bash
cd /opt/home-sensor
sudo ./home-assistant/scripts/deploy.sh
```

Open `http://<pi-lan-ip>:8123`, create the first Home Assistant owner, and set
the system location and timezone. The configuration persists under
`/opt/home-assistant/config` across container replacement and Pi restarts.

### Add the existing Mosquitto broker

Current Home Assistant versions configure broker connection credentials in the
UI, not in `configuration.yaml`:

1. Go to **Settings > Devices & services > Add integration > MQTT**.
2. Enter broker `127.0.0.1`, port `1883`, username `home_assistant`, and the same
   password from `/opt/home-assistant/.env`.
3. Leave discovery enabled and its prefix set to `homeassistant`.
4. Do not install the Home Assistant Mosquitto app/add-on or another broker.
5. Confirm the MQTT integration shows an SHT41 device after its next packet and
   the SEN66 device within its normal publishing interval.

`configuration/secrets.yaml.example` is provided as requested, but the MQTT UI
stores the real broker credential in `/config/.storage`. Both locations and the
companion `.env` are ignored by Git. Never add a Tapo or MQTT password to a
tracked YAML file.

## TP-Link Tapo P115 setup

Use Home Assistant's official **TP-Link Smart Home** integration; P115 is a
supported local-control plug and no custom implementation is needed.

1. In the Tapo app, add each P115, connect it to the 2.4 GHz home Wi-Fi, update
   it deliberately, and assign a clear name.
2. Reserve its IP address in the router. This is recommended for predictable
   rediscovery and troubleshooting, but is not a Home Assistant requirement.
3. If authentication fails, enable **Tapo Lab > Third-Party Compatibility** in
   the Tapo app. Some firmware requires this; it is not universally required.
4. In Home Assistant, go to **Settings > Devices & services**. Accept a
   discovered TP-Link device or select **Add integration > TP-Link Smart Home**.
5. For manual setup, enter the reserved plug IP plus the case-sensitive TP-Link
   cloud account email and password. Home Assistant uses them to authenticate
   local access; do not put them in this repository.
6. From the device page, toggle the plug on and off with a lamp or fan attached.
7. Confirm current power, current, voltage, and energy entities appear. Enable a
   disabled diagnostic entity from the device's **Entities** list if necessary.
8. Rename entities consistently, for example:

```text
switch.printer_room_dehumidifier
switch.printer_ventilation
switch.filament_dryer
switch.space_heater
sensor.printer_room_dehumidifier_current_power
sensor.printer_room_dehumidifier_today_energy
sensor.printer_room_dehumidifier_current
sensor.printer_room_dehumidifier_voltage
```

Entity availability depends on the plug model, hardware revision, firmware, and
the integration. A P115 may not expose every illustrative energy name above.

## Automation examples and manual control

Every item in `configuration/automations.yaml` has `initial_state: false`, so it
returns to disabled on every Home Assistant restart. Before promoting one to
normal use, test it, then remove that line or deliberately change it to `true`.
Before enabling one:

1. Replace example sensor and switch IDs with the entities on this installation.
2. Check that every **on** threshold is greater than its matching **off**
   threshold.
3. Set the discovery timeout and the helper timeout consistently. The 30-minute
   default accounts for the current 15-minute SHT41 sleep interval.
4. Validate configuration and enable one automation at a time.
5. Test with a lamp or fan before an unattended appliance.

The three-state mode avoids fighting manual commands:

- **Automatic** allows threshold/schedule rules to control the device and
  enforces their maximum runtime.
- **Manual** ignores threshold/schedule on/off actions and follows the matching
  Manual power helper. Stale-data safety can still turn the device off.
- **Disabled** forces the device off when the mode-control example is enabled.

The examples provide five-minute humidity hysteresis, multi-sensor ventilation
hysteresis and recovery time, a once-per-day 08:00 dryer schedule, restored
daily lockout with manual reset, maximum runtime, P115 power-start verification,
and stale-data fail-closed notifications. The start-verification script waits a
configurable time before checking power because many appliances delay their
load. `persistent_notification.create` works without a phone integration;
replace or supplement it with the desired `notify.mobile_app_*` action later.

The maximum runtime governs Automatic mode. Manual mode is intentionally not
immediately undone by threshold logic; the human operator remains responsible
for runtime, while stale/unavailable safety remains fail-closed.

### Heater safety

No heater automation is included. Do not enable unattended heater control
without an appliance-specific risk review. A heater must have its own thermostat
and overtemperature protection, should have tip-over protection, must not use an
extension cord or power strip, and must remain comfortably below the P115's
continuous current rating. A future heater example must default disabled, turn
off on stale data and Home Assistant restart, enforce a hard upper temperature,
maximum continuous runtime and minimum off-time, and never treat a smart plug as
the primary thermal safety system.

## Current-state dashboard

`examples/dashboard.yaml` is a starting point for current readings, online
state, battery, P115 controls, power/energy, helper modes, and recent control
activity. In Home Assistant, create a dashboard, open its raw configuration
editor, paste the file, and replace example entity IDs. Add a card for every
discovered node. Persistent warnings appear in the notification panel.

This dashboard intentionally does not recreate historical graphs. Grafana
remains the long-term analysis interface.

## Operations

Status and recent logs:

```bash
sudo /opt/home-assistant/scripts/status.sh
sudo docker compose --project-directory /opt/home-assistant \
  --env-file /opt/home-assistant/.env \
  -f /opt/home-assistant/compose.yaml ps
sudo docker inspect homeassistant
curl --fail --show-error http://127.0.0.1:8123/
```

Follow both logs or one service:

```bash
sudo /opt/home-assistant/scripts/logs.sh
sudo /opt/home-assistant/scripts/logs.sh homeassistant
sudo /opt/home-assistant/scripts/logs.sh mqtt-discovery
```

The status script reports container state, port 8123, broker TCP reachability,
the config directory, inspect state, and recent logs.

### Upgrade and redeploy

```bash
cd /opt/home-sensor
git pull --ff-only
sudo ./home-assistant/scripts/deploy.sh
```

Deployment validates Compose, pulls the current stable Home Assistant image,
validates a staged candidate configuration and the installed configuration,
backs up changed managed YAML, updates/rebuilds only this Compose project, and
shows status/logs. It never invokes `systemctl` or the server Compose project.

### Roll back

List the timestamped backups produced by deployment:

```bash
sudo find /opt/home-assistant/backups -mindepth 1 -maxdepth 1 -type d -print
```

Restore managed configuration from one selected timestamp and recreate only
Home Assistant:

```bash
BACKUP=/opt/home-assistant/backups/YYYYMMDDTHHMMSSZ
sudo rsync -a "$BACKUP/configuration/" /opt/home-assistant/config/
sudo docker compose --project-directory /opt/home-assistant \
  --env-file /opt/home-assistant/.env \
  -f /opt/home-assistant/compose.yaml up -d --force-recreate --no-deps homeassistant
```

If the previous image ID exists in `homeassistant-image.txt`, retag it locally
and recreate without pulling:

```bash
BACKUP=/opt/home-assistant/backups/YYYYMMDDTHHMMSSZ
OLD_IMAGE="$(sudo cat "$BACKUP/homeassistant-image.txt")"
sudo docker tag "$OLD_IMAGE" ghcr.io/home-assistant/home-assistant:stable
sudo docker compose --project-directory /opt/home-assistant \
  --env-file /opt/home-assistant/.env \
  -f /opt/home-assistant/compose.yaml up -d --force-recreate --no-deps homeassistant
```

### Uninstall

Preserve all persistent data by default:

```bash
sudo /opt/home-assistant/scripts/uninstall.sh
```

Permanently delete Home Assistant config, secrets, discovery registry, backups,
and its `.env` only with the explicit destructive flag:

```bash
sudo /opt/home-assistant/scripts/uninstall.sh --delete-data
```

Neither form removes or stops Mosquitto, InfluxDB, Grafana, Flask, the bridge,
or either existing systemd unit.

## Tailscale access

With Tailscale already installed on the Pi and client, find the Pi address and
open Home Assistant only from the tailnet:

```bash
tailscale ip -4
```

```text
http://<pi-tailscale-ip>:8123
```

Do not add router port forwarding. If a Tailscale ACL is in use, permit the
intended users/devices to reach the Pi on TCP 8123.

## Verification

### Original stack remains operational

```bash
sudo systemctl status home-sensor-bridge.service --no-pager
sudo systemctl status home-sensor-dashboard.service --no-pager
sudo systemctl status mosquitto.service influxdb.service grafana-server.service --no-pager
sudo /opt/home-sensor/server/scripts/verify_all.sh
curl --fail --show-error http://127.0.0.1:8080/api/health
curl --fail --show-error http://127.0.0.1:3000/api/health
```

Adjust service names only if this Pi intentionally uses the existing server
Docker Compose deployment instead of native services.

### MQTT discovery and sensor entities

```bash
mosquitto_sub -h 127.0.0.1 -u home_assistant -P '<mqtt-password>' \
  -t 'homeassistant/+/+/+/config' -v
mosquitto_sub -h 127.0.0.1 -u home_assistant -P '<mqtt-password>' \
  -t 'homeassistant/home_sensor/#' -v
```

In Home Assistant:

1. Open **Settings > Devices & services > MQTT**.
2. Confirm one device per SHT41 node and one per SEN66 location.
3. Confirm there is only one entity for each unique reading after restarting
   both containers.
4. Compare values with `mosquitto_sub` on `home/sensors/#` and `home/air/#`.
5. Stop one test publisher or temporarily set a short timeout in a controlled
   test. Confirm Online becomes off, measurement entities become unavailable,
   and an enabled stale-safety automation turns its plug off and warns.
6. Restore the production timeout and restart the discovery companion.

### Safe first plug test

1. Attach a lamp or fan well below the P115 rating; do not begin with a heater.
2. Toggle the switch from Home Assistant and verify local on/off response.
3. Confirm current power rises and returns near zero when off.
4. Run `script.verify_device_started` from Developer Tools with the switch,
   current-power entity, a conservative wait, and an expected minimum wattage.
5. Unplug the load or choose a deliberately high minimum to confirm the warning.
6. Keep all automatic examples disabled until this test passes.

## Troubleshooting and live-test boundary

- No MQTT devices: verify the UI broker entry, discovery prefix, dedicated ACL,
  source messages, and `mqtt-discovery` logs. A new sleeping SHT41 device is
  registered by its first packet; because source readings are deliberately not
  retained or republished, its initial entity values may populate on the next
  packet. Known devices are republished immediately from the companion registry.
- Entities duplicate: inspect retained config topics and unique IDs. This
  publisher uses deterministic topics/IDs; remove obsolete configs only after
  identifying the old publisher that created them.
- Sensor stays online: compare `SENSOR_STALE_AFTER_SECONDS` with actual publish
  intervals and watch `homeassistant/home_sensor/+/availability`.
- Tapo authentication: verify the case-sensitive account email, stable LAN
  reachability, and third-party compatibility; then re-add by IP.
- Port 8123 fails: inspect the container and logs, then run the Home Assistant
  config check used by `deploy.sh`.
- Compose validation on a development Mac confirms structure but cannot prove
  ARM image startup, LAN discovery, live ACLs, hardware timing, or P115 firmware
  behavior.

Static repository tests cover YAML parsing, shell syntax, deterministic unique
IDs/config topics, complete metric generation, malformed payload rejection, and
stale-boundary behavior. The exact live tests above remain required on the Pi
and physical plugs.

## References

- Home Assistant Container on Raspberry Pi:
  <https://www.home-assistant.io/installation/raspberrypi-other/>
- Home Assistant MQTT and discovery:
  <https://www.home-assistant.io/integrations/mqtt/>
- Home Assistant TP-Link Smart Home:
  <https://www.home-assistant.io/integrations/tplink/>
- Docker Engine on Debian:
  <https://docs.docker.com/engine/install/debian/>
