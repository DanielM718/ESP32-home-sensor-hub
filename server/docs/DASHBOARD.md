# Frontend Dashboard

The Flask app serves the overview dashboard at:

```text
http://sensor-pi.local:8080
http://<raspberry-pi-ip>:8080
```

The frontend uses Chart.js for historical graphs and calls only the Flask REST
API. It never reads from MQTT directly.

## Files

```text
server/frontend/templates/index.html
server/frontend/static/styles.css
server/frontend/static/app.js
server/frontend/static/vendor/chart.umd.min.js
```

`chart.umd.min.js` is installed on the Raspberry Pi by:

```bash
sudo /opt/home-sensor/server/scripts/install_frontend_assets.sh
```

The main `install.sh` runs this script by default. For offline installation, use:

```bash
sudo ./install.sh --no-frontend-assets
```

Then copy or download the Chart.js browser bundle before using the dashboard.

## Behavior

- `/` renders the dashboard.
- `/api/latest` is polled every 7 seconds for current readings.
- `/api/nodes` is polled every 7 seconds for node status.
- `/api/readings` is refreshed when the selected range changes.
- Supported ranges are `1h`, `24h`, `7d`, and `30d`.

## Displayed Data

- current temperature and humidity by node/station
- battery voltage for battery nodes
- status flags for battery nodes
- historical temperature, humidity, battery, CO2, and PM2.5 charts
- node online/stale status

## Official References

- Chart.js installation: <https://www.chartjs.org/docs/latest/getting-started/installation.html>
- Chart.js line charts: <https://www.chartjs.org/docs/latest/charts/line.html>
- Chart.js responsive charts: <https://www.chartjs.org/docs/latest/configuration/responsive.html>
