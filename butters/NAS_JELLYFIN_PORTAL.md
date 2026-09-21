# NAS control and the Jellyfin access portal

This document covers the integrated NAS controls in Admin → Tools and the
separate passkey-authenticated Jellyfin access portal. It is written to be read
before the first deployment, because several things here are deliberately
shipped disabled.

## What runs where

Everything runs on **Butters**, inside the existing `butters-web.service`. The
portal is not a second daemon and it is never hosted on the NAS, for the
obvious reason: when the NAS is powered off its Jellyfin, its web UI and its
Tailscale node are all gone, and something that is still up has to send the
magic packet.

```
Partner → Tailscale HTTPS → Butters (/portal) → passkey → Wake NAS
        → WOL from Butters → poll status → Jellyfin ready → redirect
```

## Tailscale Serve

**No Serve change is required.** The existing mapping is already:

```
https://sensor-pi.tail9644cc.ts.net (tailnet only)
|-- / proxy http://127.0.0.1:8090
```

Because it mounts `/`, the new `/portal` page and `/api/portal/*` endpoints are
served by that route as it stands. This is the desired end state, not a
shortcut:

* The portal listens only on the existing loopback socket. It is never bound to
  a LAN or public TCP listener of its own.
* There is exactly **one** WebAuthn relying-party origin for control,
  `https://sensor-pi.tail9644cc.ts.net`. LAN users and remote users use the same
  origin; only the final Jellyfin destination differs.
* The Desktop Agent ingress keeps its own separate listener and is untouched by
  this route.
* Admin is not exposed by this mapping any more than it already was:
  `/admin` and `/api/admin/*` remain gated by `AuthPolicy.admin_identity`, which
  requires a loopback proxy peer and an allow-listed `Tailscale-User-Login`.

If the mapping is ever narrowed from `/` to specific paths, it must include
`/portal`, `/api/portal/`, `/api/session` and `/assets/`.

## The trust boundary for LAN vs Tailscale

`butters/src/butters/web/locality.py` is the whole decision, and its module
docstring is the normative statement. In short:

1. The request must arrive on loopback from a trusted peer (Tailscale Serve, or
   a reviewed local reverse proxy). Otherwise the answer is `UNKNOWN`.
2. Inside that boundary only an operator-configured `portal.locality_header`
   set by that local ingress is read.
3. Failing that, Butters may read **its own** tailscaled (`portal.
   tailscale_status_command`) to find the calling identity's current direct
   endpoint, and compare it against `portal.lan_networks`.

Client-supplied values never participate: `X-Forwarded-For`, `X-Real-IP`, a
client-set Tailscale identity header, `?redirect=`, `?host=`, `?ip=`,
`?local=true`, `?remote=true` are all ignored for this decision.

`UNKNOWN` resolves to the **Tailscale** destination. The failure mode is "a home
user takes the overlay path", never an open redirect: the returned URL is always
one of `nas.jellyfin_lan_url` or `nas.jellyfin_tailscale_url`.

## Configuration to complete before enabling

`/opt/butters/config/assistant.toml`:

All of these were observed on 2026-09-14 and need only confirming, except the
DHCP reservation, which must be checked on the router:

```toml
[nas]
# 192.168.1.240 is where the NAS answers today (ARP for 00:e2:69:7d:40:cd).
# CONFIRM this is a DHCP *reservation*; a lease that moves breaks LAN probing.
lan_host = "192.168.1.240"
# TrueNAS middleware; tcp/443 confirmed open.
api_url = "https://192.168.1.240"
tailscale_host = "truenas-scale.tail9644cc.ts.net"
# Both confirmed answering /health with 200.
jellyfin_lan_url = "http://192.168.1.240:8096"
jellyfin_tailscale_url = "http://truenas-scale.tail9644cc.ts.net:8096"

[portal]
enabled = true
lan_networks = ["192.168.1.0/24"]
# Optional; leave empty unless a reviewed local ingress sets it.
locality_header = ""
# Optional; enables endpoint-based classification. The butters service user can
# already read tailscaled, and peers on the home LAN do report a CurAddr of the
# form 192.168.1.x:41641, so this classifier has real data to work with.
tailscale_status_command = ["/usr/bin/tailscale", "status", "--json"]

[actions.nas]
enabled = true
configured = true

# Leave BOTH false until the destructive test is separately approved.
[actions.nas_shutdown]
enabled = false
configured = false
```

`/etc/butters/action-broker.toml` — **no edit is required to deploy.** The
parser treats the shutdown transport fields and the `nas.shutdown` gate as
optional, and an absent gate is disabled, so the configuration already deployed
keeps every gate it has. Edit this file only when enabling shutdown:

```toml
[nas]
mac = "00:e2:69:7d:40:cd"
broadcast = "192.168.1.255"
# Both empty leaves the shutdown handler unregistered entirely.
api_url = ""
api_key = ""

[operations]
"nas.wake" = true
"nas.shutdown" = false
```

## NAS shutdown: transport and gates

The transport is the TrueNAS middleware's own `POST /api/v2.0/system/shutdown`,
with a fixed URL, a fixed method, a literal `{}` body, and a bearer credential
read by the **root** broker from a `root:root 0600` file the service user cannot
read. It is deliberately not SSH: there is no shell, no argv, and no remote
command string on this path, so there is nothing for a caller to influence even
if the socket request carried a field — which it does not.

Four independent gates stand in front of it, and three ship closed:

| Gate | Default | Where |
| --- | --- | --- |
| `actions.nas_shutdown.enabled/configured` | `false` | `assistant.toml` |
| broker `"nas.shutdown"` | `false` | `action-broker.toml` |
| fixed transport configured (`api_url` + `api_key`) | unset | `action-broker.toml` |
| FRESH passkey bound to the exact frozen action + explicit confirmation | n/a | runtime |

`shutdown_nas` is also planner-hidden: it is absent from
`CONVERSATIONAL_PLANNER_ACTIONS`, so no conversational path can reach it.

## Enrollment and revocation

Portal registration is **never** open to an unauthenticated visitor.

1. An administrator creates an invitation in Admin → Tools → Jellyfin access
   portal for one named tailnet identity (`identity:person@example.com`).
2. The invitation is single-use, expires (`portal.invite_ttl_seconds`), and is
   bound to that identity — nobody else can redeem it.
3. The person opens `/portal` as that identity, redeems it, and registers their
   own passkey. They hold `jellyfin_access` and nothing else.

## Granting `nas_power`

`nas_power` is never carried by an invitation, so nobody is enrolled straight
into power authority. It is added afterwards, to an identity that already holds
a passkey, in the same Admin panel: tick the roles that identity should hold and
press **Update roles**. The control sends the *complete* role set, because the
store replaces rather than merges — the checkboxes are pre-filled from what the
identity holds today so the full set is what an administrator is editing.

Each change requires the administrator's own FRESH passkey assertion, bound to
the identity being changed (`purpose="portal_role_update"`), plus an explicit
confirmation. An assertion collected for one person cannot be replayed against
another. Granting the role powers nothing off: it confers the authority to
*begin* the fixed shutdown ceremony, which still demands that person's own FRESH
assertion bound to the frozen plan's digest.

Two things this path deliberately will not do. It will not create an identity —
a name nobody has enrolled is refused rather than granted a role it has no
passkey to use. And it will not reinstate a revoked identity, so revocation is
not quietly undone by a role edit. Note the consequence honestly: revocation
leaves credentials intact while `begin_portal_registration` excludes the
identity's live credentials, so a revoked person cannot be re-invited onto the
*same* authenticator either. Reinstating one is not a supported operation today.

An added role takes effect at the holder's next portal sign-in, since a session
records what was granted at sign-in and is intersected with the current grant on
every request. A removed role takes effect on their next request.

`jellyfin_access` never implies administrator. Administrator authorization is
decided by `AuthPolicy` from the tailnet identity and the configured
administrator list, and consults nothing the portal writes. An administrator who
also wants portal access must be granted it explicitly.

Revoking an identity in the same panel ends the role, drops every live portal
session on the next request, and cancels any unredeemed invitation. The person's
credential is revoked separately through the normal credential path.

## Rollout order

1. Deploy the code. Confirm Admin → Tools still renders the full Desktop section
   and that the Desktop Agent reconnects.
2. Configure `[nas]`, enable `actions.nas`, and set the broker `"nas.wake"` gate.
   Verify status observations and, if appropriate, one Wake NAS.
3. Enable `[portal]`, verify that `/portal` requires authentication, that
   registration is closed without an invitation, and that no Admin control
   appears on it.
4. Enroll the partner **only with them present**.
5. NAS shutdown stays off until its destructive test is separately approved.

## Note on the Tailscale Jellyfin URL

`jellyfin_tailscale_url` is plain `http://` on port 8096. The tailnet itself is
encrypted by WireGuard, so nothing travels in clear over the network, but the
browser still treats the page as a non-secure context. If that matters, run
`tailscale serve` on the NAS to front Jellyfin with HTTPS and change this one
value; nothing else in the design depends on the scheme.
