# Security Model

Security defaults for the Raspberry Pi backend:

- Use a dedicated `home-sensor` service user.
- Run Python services as `home-sensor`, never root.
- Store secrets in `server/backend/.env`.
- Restrict `server/backend/.env` permissions to the service user.
- Disable anonymous MQTT access.
- Use non-default passwords and tokens.
- Keep services on LAN and Tailscale only.
- Do not expose ports directly to the public internet.
- Use systemd hardening options where compatible with the services.
- Use Python logging for production services.

## Secrets

Committed files may include examples only. Real values belong in:

```text
server/backend/.env
```

This file should be readable only by the `home-sensor` user and root on the
Raspberry Pi.

## Least Privilege

The bridge only needs MQTT read access for sensor topics and InfluxDB write
access for the environment bucket. The dashboard only needs InfluxDB read access
where practical.

The generated Mosquitto ACL applies this MQTT split:

- gateway user: write-only access to sensor and air-quality input topics
- bridge user: read-only access to sensor and air-quality input topics

Anonymous MQTT clients are disabled.

InfluxDB uses scoped application tokens:

- bridge: write access to the environment bucket
- dashboard/Grafana: read access to the environment bucket

The InfluxDB admin token is only for setup and maintenance. Do not place it in
systemd service files or frontend-visible configuration.

Grafana provisioning uses the InfluxDB read token only. The rendered datasource
file on the Pi contains that token and should remain root-owned with Grafana
group read access only.

Change the Grafana admin password before remote use. Do not leave the default
admin password in place.

## Remote Access

Remote access is through Tailscale only:

- do not expose Flask, Grafana, Mosquitto, or InfluxDB with router port forwarding
- do not enable Tailscale Funnel for this backend
- avoid subnet-router and exit-node modes unless a future design explicitly
  requires them
- use Tailnet ACLs to restrict access to the Pi

Recommended Tailnet exposure:

- allow trusted users to reach Flask on port `8080`
- allow trusted users to reach Grafana on port `3000`
- optionally allow Tailscale SSH on port `22`
- do not expose Mosquitto `1883` or InfluxDB `8086` over the Tailnet by default

## Service Hardening

The generated systemd units include conservative hardening defaults:

- `NoNewPrivileges=true`
- private temporary directories and device isolation
- read-only protected system paths
- restricted address families
- empty Linux capability bounding set

The Python services are designed to write logs to journald and avoid writing to
the application tree at runtime.
