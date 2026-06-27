# Tailscale Access

Remote access is through Tailscale only.

Official reference:

- <https://tailscale.com/kb/1031/install-linux>
- <https://tailscale.com/kb/1080/cli>
- <https://tailscale.com/kb/1085/auth-keys>
- <https://tailscale.com/kb/1337/policy-syntax>
- <https://tailscale.com/kb/1324/grants>
- <https://tailscale.com/docs/features/tailscale-ssh>
- <https://tailscale.com/docs/features/tailscale-funnel>

## Intended Access Model

Services listen on the Raspberry Pi for LAN use. Remote clients connect through
the Tailnet and use the Pi's Tailscale IP or MagicDNS name.

Do not configure router port forwarding for:

- Mosquitto
- InfluxDB
- Grafana
- Flask dashboard

## Generated Files

```text
server/scripts/install_tailscale.sh
server/scripts/verify_tailscale.sh
server/config/tailscale/tailnet-policy-example.hujson
```

## Raspberry Pi Setup

Interactive setup:

```bash
sudo /opt/home-sensor/server/scripts/install_tailscale.sh --hostname sensor-pi
```

The script prints the Tailscale login URL if browser authentication is needed.

Non-interactive setup with an auth key:

```bash
export TAILSCALE_AUTHKEY='<tskey-auth-...>'
sudo --preserve-env=TAILSCALE_AUTHKEY /opt/home-sensor/server/scripts/install_tailscale.sh \
  --hostname sensor-pi \
  --advertise-tags tag:home-sensor
unset TAILSCALE_AUTHKEY
```

Optional Tailscale SSH:

```bash
sudo /opt/home-sensor/server/scripts/install_tailscale.sh \
  --hostname sensor-pi \
  --enable-ssh
```

Use Tailscale SSH only if your Tailnet policy restricts access to trusted users.

Verify:

```bash
/opt/home-sensor/server/scripts/verify_tailscale.sh
```

The verifier checks that the `tailscale` command exists, `tailscaled` is known
to systemd, `tailscale status` succeeds, and the Pi has a Tailscale IPv4 address.

## Service URLs Over Tailscale

Use either MagicDNS or the Pi's Tailscale IP:

```text
http://sensor-pi:8080
http://<tailscale-ip>:8080
http://sensor-pi:3000
http://<tailscale-ip>:3000
```

Do not use Tailscale Funnel for this project. Funnel exposes services to the
public internet, which is outside the intended access model.

## Tailnet Policy

`server/config/tailscale/tailnet-policy-example.hujson` shows a restrictive
example using current Tailscale grants for network access. It allows admins to
reach:

- SSH: `22`
- Flask dashboard: `8080`
- Grafana: `3000`

It intentionally does not grant remote access to Mosquitto or InfluxDB.

The SSH policy uses check mode for admin access and allows `root` plus non-root
local users. Adjust the `users` list to match the Linux accounts you actually
use on the Pi.

If the Pi uses `tag:home-sensor`, create or update `tagOwners` in your Tailnet
policy before running `tailscale up --advertise-tags=tag:home-sensor`.

## Security Notes

- Do not commit Tailscale auth keys.
- Treat Tailscale auth keys as setup-only secrets. Do not keep them in
  `server/backend/.env` after the Pi has joined the Tailnet.
- Prefer ephemeral or pre-approved auth keys for automated provisioning.
- Do not enable subnet routing or exit-node behavior for this backend.
- Do not enable Funnel or public sharing.
- Keep Grafana and Flask available through LAN and Tailnet only.
- Keep Mosquitto and InfluxDB bound to LAN/local use unless you intentionally
  add tighter firewall and Tailnet ACL controls.
