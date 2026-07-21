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
mosquitto_pub -h 127.0.0.1 -p 1883 -u home_sensor_gateway -P '<gateway-password>' -t 'home/sensors/1' -m '{"node_id":1,"sequence":1,"temperature_c":24.8,"humidity":41.6,"battery_mv":4058,"status_flags":4}'
```

Expected result: the bridge logs a successful write at debug level, or no warning
at info level. The data should appear in the InfluxDB `environment` bucket as an
`environment_reading`.

For the full SEN66 path, first watch the air topics:

```bash
mosquitto_sub -h 127.0.0.1 -p 1883 \
  -u home_sensor_bridge -P '<bridge-password>' \
  -t 'home/air/#' -v
```

Then use the checked-in full payload and wait for all nine fields to reach the
API:

```bash
MQTT_PUBLISH_PASSWORD='<gateway-password>' \
  /opt/home-sensor/server/scripts/verify_sen66.sh
```

The script publishes to `home/air/sen66_test` by default. It verifies
temperature, humidity, CO2, all four PM sizes, VOC Index, and NOx Index. Check
both Python services if it fails:

```bash
sudo journalctl -u home-sensor-bridge.service --since '10 minutes ago' --no-pager
sudo journalctl -u home-sensor-dashboard.service --since '10 minutes ago' --no-pager
```

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
registration, and access to both the configured long-term and live buckets.
SEN66 high-resolution points appear in `environment_live`; wait through a UTC
quarter-hour to verify `air_quality_15m` in `environment`, or follow the exact
queries in [`SEN66_AIR_QUALITY.md`](SEN66_AIR_QUALITY.md).

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

If the dashboard briefly reports `backend query failed`, the Flask process is
still responsive enough to return an HTTP 503; this message alone does not mean
the server is hung. Identify the failing endpoint and its duration with:

```bash
for path in api/health api/latest 'api/readings?range=24h' api/nodes; do
  curl --silent --show-error --output /dev/null \
    --write-out "${path} HTTP %{http_code} in %{time_total}s\n" \
    "http://127.0.0.1:8080/${path}"
done
sudo journalctl -u home-sensor-dashboard.service --since '10 minutes ago' --no-pager
sudo systemctl show home-sensor-dashboard.service \
  --property=ActiveState,SubState,NRestarts,MemoryCurrent,CPUUsageNSec
```

The browser error now includes the endpoint that failed. The journal contains
the underlying InfluxDB exception and traceback. A fast `/api/health` response
with a slow or failing data endpoint indicates an InfluxDB/query problem rather
than a hung Gunicorn process.

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
