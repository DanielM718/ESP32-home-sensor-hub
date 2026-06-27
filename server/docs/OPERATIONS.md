# Operations

Operational docs will be expanded as services are added. The target operating
model is:

- systemd manages the bridge and dashboard
- logs are viewed with `journalctl`
- Mosquitto receives authenticated gateway messages
- InfluxDB stores all historical data
- Grafana provides primary analytics
- Flask provides a lightweight current-state view and REST API

## Health Checks

Milestone 2 and later milestones will add install and verification scripts.
The final verification path will check:

- service user exists
- virtual environment exists
- environment file permissions are restricted
- Mosquitto auth config is present
- InfluxDB is reachable
- bridge service can connect to MQTT and InfluxDB
- dashboard health endpoint responds locally
- Grafana datasource is configured
- Tailscale status is available

Run the complete verification suite as root:

```bash
sudo /opt/home-sensor/server/scripts/verify_all.sh
```

## Bridge Smoke Test

After Mosquitto and InfluxDB are configured, start the bridge on the Pi:

```bash
sudo systemctl restart home-sensor-bridge.service
sudo journalctl -u home-sensor-bridge.service -f
```

Publish a test payload as the gateway user:

```bash
mosquitto_pub -h 127.0.0.1 -p 1883 -u home_sensor_gateway -P '<gateway-password>' -t 'home/sensors/1' -m '{"node_id":1,"sequence":1,"temperature_c":24.8,"humidity":41.6,"battery_mv":4058,"status_flags":0}'
```

Expected result: the bridge logs a successful write at debug level, or no warning
at info level. The data should appear in the InfluxDB `environment` bucket as an
`environment_reading`.

The base verification script is:

```bash
/opt/home-sensor/server/scripts/verify_install.sh
```

It checks the service user, deployment root, virtual environment, `.env`
permissions, and systemd unit installation.

After Mosquitto setup, run:

```bash
/opt/home-sensor/server/scripts/verify_mqtt.sh
```

It checks the broker/client commands, installed config, installed ACL, password
file permissions, and expected MQTT users in the ACL.

After InfluxDB setup, run:

```bash
/opt/home-sensor/server/scripts/verify_influxdb.sh
```

It checks the `influx` and `influxd` commands, InfluxDB ping, systemd service
registration, and access to the configured bucket using the backend token.

## Dashboard API Smoke Test

After the Flask API milestone is installed on the Pi:

```bash
sudo systemctl restart home-sensor-dashboard.service
curl http://127.0.0.1:8080/api/health
curl http://127.0.0.1:8080/api/latest
curl 'http://127.0.0.1:8080/api/readings?range=24h'
curl http://127.0.0.1:8080/api/nodes
```

Expected result: `/api/health` returns `{"status":"ok"}` and the data endpoints
return JSON from InfluxDB.

The same checks are wrapped in:

```bash
/opt/home-sensor/server/scripts/verify_api.sh
```

The browser dashboard is available at:

```text
http://sensor-pi.local:8080
```

If charts do not render, verify that the Chart.js asset exists:

```bash
ls -l /opt/home-sensor/server/frontend/static/vendor/chart.umd.min.js
```

## Grafana Smoke Test

After Grafana provisioning:

```bash
sudo systemctl restart grafana-server.service
/opt/home-sensor/server/scripts/verify_grafana.sh
```

Then open:

```text
http://sensor-pi.local:3000
```

Expected result: Grafana loads with the `Home Sensor Environment` dashboard
under the `Home Sensor` folder.

## Tailscale Smoke Test

After Tailscale setup:

```bash
/opt/home-sensor/server/scripts/verify_tailscale.sh
tailscale status
tailscale ip -4
```

From another Tailnet device, open:

```text
http://sensor-pi:8080
http://sensor-pi:3000
```

If MagicDNS is not enabled, use the Pi's Tailscale IP instead of `sensor-pi`.

## systemd Services

Milestone 2 adds:

- `home-sensor-bridge.service`
- `home-sensor-dashboard.service`

The services run as `home-sensor` and load environment variables from:

```text
/opt/home-sensor/server/backend/.env
```

Their working directory is:

```text
/opt/home-sensor/server/backend
```

After the later configuration milestones are complete, typical service commands
on the Raspberry Pi are:

```bash
sudo systemctl daemon-reload
sudo systemctl enable home-sensor-bridge.service home-sensor-dashboard.service
sudo systemctl start home-sensor-bridge.service home-sensor-dashboard.service
sudo journalctl -u home-sensor-bridge.service -f
sudo journalctl -u home-sensor-dashboard.service -f
```

Official systemd references:

- <https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html>
- <https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html>
