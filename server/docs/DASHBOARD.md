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
- `/api/latest` is polled every 7 seconds for current readings and node status.
- `/api/nodes` remains available to API clients, but the dashboard uses the node
  snapshot included with `/api/latest` to avoid repeating the same InfluxDB
  latest-value query.
- `/api/readings` is refreshed when the selected range changes.
- Supported ranges are `1h`, `24h`, `7d`, and `30d`.
- Refreshes do not overlap: the periodic poll waits while a full refresh is in
  progress, and the refresh button is disabled until its query completes.

## Displayed Data

- current temperature and humidity by node/station
- calibrated battery voltage for battery nodes when `STATUS_BATTERY_OK` is set
- raw status flags plus decoded battery measurement, low, and shutdown states
- all nine current SEN66 values, grouped as climate, gas/indices, and
  particulate matter, with plain-language status and source authority
- station summary driven transparently by the worst available current pollutant
- stale/invalid/warm-up visibility, last-update age, 15-minute mean/max/trend,
  rolling 24-hour PM context, and active-event state
- historical temperature and humidity for both SHT41 nodes and SEN66 stations
- a SEN66 gas/index chart for CO2, VOC Index, and NOx Index
- a SEN66 particulate chart for PM1.0, PM2.5, PM4.0, and PM10
- historical battery voltage for SHT41 battery nodes
- node online/stale status

Dashboard units are degrees Celsius (`°C`), relative humidity percent (`%`),
CO2 parts per million (`ppm`), particulate mass concentration (`µg/m³`), and
unitless VOC/NOx `index` values. The gas chart gives CO2 and the two indices
separate axes so the lower index values remain readable. Particulate sizes
share one chart because they use the same unit.

The current dashboard does not label every threshold as a regulatory limit.
EPA PM breakpoints, WHO PM guidelines, Sensirion index guidance, a CO2
ventilation heuristic, a separate occupational CO2 comparison, and
temperature/RH context each name their framework and limitations. Source links
are expandable per metric. PM cards distinguish instantaneous provisional
context from coverage-qualified rolling 24-hour context. Historical charts
show average and maximum series separately, keep sparse event markers
unconnected, and label whether the response came from one-minute live data or
verified persistent 15-minute aggregate tier.
Stored p95 series for CO2, PM2.5, PM10, VOC Index, and NOx Index are available as
hidden legend toggles on long-range charts, alongside hidden maxima; primary
means remain visible by default to keep the graphs readable.

Old air-quality records that contain only a subset of these fields remain
supported. Current cards show `-` for a missing value, chart datasets are
created only when a field has at least one numeric point, and absent fields do
not hide the station or affect the SHT41 display.

The current-reading card and node table show battery measurement unavailable
when `BIT2` is clear or `status_flags` is missing. `BIT3` produces a visible
low-battery warning, while `BIT4` produces a critical shutdown state. If the
final shutdown packet later becomes stale, the row remains stale but is labeled
`stale - battery shutdown`; a node that disappears without `BIT4` remains a
normal unexplained stale node. The historical battery chart includes only
points paired with a same-timestamp `STATUS_BATTERY_OK` bit, so legacy records
without status are not presented as measurements. No battery percentage is
estimated.

The status display always retains the raw unsigned integer in decimal and
hexadecimal, then labels every known SHT41 bit: `BIT0` SHT41 read OK, `BIT1`
ESP-NOW send attempted, `BIT2` battery measurement OK, `BIT3` low battery, and
`BIT4` confirmed battery shutdown. Any additional bits are shown as an unknown
hexadecimal mask rather than discarded.

## Official References

- Chart.js installation: <https://www.chartjs.org/docs/latest/getting-started/installation.html>
- Chart.js line charts: <https://www.chartjs.org/docs/latest/charts/line.html>
- Chart.js responsive charts: <https://www.chartjs.org/docs/latest/configuration/responsive.html>
