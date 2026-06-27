# Optional Docker Compose Mirror

Native Raspberry Pi OS Lite deployment is the primary target. The Docker Compose
file is an optional local mirror for development or lab testing.

## Files

```text
server/docker-compose.yml
server/Dockerfile
server/.dockerignore
server/config/mosquitto/docker-mosquitto.conf
server/config/grafana/provisioning/datasources/home-sensor-influxdb.docker.yml
```

## Setup

Create a Docker-focused environment file from the example:

```bash
cd server
cp .env.example .env
```

Edit `.env` and set non-default values for:

- `MQTT_PASSWORD`
- `INFLUXDB_TOKEN`
- `INFLUXDB_ADMIN_PASSWORD`
- `GRAFANA_ADMIN_PASSWORD`

The Compose mirror uses `INFLUXDB_TOKEN` as the local all-purpose InfluxDB token.
The native Pi deployment still uses scoped read/write tokens.

Create Mosquitto users in the named config volume:

```bash
docker compose run --rm mosquitto mosquitto_passwd -c /mosquitto/config-runtime/passwd home_sensor_gateway
docker compose run --rm mosquitto mosquitto_passwd /mosquitto/config-runtime/passwd home_sensor_bridge
```

Use the same bridge password in `.env` as `MQTT_PASSWORD`.

Start the stack:

```bash
docker compose up --build
```

Local URLs:

```text
http://127.0.0.1:8080
http://127.0.0.1:3000
```

MQTT is bound to localhost by default:

```text
127.0.0.1:1883
```

## Limitations

- Compose uses container networking and is not the production Raspberry Pi
  deployment model.
- Compose does not configure Tailscale.
- Compose defaults are for local testing; use strong secrets if binding beyond
  localhost.
- Native scripts and systemd units remain the source of truth for Pi deployment.
