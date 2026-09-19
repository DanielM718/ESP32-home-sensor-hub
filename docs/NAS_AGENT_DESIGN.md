# NAS Agent design

Status: implementation and tests only; production remains disabled. The design
targets TrueNAS SCALE 25.10 and the single machine identity `nas-primary`.

## Decisions

Wake remains the existing fixed Butters-side `nas.wake` operation (MAC
`00:e2:69:7d:40:cd`, broadcast `192.168.1.255`, LAN `192.168.1.240`). An agent
cannot wake its powered-off host. Shutdown moves to a fixed local NAS Agent
operation and the current broker-side shutdown path is not used by the new
administrator or portal flow.

The NAS implementation is deliberately separate from the proven Desktop Agent:

- reused semantics: TLS, SPKI pinning before machine-token disclosure, a
  separate token digest, canonical HMAC-SHA256 frames, timestamps, a
  connection nonce, replay/idempotency bounds, ACK/result framing, coordinator
  policy, and immutable pending plans;
- separate code and configuration: `NasAgentHub`, `/nas-agent/v1/session`,
  `nas-primary`, the `nas.*` namespace, NAS credential files, status schemas,
  state, container, and TrueNAS backend;
- not generalized yet: transport base classes, backend registries, generic
  machine actions, arbitrary middleware calls, arbitrary HTTP calls, and shell
  execution. A second proven use case should precede shared abstractions.

## Trust boundaries

1. The browser supplies only a session, explicit confirmation, and WebAuthn
   ceremony response. It never supplies an action, host, URL, method, command,
   mode, delay, or argument.
2. Butters web applies origin/CSRF/session checks and either administrator policy
   or the exact portal role.
3. `ActionCoordinator` freezes the deterministic zero-argument action, binds
   FRESH authentication to its digest, audits it, and executes the registered
   implementation.
4. `NasAgentHub` accepts one configured identity and exactly one advertised
   schema set. It projects results into small allowlisted structures.
5. The network is untrusted. TLS provides confidentiality; the NAS client pins
   the Butters SPKI before sending its token. Every post-hello frame is HMAC
   signed, timestamp checked, and bound to the random connection ID.
6. The unprivileged NAS Agent container holds only fixed local endpoint config
   and file-mounted credentials. It has no inbound port, shell interface,
   Docker socket, or host filesystem access.
7. The TrueNAS JSON-RPC boundary exposes only source-owned method constants.
   Its locally observed certificate SPKI is verified before an API key is
   transmitted. Read and dormant shutdown credentials are independent.
8. Jellyfin health and session reads use fixed agent-owned endpoints. Session
   monitoring is optional and projects only bounded policy fields; the NAS-local
   token, raw endpoint address, and raw response never cross the agent boundary.
9. Tailscale remains an observation source, not a control channel. Optional
   bandwidth telemetry reads only the documented local client counters and
   accepts no caller-selected endpoint.

## Identities, credentials, and transport

The only accepted NAS machine identity is `nas-primary`; Desktop credentials
cannot authenticate it. Butters stores a root-managed configuration containing
the SHA-256 digest of the 32-byte machine token and a reference to a separate
32-byte HMAC key. The agent receives the token and HMAC key in a mode `0600`
file. These credentials are independently revocable and rotatable.

The outbound WebSocket is `wss://.../nas-agent/v1/session`. After SPKI
verification, the agent sends a hello containing its exact identity, protocol
and schema versions, and exact action list. Butters returns a signed welcome
with a random connection ID. All subsequent frames include that ID and a
90-second freshness window. Heartbeat sequence numbers are strictly
increasing. One request may be pending, timeouts are at most 30 seconds, late
terminal frames are accepted only from a bounded per-connection tombstone set,
and a newer authenticated connection supersedes the old socket and its work.

Request IDs and idempotency keys are UUIDv4 values. The agent keeps a bounded,
expiring terminal-result cache keyed by both. Reusing either key for a different
action fingerprint fails closed; an exact duplicate returns the cached result.
The coordinator derives a deterministic shutdown idempotency key from its job
ID.

## Closed protocol

Every operation has an exact zero-parameter schema:

| Operation | Class | Result payload |
|---|---|---|
| `nas.agent.status` | read-only | agent/protocol/schema version, bounded hostname, process uptime, connection count/uptime, last heartbeat sequence |
| `nas.system.status` | read-only | local TrueNAS reachability, bounded hostname/version, optional uptime, `unknown\|online\|shutting_down` |
| `nas.jellyfin.status` | read-only | local reachability, readiness, optional bounded version, optional HTTP status |
| `nas.network.status` | read-only | physical and Tailscale rates, fixed sources, quality/window, bounded path counters |
| `nas.jellyfin.sessions` | read-only | bounded playback fields and trusted `local\|remote\|unknown` classification |
| `nas.bandwidth.status` | read-only | configured budget, derived accounting, dry-run target/reason, bounded sessions |
| `nas.system.shutdown` | destructive, dormant | exactly `accepted=true`, `state=scheduled`, `method=system.shutdown` |

Unknown actions, missing/extra keys, unknown result fields, non-finite values,
oversized frames, wrong identities, wrong targets, and arbitrary shutdown input
are rejected. No raw middleware or Jellyfin body crosses the agent boundary.
The three bandwidth operations and their action schema version 2 are described
in `docs/JELLYFIN_BANDWIDTH_GOVERNOR.md`. `enforce` is not a valid mode in this
lineage.

## State and power truth

Agent connectivity and system power are independent state machines.

Agent state is one of `not_configured`, `disconnected`,
`awaiting_heartbeat`, `connected`, `heartbeat_aging`, or `heartbeat_stale`.
A heartbeat is authoritative only for its current authenticated connection.
Reconnect and disconnect clear its TrueNAS and Jellyfin payloads; stale data is
never presented as a fresh observation.

System state is `unknown`, `online`, or `shutting_down`. A normal socket loss can
mean shutdown, restart, network loss, or agent failure. Therefore:

- agent connected/aging plus a local successful TrueNAS observation => online;
- an accepted fixed shutdown or local `SHUTTING_DOWN` observation => shutting
  down;
- agent disconnected **and** a fresh LAN probe unreachable **and** a fresh
  TrueNAS/API probe unreachable => off;
- everything else => unknown.

Corroborated OFF evidence takes precedence over the earlier shutdown marker and
completes the lifecycle. Agent disconnect alone never means OFF.

The presentation lifecycle is observation driven:

```text
OFF -> WAKE_SENT -> NAS_REACHABLE -> AGENT_CONNECTED
    -> TAILSCALE_REACHABLE -> JELLYFIN_READY -> READY

READY -> SHUTDOWN_AUTH_REQUIRED -> SHUTDOWN_ACCEPTED -> SHUTTING_DOWN
      -> AGENT_DISCONNECTED -> NAS_UNREACHABLE -> OFF
```

The current renderer collapses `JELLYFIN_READY` into `READY` only when the
agent's local Jellyfin probe is ready; no progress timer invents an intermediate
state.

## TrueNAS SCALE 25.10 integration findings

These assumptions were checked against the current official 25.10 API and
application documentation. Before hardware staging, record the appliance's
exact 25.10 patch release and re-check its matching API documentation.

- TrueNAS SCALE's supported current API is JSON-RPC 2.0 over WebSocket at
  `/api/current`; REST is deprecated from 25.04 and is scheduled for removal in
  26. [JSON-RPC guide](https://api.truenas.com/v25.10/jsonrpc.html)
- For 25.10, the agent authenticates with `auth.login_ex` and the
  `API_KEY_PLAIN` mechanism over SPKI-pinned WSS. This accommodates the
  appliance default self-signed certificate, which can be valid only for
  `localhost`, without using host networking or sending a credential before
  server identity is verified. The older
  `auth.login_with_api_key` exists but is deprecated; SCRAM becomes mandatory
  only in 26+. [login_ex](https://api.truenas.com/v25.10/api_methods_auth.login_ex.html)
- The exact shutdown operation is the job method `system.shutdown`, with
  parameters `[reason, {"delay": null}]`; the 25.10 documentation describes
  its immediate result as `null`. Hardware acceptance on 25.10.7 returned a
  non-null success acknowledgement before powering off. The agent therefore
  treats any well-formed JSON-RPC success envelope as accepted, discards its
  result value, and reports only that TrueNAS accepted/scheduled the job.
  [system.shutdown](https://api.truenas.com/v25.10/api_methods_system.shutdown.html),
  [jobs](https://api.truenas.com/v25.10/jobs.html)
- `system.shutdown` requires `FULL_ADMIN`. TrueNAS documents that role as
  unrestricted and not scopeable. There is no suitably narrow built-in
  shutdown-only privilege in 25.10. This is the principal residual risk.
  [25.10 RBAC](https://api.truenas.com/v25.10/rbac.html)
- The bounded status backend uses `system.state`, `system.version_short`, and
  `system.info`. The first two are available with narrower system-read roles;
  `system.info` requires `READONLY_ADMIN`. A dedicated read-only service user is
  still materially narrower than the separate dormant `FULL_ADMIN` shutdown
  user/key. [system.state](https://api.truenas.com/v25.10/api_methods_system.state.html),
  [system.info](https://api.truenas.com/v25.10/api_methods_system.info.html)
- Read-only status calls share one serialized, authenticated middleware
  WebSocket. Heartbeat collection and explicit status actions therefore cannot
  compete for a reader or correlate the same JSON-RPC response. Request IDs
  increase for the life of that socket. Any timeout, malformed frame, or
  server-side closure invalidates the socket; the next attempt performs a new
  TLS/SPKI check and authentication. A stale established socket may be retried
  once, but only within the original end-to-end backend deadline.
- The TrueNAS backend timeout is one operation budget covering lock wait,
  connect, TLS/SPKI, authentication, and all three fixed status methods. It is
  not restarted for each phase, so the 10-second agent-local bound remains
  below the 30-second signed request/hub deadline.
- TrueNAS 25.10 supports Custom App installation from YAML/Compose. A normal
  bridged container can reach the NAS's fixed LAN/MagicDNS WSS endpoint; host
  networking and a middleware socket mount are unnecessary and would enlarge
  the boundary. [Custom App YAML](https://www.truenas.com/docs/scale/25.10/scaleuireference/apps/installcustomappscreens/),
  [container networking](https://www.truenas.com/docs/scale/25.10/scaletutorials/network/containernasbridge/)

### Local credential security

The read key and disabled shutdown key belong to different dedicated TrueNAS
users. They exist only as separate NAS-mounted secret files, never in Compose,
the image, Butters, a browser, a response, or a log. Files must be owned by the
container UID and mode `0600`; dataset/ACL access is restricted to the app.
Keys should be expiring where operationally feasible, independently revocable,
and rotated after staging. The shutdown key is neither provisioned nor mounted
until a separately approved destructive test, and the process refuses to load
it while `shutdown_enabled=false`.

The local configuration names the two identities separately. Read sessions
authenticate only as `butters_nas_status`; an enabled shutdown backend requires
the distinct fixed `butters_nas_power` username and refuses configuration that
reuses the read identity. Separate key files without separate login identities
are not sufficient isolation.

The eventual destructive stage has an unavoidable privilege asymmetry:
TrueNAS 25.10 requires `FULL_ADMIN` for `system.shutdown`, so the API credential
itself has broader TrueNAS authority than the NAS Agent protocol exposes. That
risk is contained, not eliminated. The future credential must be a distinct
key for a distinct shutdown identity, stored in its own NAS-local secret file,
never sent to Butters or a browser, never loaded or mounted while either
shutdown gate is false, and independently revocable/rotatable. Even when that
credential is present, the agent exposes it only through the fixed,
zero-argument `nas.system.shutdown`; no middleware method, URL, delay, reboot
mode, shell, or argv crosses the agent protocol. No such credential exists in
the read-only baseline.

## Shutdown gates and disconnect semantics

`nas.system.shutdown` is registered as destructive, explicit-intent,
confirmation-required, FRESH, and default disabled. Its caller schema is `{}`;
the backend owns the reason, null delay, method, endpoint, username, and mode.
`jellyfin_access` never satisfies shutdown policy. A separate `nas_power` role
may prepare only this immutable plan, and portal authentication is bound to the
plan digest. `nas_power` grants neither administrator status nor Jellyfin
access. Default portal invites still grant only `jellyfin_access`.

After the ACK, connection loss is expected during a real shutdown. A signed
accepted result is success. A loss after ACK but before that result is reported
as `shutdown_result_indeterminate`, not as a refusal or proof of failure. Final
OFF still requires the independent reachability evidence above.

## Package and Custom App

`nas-agent/` is independently installable and contains strict config loading,
the closed protocol, outbound client, engine, local/Jellyfin/TrueNAS backends,
entry point, container build, and tests. It imports no Butters web package.

`nas-agent/truenas-custom-app.compose.yaml` is dormant and intentionally has an
invalid image-digest placeholder. Before staging, build/review/publish the
image and replace it with its immutable digest. The container runs as UID/GID
568, read-only, all Linux capabilities dropped, `no-new-privileges`, no inbound
ports, no Docker socket, no host shell/filesystem, small tmpfs mounts, a
heartbeat health check, `unless-stopped` restart, CPU/memory limits, and bounded
JSON logs. Config and secrets are read-only mounts. The separately reviewed
shutdown secret and CLI option are absent.

## Deferred work and known limits

- Do not enable shutdown until the FULL_ADMIN residual risk is explicitly
  accepted, a dedicated expiring credential is created, and the mock staging
  run passes.
- Do not add pool, SMART, disk, application, qBittorrent, arbitrary HTTP, or
  generic middleware operations to this protocol. Add each future operation as
  a reviewed symbolic schema and fixed backend method.
- The Custom App YAML needs a real immutable image digest and concrete dataset
  paths. No installer or `/opt/butters/DEPLOYMENT` change is part of this branch.
- Production frontend controls remain dormant; the server-side lifecycle and
  fixed RBAC/auth flow are ready for a later explicitly approved UI slice.
