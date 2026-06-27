# Grafana Provisioning

This directory contains Grafana datasource and dashboard provisioning assets.

Templates and dashboards are copied to the Raspberry Pi by:

```bash
/opt/home-sensor/server/scripts/provision_grafana.sh
```

The datasource file is committed as a template so the InfluxDB read token is not
stored in the repository. The rendered datasource file on the Pi is installed
with restricted permissions.

See `server/docs/GRAFANA.md` for deployment details.
