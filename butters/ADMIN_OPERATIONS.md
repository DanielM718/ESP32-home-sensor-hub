# Butters Admin operations

Operational reference for the Admin/Tools page: how authentication and
elevation behave, what each Tools control state means, how to deploy without
drifting, and how to recover. Concise and command-oriented by intent.

Companion documents: `BETA1.md` (install, sessions, budgets),
`DESKTOP_COMPUTE.md` (SSH compute), `../docs/DESKTOP_AGENT_DESIGN.md`
(interactive agent protocol).

## Authentication: three independent layers

The Admin page is gated by three things that expire separately. Confusing them
is what used to make the page look broken, so they are named here explicitly.

| Layer | Source | Lifetime | Failure code |
| --- | --- | --- | --- |
| **Tailnet identity** | `Tailscale-User-Login` header, injected by `tailscale serve` and trusted only from the loopback peer | per request | `admin_identity_missing`, `admin_identity_denied`, `untrusted_proxy_peer` |
| **Browser session** | `butters_session` cookie, bound to the tailnet identity that created it | idle TTL, `web.session_ttl_seconds` (default 1800 s) | `invalid_session`, `session_identity_denied` |
| **Elevation** | Passkey assertion, held server-side, non-sliding | `authentication.elevation_seconds` | `elevation_required` |

Identity is checked on every request. The session carries the conversation and
CSRF token. Elevation authorizes privileged actions only.

**These are not interchangeable.** An expired elevation does not invalidate the
session, and the page must never say it did.

### What the page does when each expires

**Elevation expired, session valid** — the normal case. The page stays fully
usable. Read-only controls (desktop status, SSH test, application list, VM
list) keep working with no ceremony. Privileged controls stay **visible** and
clickable, marked `Authorization required`. Clicking one runs the passkey
ceremony and then performs the action. Nothing is hidden, because hiding
controls is indistinguishable from the controls not existing.

**Session expired** — a banner appears at the top of the page: *"Your browser
session expired. No action was retried."* with a **Reload and sign in** button.
Every other control is disabled immediately, so no button remains that would
only fail once clicked. Reloading restores the page.

**Identity denied** — the status pill reads `Denied`. Check
`web.admin_identities` / `BUTTERS_ADMIN_IDENTITIES` and that the request
arrives through `tailscale serve`, not directly.

### Re-elevating deliberately

Passkeys / Authentication panel → **Authenticate**. **Lock Now** drops
elevation immediately without touching the session. Elevation is
server-authoritative and never extended by activity.

## Tools control states

Every control renders in exactly one of four states. A control is never
displayed as usable when it is not, and never hidden merely because
authorization lapsed.

| State | Meaning | Rendering |
| --- | --- | --- |
| **Available** | Registered, backend capability present, authorized | Enabled |
| **Temporarily unavailable** | Real but not usable right now — desktop asleep, agent disconnected, no unlocked interactive session, stale heartbeat | Visible, disabled, reason in tooltip and inline |
| **Not configured** | No backing configuration — no registered project, application absent from the agent's `apps.toml`, no VM backend | Visible, disabled, labelled `Not configured` |
| **Authorization required** | Available but elevation expired | Visible, **enabled**; clicking requests the passkey ceremony |

### Wake Desktop

The Tools desktop section exposes **Wake Desktop**, which invokes the existing
registered `wake_desktop` skill. That skill reaches the root broker's
`desktop.wake` operation, which sends one Wake-on-LAN packet using the `mac`
and `broadcast` in `/etc/butters/action-broker.toml`. There is no second WOL
implementation, and the browser sends no parameters at all: the machine
identity comes from `desktop.machine` in `assistant.toml` (allow-listed to the
single configured desktop) and the MAC and broadcast from the broker's
root-owned config. A request carrying a `mac`, `host`, or `machine` parameter is
rejected with `invalid_action`.

Authorization is the skill's own — `elevated`, via the same freeze →
passkey → coordinator path a spoken "wake my computer" takes, with the same
audit record. Exposing it in Tools added a button, not a capability.

Control states:

| Condition | Rendering |
| --- | --- |
| Desktop unreachable, or reachability unknown | **Available** (or `Authorization required` if elevation lapsed) |
| Desktop already reachable | **Temporarily unavailable** — *"already reachable; no wake is needed"*. Clicking is unnecessary, not an error |
| `desktop.wake_enabled` false, or broker unprovisioned | **Not configured**, with the registry's reason |

Repeated requests are safe. A click while an action is running is dropped, and
if the desktop is already awake no packet is sent — that is reported rather
than treated as a failure.

Progress is reported only for stages Butters has **observed**, polled from
`/api/desktop/status` for up to 120 s:

```
wake requested → magic packet sent → waiting for desktop
              → network reachable → SSH available → agent connected
```

A cancelled ceremony or a failed action reports *"wake was not performed; no
packet was sent"* and claims no stage. If the desktop does not become reachable
within 120 s the panel says so and notes it may still be booting; the timeout
is an observation window, not a failure of the wake itself.

Shutdown, restart and sleep are **not** exposed in Tools. Their broker gates
are separately default-off.

### Desktop state axes

Desktop condition is reported as separate axes, never collapsed into one word:

- **Power** — `RESPONDING` / `UNKNOWN`
- **Network** — `REACHABLE` / `UNREACHABLE` (ICMP or SSH banner)
- **SSH / OS** — `AUTHENTICATED` / `SSH_RESPONDING` / `UNKNOWN`
- **Windows session** — `ACTIVE` / `LOCKED` / `NONE` / `MULTIPLE` / `UNKNOWN`
- **Desktop Agent** — `CONNECTED` / `OFFLINE`

### Desktop Agent states

`AgentHub.status()["state"]` is one of:

| State | Meaning |
| --- | --- |
| `not_configured` | No `/etc/butters/agents.toml`, or it failed validation |
| `disconnected` | No socket. Desktop powered off, asleep, or agent not running |
| `awaiting_heartbeat` | Socket attached, no authenticated heartbeat yet. **Not** treated as connected |
| `heartbeat_aging` | Heartbeat older than 30 s. GUI launch withheld |
| `heartbeat_stale` | Heartbeat older than 45 s. Agent treated as gone |
| `connected` | Fresh authenticated heartbeat |

Agent state is in-memory and ages out on its own. A desktop shut down normally
goes to `disconnected` when the socket closes, and the last observed Windows
session snapshot is dropped with it — Butters does not keep claiming an
interactive session it can no longer see. Restarting `butters-web` also clears
it; the agent reconnects by itself.

## Deployment

`/opt/butters` is an install target, **not** a git checkout. Editing files in
it, or copying individual files into it, is the one thing that reliably breaks
the Admin page: the frontend and backend then disagree about which actions
exist, and the symptom looks like an authorization failure.

### Deploy

```bash
cd ~/ESP32-home-sensor-hub
sudo ./butters/scripts/install-beta1 --start
./butters/scripts/verify-deployment
```

`install-beta1` stages a complete tree, builds the production virtualenv from
pinned requirements, verifies it imports `butters.web.app`, then swaps
atomically and restarts **every** daemon that executes the tree:
`butters-web`, `butters-agent-ingress`, and `butters-action-broker` if active.
Restarting only one leaves the deployment half applied.

It also stages `butters-agent/src/butters_agent` into `/opt/butters/src`. The
server imports `butters_agent.protocol`, the wire contract shared with the
Windows agent; it is not optional.

### Verify

```bash
./butters/scripts/verify-deployment    # exit 0 = consistent
```

Checks three things and names the fix for each:

1. The installed tree matches this checkout.
2. The installed tree has not been modified since it was installed.
3. No daemon has been running since before the tree last changed.

The Admin **Overview** panel shows the same information under `deployment`:
`commit`, `installed_at`, and `status`. A `status` of
`modified_since_install` means somebody hand-patched `/opt/butters` — reinstall
rather than debugging the symptom.

### Static assets

Asset URLs carry a digest of the running tree (`/assets/admin.js?v=…`) and are
served `Cache-Control: no-cache`. A restart after any deployment therefore
changes the URL, so no browser can keep executing a previous release's
JavaScript. Assets are read into memory at startup: **editing a file under
`static/` requires a service restart**, not just a page reload.

### Rollback

```bash
sudo systemctl stop butters-web.service butters-agent-ingress.service
sudo rm -rf /opt/butters && sudo mv /opt/butters.previous /opt/butters
sudo systemctl start butters-web.service butters-agent-ingress.service
./butters/scripts/verify-beta1
```

`/opt/butters.previous` holds exactly one generation. Never delete
`/var/lib/butters` — it holds passkeys, elevation state, usage and presets.

## Service health

Eight Butters-stack services, plus three platform services:

```bash
systemctl is-active butters-web butters-agent-ingress butters-action-broker \
  home-sensor-bridge home-sensor-dashboard home-sensor-export-worker \
  home-sensor-printer-observer mosquitto
systemctl is-active influxdb grafana-server tailscaled
systemctl --failed
```

`butters-action-broker` is correctly `static`: it is socket-activated, and
`butters-action-broker.socket` is the enabled unit. Everything else is
`enabled`.

```bash
curl -fsS http://127.0.0.1:8090/healthz     # Butters liveness
curl -sS  http://127.0.0.1:8090/readyz      # Butters readiness checks
curl -fsS http://127.0.0.1:8080/api/health  # environmental dashboard
curl -sS  http://127.0.0.1:8080/api/status  # per-service environmental view
curl -sS  http://127.0.0.1:8086/health      # InfluxDB
tailscale status && sudo tailscale serve status
```

`home-sensor-dashboard` and `home-sensor-printer-observer` order themselves
`After=influxdb.service`, then use a shared bounded readiness check against
InfluxDB's local `/health` endpoint and an authenticated Flux query before
starting. The check uses the existing backend environment credentials, retries
once per second for up to 30 seconds, and logs both retry progress and timeout
failures. `Restart=on-failure` remains enabled as a fallback.

## Logs

```bash
journalctl -u butters-web -f                     # web, actions, agent sockets
journalctl -u butters-agent-ingress -n 100       # TLS ingress (never logs headers)
journalctl -u butters-action-broker -n 100       # privileged broker
journalctl -u home-sensor-bridge -n 100          # MQTT to InfluxDB
```

Butters logs action metadata only — action name, success, exit code, duration.
Command output may contain project secrets and is never journalled. The agent
ingress deliberately logs nothing about handshakes.

Browser-side: Live Trace panel, or `/api/admin/traces`. Hidden model reasoning
is never requested or recorded.

## Common failures

| Symptom | Cause | Fix |
| --- | --- | --- |
| Tools shows an authorization error and no application rows | Frontend/backend deployment drift | `./butters/scripts/verify-deployment`, then `install-beta1 --start` |
| `Launch <app>` rows absent while the agent is connected | Application not in the agent's `apps.toml`, or its path does not resolve | Fix `apps.toml` on the desktop; rows come from `desktop.app.list`, not from Butters |
| Privileged button says `Authorization required` | Elevation expired | Click it, or Passkeys → Authenticate |
| `Wake Desktop` shows `Not configured` | `desktop.wake_enabled` false, or broker unprovisioned | Check `assistant.toml` and `/etc/butters/action-broker.toml` (`"desktop.wake" = true`) |
| Wake sent but nothing happens | Desktop WOL disabled in firmware, or wrong `broadcast` for the LAN | Verify `mac`/`broadcast` in the broker config; WOL cannot be diagnosed from Butters alone |
| Banner: session expired | Session idle TTL | Reload and sign in |
| Status pill `Denied` | Identity not allow-listed, or request bypassed `tailscale serve` | Check `admin_identities`; reach Butters via its Serve origin |
| Agent `disconnected` but the desktop is on | Agent not running, or ingress unreachable on the LAN | Check the Windows task; `journalctl -u butters-agent-ingress` |
| `ModuleNotFoundError: butters_agent` | Tree installed without the shared protocol | Reinstall with current `install-beta1` |
| Everything 503 / origin errors | `BUTTERS_ALLOWED_ORIGINS` unset | Set it in `/etc/butters/butters.conf`, restart |

## What must not be weakened

Passkey ceremonies, CSRF tokens, Origin and `Sec-Fetch-Site` checks, Tailscale
identity validation, and the separation between machine-agent authentication
(HMAC over the agent socket, which refuses any request carrying an `Origin`
header) and browser authentication. Desktop actions are an allowlist: the wire
carries registry *keys* matching `[a-z][a-z0-9_]{0,63}`, never paths,
arguments, or shell text. WOL wakes the machine and confers no control
capability of its own.
