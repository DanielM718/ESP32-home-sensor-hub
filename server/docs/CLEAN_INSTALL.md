# Clean Raspberry Pi OS Lite Deployment

This is the end-to-end native install path for Raspberry Pi OS Lite 64-bit.
Run these commands on the Raspberry Pi, not on the Mac.

## 1. Prepare The Pi

Install Raspberry Pi OS Lite 64-bit, enable SSH if desired, set the hostname to
`sensor-pi`, connect it to your LAN, and copy this repository to the Pi.

Example copy target:

```text
~/sensor_home
```

## 2. Run Base Installer

```bash
cd ~/sensor_home/server
sudo ./install.sh --no-enable-services
```

This copies the project to:

```text
/opt/home-sensor/server
```

It also creates the `home-sensor` user, Python virtual environment, frontend
Chart.js asset, `.env` file, and systemd unit files.

## 3. Edit Secrets

```bash
sudo nano /opt/home-sensor/server/backend/.env
```

Set non-default values for:

- `MQTT_PASSWORD`
- `GRAFANA_ADMIN_PASSWORD`

InfluxDB tokens are populated later by `setup_influxdb.sh`.

## 4. Configure Mosquitto

```bash
sudo /opt/home-sensor/server/scripts/install_mosquitto.sh
sudo /opt/home-sensor/server/scripts/create_mqtt_users.sh
sudo systemctl restart mosquitto.service
sudo /opt/home-sensor/server/scripts/verify_mqtt.sh
```

Use the same bridge MQTT password in `backend/.env`.

Configure the external ESP32 gateway to publish with:

```text
username: home_sensor_gateway
topic: home/sensors/<node_id>
```

## 5. Configure InfluxDB

```bash
sudo /opt/home-sensor/server/scripts/install_influxdb.sh
sudo /opt/home-sensor/server/scripts/setup_influxdb.sh
sudo /opt/home-sensor/server/scripts/verify_influxdb.sh
```

Store the printed admin token in a password manager. The script writes scoped
read/write application tokens into `backend/.env`.

## 6. Configure Grafana

Confirm `GRAFANA_ADMIN_PASSWORD` is set in:

```text
/opt/home-sensor/server/backend/.env
```

Then run:

```bash
sudo /opt/home-sensor/server/scripts/install_grafana.sh
sudo /opt/home-sensor/server/scripts/provision_grafana.sh
sudo /opt/home-sensor/server/scripts/verify_grafana.sh
```

## 7. Configure Tailscale

Interactive:

```bash
sudo /opt/home-sensor/server/scripts/install_tailscale.sh --hostname sensor-pi
```

Or with a setup-only auth key:

```bash
export TAILSCALE_AUTHKEY='<tskey-auth-...>'
sudo --preserve-env=TAILSCALE_AUTHKEY /opt/home-sensor/server/scripts/install_tailscale.sh \
  --hostname sensor-pi \
  --advertise-tags tag:home-sensor
unset TAILSCALE_AUTHKEY
```

Verify:

```bash
/opt/home-sensor/server/scripts/verify_tailscale.sh
```

## 8. Start Backend Services

```bash
sudo /opt/home-sensor/server/scripts/install_systemd_units.sh --enable --start
sudo systemctl status home-sensor-bridge.service --no-pager
sudo systemctl status home-sensor-dashboard.service --no-pager
```

## 9. Smoke Test

Publish a test reading from the Pi:

```bash
mosquitto_pub -h 127.0.0.1 -p 1883 \
  -u home_sensor_gateway \
  -P '<gateway-password>' \
  -t 'home/sensors/1' \
  -m '{"node_id":1,"sequence":1,"temperature_c":24.8,"humidity":41.6,"battery_mv":4058,"status_flags":0}'
```

Check the API:

```bash
curl http://127.0.0.1:8080/api/latest
curl 'http://127.0.0.1:8080/api/readings?range=24h'
curl http://127.0.0.1:8080/api/nodes
```

Open:

```text
http://sensor-pi.local:8080
http://sensor-pi.local:3000
```

Remote access should use the Pi's Tailscale name or Tailscale IP.

## 10. Full Verification

```bash
sudo /opt/home-sensor/server/scripts/verify_all.sh
```

If any check fails, inspect service logs:

```bash
sudo journalctl -u home-sensor-bridge.service -n 100 --no-pager
sudo journalctl -u home-sensor-dashboard.service -n 100 --no-pager
sudo journalctl -u mosquitto.service -n 100 --no-pager
sudo journalctl -u influxdb.service -n 100 --no-pager
sudo journalctl -u grafana-server.service -n 100 --no-pager
```
