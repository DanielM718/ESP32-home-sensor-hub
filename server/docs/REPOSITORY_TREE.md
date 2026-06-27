# Repository Tree

Generated Raspberry Pi backend files live under `server/`.

```text
server/
|-- backend/
|   |-- app/
|   |   |-- __init__.py
|   |   |-- config.py
|   |   |-- influx.py
|   |   |-- models.py
|   |   |-- queries.py
|   |   |-- validation.py
|   |   `-- web.py
|   |-- bridge/
|   |   |-- __init__.py
|   |   |-- mqtt_bridge.py
|   |   `-- topic_router.py
|   |-- tests/
|   |   |-- test_queries.py
|   |   `-- test_topic_router.py
|   `-- .env.example
|-- config/
|   |-- grafana/
|   |   |-- dashboards/
|   |   |   `-- home-sensor-environment.json
|   |   `-- provisioning/
|   |       |-- dashboards/
|   |       |   `-- home-sensor-dashboards.yml
|   |       `-- datasources/
|   |           |-- home-sensor-influxdb.docker.yml
|   |           `-- home-sensor-influxdb.yml.tmpl
|   |-- influxdb/
|   |   |-- README.md
|   |   `-- schema.md
|   |-- mosquitto/
|   |   |-- README.md
|   |   |-- docker-mosquitto.conf
|   |   |-- home-sensor.acl
|   |   `-- home-sensor.conf
|   `-- tailscale/
|       |-- README.md
|       `-- tailnet-policy-example.hujson
|-- docs/
|   |-- API.md
|   |-- ARCHITECTURE.md
|   |-- BRIDGE.md
|   |-- CLEAN_INSTALL.md
|   |-- DASHBOARD.md
|   |-- DEPLOYMENT.md
|   |-- DOCKER.md
|   |-- FINAL_REVIEW.md
|   |-- GRAFANA.md
|   |-- INFLUXDB.md
|   |-- MQTT.md
|   |-- OPERATIONS.md
|   |-- REPOSITORY_TREE.md
|   |-- SECURITY.md
|   `-- TAILSCALE.md
|-- frontend/
|   |-- static/
|   |   |-- app.js
|   |   |-- styles.css
|   |   `-- vendor/
|   |       `-- README.md
|   `-- templates/
|       `-- index.html
|-- scripts/
|   |-- bootstrap_python.sh
|   |-- common.sh
|   |-- create_mqtt_users.sh
|   |-- create_service_user.sh
|   |-- install_base_packages.sh
|   |-- install_frontend_assets.sh
|   |-- install_grafana.sh
|   |-- install_influxdb.sh
|   |-- install_mosquitto.sh
|   |-- install_systemd_units.sh
|   |-- install_tailscale.sh
|   |-- provision_grafana.sh
|   |-- setup_influxdb.sh
|   |-- verify_all.sh
|   |-- verify_api.sh
|   |-- verify_grafana.sh
|   |-- verify_influxdb.sh
|   |-- verify_install.sh
|   |-- verify_mqtt.sh
|   `-- verify_tailscale.sh
|-- systemd/
|   |-- home-sensor-bridge.service
|   `-- home-sensor-dashboard.service
|-- .dockerignore
|-- .env.example
|-- Dockerfile
|-- README.md
|-- docker-compose.yml
|-- install.sh
`-- requirements.txt
```
