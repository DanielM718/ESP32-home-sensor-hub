# Grafana

Grafana is the primary analytics dashboard. The Flask app is a lightweight
overview dashboard and REST API.

Official references:

- <https://grafana.com/docs/grafana/latest/setup-grafana/installation/debian/>
- <https://grafana.com/docs/grafana/latest/administration/provisioning/>

## Provisioning Plan

Milestone 8 adds provisioning files for:

- InfluxDB datasource
- environmental sensor dashboard
- battery/status panels
- air-quality dashboard panels

Generated files:

```text
server/config/grafana/provisioning/datasources/home-sensor-influxdb.yml.tmpl
server/config/grafana/provisioning/dashboards/home-sensor-dashboards.yml
server/config/grafana/dashboards/home-sensor-environment.json
server/scripts/install_grafana.sh
server/scripts/provision_grafana.sh
server/scripts/verify_grafana.sh
```

The datasource file in the repository is a template. The Pi-side provisioning
script renders it with the InfluxDB read token from `server/backend/.env` and
installs the rendered file with restricted permissions.

## Raspberry Pi Setup

Run after InfluxDB setup has created `INFLUXDB_READ_TOKEN`:

```bash
sudo /opt/home-sensor/server/scripts/install_grafana.sh
sudo /opt/home-sensor/server/scripts/provision_grafana.sh
sudo /opt/home-sensor/server/scripts/verify_grafana.sh
```

Before provisioning, set a non-default password in:

```text
/opt/home-sensor/server/backend/.env
```

```text
GRAFANA_ADMIN_PASSWORD=change-this-grafana-password
```

`provision_grafana.sh` uses `grafana cli ... reset-admin-password` when
`GRAFANA_ADMIN_PASSWORD` is available.

## Installed Paths

Datasource provisioning:

```text
/etc/grafana/provisioning/datasources/home-sensor-influxdb.yml
```

Dashboard provider:

```text
/etc/grafana/provisioning/dashboards/home-sensor-dashboards.yml
```

Dashboard JSON:

```text
/var/lib/grafana/dashboards/home-sensor/home-sensor-environment.json
```

## Dashboard

The generated dashboard is titled `Home Sensor Environment` and includes:

- temperature by node
- humidity by node
- battery voltage by node
- air-quality overview for direct-MQTT stations
- latest battery voltage stat panel
- latest node status table

The Flux queries target the `environment_reading` and `air_quality_reading`
measurements created by the bridge.

## Access

Grafana is for LAN or Tailscale access only. Do not expose Grafana directly to
the public internet.

Default local URL:

```text
http://sensor-pi.local:3000
http://<raspberry-pi-ip>:3000
```

Remote access must go through Tailscale.
