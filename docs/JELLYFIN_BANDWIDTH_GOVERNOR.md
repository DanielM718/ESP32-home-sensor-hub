# Jellyfin bandwidth governor

Status: implementation and the first production read-only hardware validation
are complete for measurement, classification, and dry-run calculation. The
branch is merge-ready but remains unmerged pending explicit approval.
This phase contains no Jellyfin mutation path and rejects
`policy_mode = "enforce"` at configuration load and again when constructing the
governor.

The implementation extends the production NAS Agent rather than introducing a
second NAS daemon. It preserves the existing outbound authenticated WSS link,
closed symbolic protocol, read-only TrueNAS session, fixed shutdown operation,
and portal authorization boundaries.

## Scope and non-goals

This phase measures three deliberately distinct quantities:

1. physical NAS interface traffic;
2. traffic carried by the NAS Tailscale client; and
3. Jellyfin's reported bitrate and playback state for active sessions.

Physical interface traffic is not treated as remote traffic. Tailscale traffic
is not treated as Jellyfin traffic. Jellyfin-reported bitrate is not represented
as a NIC counter. Reconciliation keeps the three observations separate.

There is no `tc`, router QoS, qBittorrent change, Tailscale configuration
change, session termination, forced transcode, Jellyfin user-policy update, or
generic HTTP/middleware proxy in this branch. The Windows desktop is outside
the workstream.

## Placement

The NAS Agent owns source collection because it is already the hardened,
NAS-local, outbound-only component with the required TrueNAS and Jellyfin
reachability. `NetworkTelemetry` samples network sources, `JellyfinBackend`
projects sessions, `DryRunGovernor` derives policy, and `BandwidthService`
maintains the latest bounded sample. `NasAgentHub` performs a second exact
projection before Butters or a browser can consume a result.

The portal receives a third, privacy-reduced projection. It excludes physical
interface names, endpoint addresses, path counters, stable session IDs,
client/device IDs, playback positions, local sessions, and unknown-session item
labels. Remote user/item labels are bounded and shown only to an authenticated
`jellyfin_access` portal session. The portal now keeps the monitor visible and
offers an explicit **Open Jellyfin** button instead of immediately redirecting
as soon as Jellyfin becomes ready.

Portal authorization remains explicit. Administrator identity does not imply a
portal role: the normalized administrator identity was granted only the
existing `jellyfin_access` role through `AuthStateStore`. It was not granted
`nas_power`, no unrelated identity was changed, and the existing WebAuthn
authentication requirement remains in force.

The existing InfluxDB/Grafana implementation belongs to the separate home
sensor server and has no authenticated NAS Agent ingestion path. This phase
does not give the NAS Agent InfluxDB credentials or create cross-daemon writes.
The stable metric set is documented below for a later, narrow exporter once
live measurements are accepted.

## Versions and research basis

- TrueNAS SCALE production baseline: 25.10.7. The supported v25.10
  `reporting.netdata_get_data` method requires `REPORTING_READ` and accepts a
  fixed `interface` graph identifier. [TrueNAS reporting API](https://api.truenas.com/v25.10/api_methods_reporting.netdata_get_data.html)
- The upstream TrueNAS interface graph identifies its vertical unit as
  `Kilobits/s`, returns `time`, `received`, and `sent` legend columns, and
  normalizes sent values positive. [TrueNAS InterfacePlugin source](https://github.com/truenas/middleware/blob/master/src/middlewared/middlewared/plugins/reporting/netdata/graphs.py#L65-L92)
- Live Jellyfin reported 10.11.11 through `/System/Info/Public`. The authenticated
  fixed `GET /Sessions?activeWithinSeconds=90` endpoint returns session DTOs;
  Jellyfin passes the authenticated user/API-key context to its session manager.
  [Jellyfin SessionController](https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Controllers/SessionController.cs#L40-L64)
- Tailscale client metrics are supported from 1.78 and expose monotonic inbound
  and outbound byte counters with `direct_ipv4`, `direct_ipv6`, `derp`, and
  peer-relay path labels. A client exposes its own metrics at
  `http://100.100.100.100/metrics`. [Tailscale client metrics](https://tailscale.com/docs/reference/tailscale-client-metrics)
- The live NAS Tailscale Custom App is `v1.102.3` (chart `1.4.14`). Its local
  metrics endpoint is reachable from the existing bridged, unprivileged NAS
  Agent container without host networking or added mounts.

## First hardware findings

The production physical interface is `enp6s0`, the sole link-up physical NIC
carrying `192.168.1.240`. Its TrueNAS interface graph exists, uses the upstream
`Kilobits/s` unit, and produces plausible physical-only bursts. Tailscale's
monotonic counters were visible for `direct_ipv4`, `direct_ipv6`, `derp`, and
both peer-relay paths; the peer-relay counters remained zero during validation.

The NAS-local Jellyfin credential successfully reads two-session responses from
Jellyfin 10.11.11. The first real response was 1,191,365 bytes, proving that the
original 256 KiB input bound was too small for Jellyfin's nested session DTOs.
The fixed 4 MiB bound accepts the measured response while raw inspection and
projected output remain capped at 64 and 32 records respectively.

Real LAN and Tailnet sessions matched the configured LAN and Tailscale CIDRs
from Jellyfin's server-recorded `RemoteEndPoint`; raw addresses were not exposed
to the portal. A LAN transcode produced 41.576--50.356 Mbps physical bursts with
0--0.001 Mbps Tailscale TX and remained outside the remote pool. A remote
transcode was classified from its real `100.64.0.0/10` endpoint and charged as
one remote stream with a stable 24 Mbps dry-run target. In the final
counter-bounded run, Jellyfin reported 5.577 Mbps. Tailscale transmitted
95,617,984 bytes over 102.312 seconds, or 7.476 Mbps: a +1.899 Mbps (+34.1% of
the Jellyfin figure) reconciliation delta. Five-second smoothed observations
ranged from 4.704 to 11.896 Mbps. This discrepancy is plausible for segmented
fetches, audio/container traffic, protocol overhead, and the difference between
Jellyfin's nominal report and wire bytes, but it is too variable to attribute
instantaneously to another service.

The remote runs also established an important limitation: Jellyfin's reported
bitrate is nominal, while clients fetch media in large bursts and then play from
buffer. Five-second Tailscale TX ranged from effectively zero to 14.632 Mbps
while Jellyfin continued reporting 7.122 Mbps and playback position advanced.
Therefore instantaneous `T - J` is a useful signed reconciliation diagnostic,
but is not a truthful measurement of non-Jellyfin traffic while a remote or
unknown Jellyfin session exists. Enforcement remains blocked on a separately
reviewed longer-window/attribution design.

A three-minute idle run measured mean physical TX/RX of 0.003/0.024 Mbps,
mean Tailscale TX/RX of 0.001/0.001 Mbps, and mean attributable other-remote
traffic of 0.001 Mbps. Quality was `good`, the chargeable count was zero, and no
target was fabricated. After the remote stream stopped, the target cleared
immediately and the source returned to `good` quality. Live start/pause/stop
transitions also verified the 15-second stability window, 1 Mbps change
threshold, and 30-second cooldown without oscillation.

The validated immutable NAS Agent image is
`ghcr.io/danielm718/butters-nas-agent@sha256:6de88fe83138b42978641a0b4d84b777574b3e50eb76c3fbf9534fb2e22a0667`,
built from source revision `ff666f9839bbcde74fda04d291fdddfdabb949a3`.
The hardened UID/GID, read-only root, dropped capabilities,
`no-new-privileges`, no-inbound-port/no-Docker-socket boundary, and bounded
resources/logs were preserved.

## Measurement sources

### Physical NAS interface

The source is the fixed TrueNAS JSON-RPC method
`reporting.netdata_get_data`, graph `interface`, identifier taken only from
operator configuration. The agent requests recent, unaggregated rows, indexes
the last complete row by its returned legend, and converts native decimal
kilobits/second to megabits/second by dividing by 1000.

This source was chosen because it is a supported read API already reachable
through the pinned, serialized, read-only TrueNAS session. It avoids mounting
host `/sys`, adding host networking, entering a host namespace, or granting a
new privileged capability. It is a rate source rather than a monotonic counter;
the response's adjacent timestamps describe its native sample interval.

The configured interface is source-owned. No protocol or browser input can
select an interface.

### Remote Tailscale traffic

The source is Tailscale's local client Prometheus endpoint at the one accepted
URL, `http://100.100.100.100/metrics`. Only these two documented counter
families are parsed:

- `tailscaled_outbound_bytes_total`
- `tailscaled_inbound_bytes_total`

All other metric families and labels are ignored. Allowed path labels are
bounded to the documented direct IPv4/IPv6, DERP, and peer-relay IPv4/IPv6
values; an unexpected path is grouped as `unknown`. Total counters feed the
rate sampler. Per-path cumulative counters are available in the agent/hub
operation for diagnosis but are removed from the portal response.

This source is preferable to physical-interface inference because it counts
bytes actually carried by the Tailscale client and distinguishes direct/relay
paths. The endpoint is not currently reachable remotely on NAS port 5252; that
mode would require enabling the web client and changing tailnet ACLs, neither
of which was authorized. Live staging proved the local endpoint reachable from
the hardened NAS Agent container. No host networking, Docker socket, host
`/proc` or `/sys` mount, or additional capability was added.

### Jellyfin sessions

The source is the fixed authenticated endpoint
`GET /Sessions?activeWithinSeconds=90`. The token is a NAS-local read secret,
never placed in Butters, a browser response, the action protocol, or logs. The
response is capped at 4 MiB, at most 64 raw records are inspected, and at most
32 projected sessions are returned.

Only sessions with a `NowPlayingItem`, a valid bounded session ID, and a
`PlayState` are projected. Fields are limited to bounded user/item/client/device
labels, active/paused state, play method, playback position, classification,
and bitrate. The active-within filter limits stale entries but does not prove a
client is still transferring bytes; reconciliation with the Tailscale counter
is therefore essential.

Bitrate preference is:

1. `TranscodingInfo.Bitrate` for a transcode (`transcode_reported`);
2. `NowPlayingItem.Bitrate` for direct play/direct stream
   (`source_reported`); or
3. unavailable.

These are reported/source rates, not measured per-session network counters.
They may differ from wire rate because of bursty segment delivery, container
overhead, audio/subtitle behavior, buffering, or imperfect media metadata.

## Trust boundary and classification

Classification uses Jellyfin's server-recorded `SessionInfo.RemoteEndPoint`.
It does not inspect browser headers, user-facing locality labels, or caller
input. The endpoint string is parsed as an IPv4/IPv6 address with an optional
port or IPv6 zone. The address is compared with exact operator-owned CIDRs:

- local: configured LAN ranges, initially `192.168.1.0/24`;
- remote: configured Tailscale ranges, initially `100.64.0.0/10` and
  `fd7a:115c:a1e0::/48`; and
- unknown: missing, malformed, outside all trusted ranges, or ambiguous.

Local wins only when the server-recorded address is inside a configured local
range. Unknown never receives a local exemption: an active, unpaused unknown
session is chargeable in dry-run allocation and degrades measurement quality.

The current direct Tailnet URL has no known reverse proxy in front of Jellyfin,
but the live session endpoint must confirm that Jellyfin really records the
Tailscale client address. If a container proxy/NAT causes every remote session
to appear as a LAN or bridge address, classification is not trustworthy and
enforcement remains blocked. Forwarded headers are not an acceptable repair
unless the intermediary and Jellyfin trusted-proxy behavior are separately
designed and verified.

## Closed NAS Agent operations

All operations have exact `{}` parameters. Unknown or extra fields fail before
backend dispatch.

### `nas.network.status`

Returns sample timestamp; physical NAS TX/RX Mbps; Tailscale TX/RX Mbps;
bounded path counters; fixed source names; source reasons; sample window; EWMA
description; and `good`, `partial`, or `unavailable` quality. Missing telemetry
is `null`, never zero.

### `nas.jellyfin.sessions`

Returns availability/reason and at most 32 bounded projected sessions: stable
ID, user, active/paused, local/remote/unknown, direct play/direct stream/
transcode/unknown, reported Mbps and its source, position, client/device, and
item label. It never returns `RemoteEndPoint`, a URL, a token, or an arbitrary
Jellyfin response.

### `nas.bandwidth.status`

Returns configured capacity/pool/reserve; known remote and unknown counts;
reported remote Jellyfin Mbps; total and attributable-other Tailscale Mbps;
reconciliation
delta; physical headroom; candidate and hysteresis-held per-stream targets;
mode/quality/reason; informational `would_enforce`; above-target stable IDs for
server-side diagnosis; Direct Play above-target IDs; and the bounded sessions.
The portal projection strips all stable IDs and non-remote session details.

The action schema version changes from 1 to 2; protocol framing remains version
1. The four production power/status actions are otherwise unchanged, and the
only mutating action remains `nas.system.shutdown`.

## Sampling and smoothing

Default Tailscale sampling interval is 5 seconds. A rate is calculated from
monotonic counters and a monotonic process clock:

```text
rate_mbps = (current_bytes - previous_bytes) * 8
            / elapsed_seconds / 1,000,000
```

The raw rate is smoothed with an exponentially weighted moving average:

```text
smoothed = alpha * raw + (1 - alpha) * previous_smoothed
alpha = 0.35
```

The raw interval is retained internally and both raw and smoothed values are
unit-tested. Policy consumes the smoothed Tailscale rate. A five-second cadence
and alpha 0.35 retain burst sensitivity without making every segment burst a
policy transition.

The first sample, missing/non-finite counter, non-positive elapsed time,
counter decrease/reset, or elapsed interval over 30 seconds produces an
unavailable rate and an explicit reason. A reset establishes a new baseline.
Agent restart naturally produces `first_sample`. Absent data never becomes
zero. One collection exception is redacted, logged as unavailable, and does not
kill the background sampling task. Cached derived state expires after two
sampling intervals.

TrueNAS physical traffic is already a supported rate. It is not incorrectly
differenced as a counter or folded into the Tailscale EWMA.

## Accounting model

Definitions:

```text
C = configured effective uplink capacity
S = configured safe Jellyfin pool
R = configured reserve
J = sum of reported bitrates for known remote active/unpaused sessions
T = smoothed Tailscale outbound rate
O = T, only when no remote/unknown Jellyfin session is present
D = T - J, signed reconciliation delta
H = max(0, C - T), only when T is known
```

When no remote or unknown Jellyfin session exists, all measured Tailscale TX is
truthfully non-Jellyfin (`O = T`). While any such session exists, `O` is
unavailable: short-window subtraction cannot distinguish non-Jellyfin traffic
from Jellyfin segment bursts. `D` remains a signed diagnostic comparison when
all known active remote sessions have reported bitrate and there is no unknown
session. A negative `D` means the reported nominal rate exceeds the current
smoothed wire rate, which is expected while a client plays from buffer. Any
active or paused remote/unknown session makes derived quality partial even when
the underlying network source remains good.

Initial configurable values are:

```text
C = 30 Mbps
S = 24 Mbps
R =  6 Mbps
minimum per stream = 3 Mbps
maximum per stream = 24 Mbps
```

## Dry-run policy

Chargeable sessions are active, unpaused sessions classified remote or unknown.
Paused sessions and trusted local sessions do not receive a full allocation.
Unknown is conservative: it counts against allocation but is not added to the
known-remote reported-bitrate sum.

When other traffic is independently attributable because no remote/unknown
Jellyfin session exists:

```text
dynamic_pool = min(S, max(0, C - R - O))
fair_share = dynamic_pool / chargeable_count
candidate = min(maximum, max(minimum, fair_share))
```

During active remote playback, `O` is unknown, the static safe pool `S` is used,
and quality is partial. The result says why. This dry-run phase deliberately
does not shrink the pool using a value hardware proved would misattribute media
bursts as other traffic. If `fair_share` is below the floor, the floor
is reported with `configured_floor_exceeds_available_fair_share`; it is not
presented as a feasible guarantee. A single stream is still capped by the
configured maximum.

With the initial policy and no other traffic, stable candidates are 24, 12, 8,
and 6 Mbps for one through four chargeable streams.

`would_enforce` is only an informational dry-run flag. It becomes true when at
least one chargeable session's known reported rate exceeds the held target by
the 1 Mbps minimum-change threshold. No call follows that flag.

Mode semantics are: `off` keeps measurement visible but suppresses policy
targets; `observe` calculates targets without setting `would_enforce`; and
`dry_run` calculates targets and the informational flag. `enforce` is rejected.

### Hysteresis

- A new stream count must remain stable for 15 seconds.
- A target movement below 1 Mbps is held.
- Accepted target changes have a 30-second cooldown.
- Stream starts and nonzero count changes pass through stability/cooldown;
  zero chargeable streams clear the displayed target immediately.
- All durations and thresholds are configurable within bounded ranges.

The governor reports both the current mathematical candidate and the held
target, along with the hold reason. Restart clears hysteresis state safely.

## Direct Play and future enforcement

A Direct Play stream whose source bitrate exceeds its safe share cannot be made
safe by merely displaying a lower number. A future enforcer must cause a fresh
playback negotiation that selects direct stream/transcode output compatible
with the assigned maximum. This can interrupt playback and depends on client
behavior.

Jellyfin's playback negotiation accepts `PlaybackInfoDto.MaxStreamingBitrate`.
The server also applies the user's `RemoteClientBitrateLimit`, falling back to
the server's remote-client limit, when its own network manager considers the
request remote; it takes the minimum during media-source selection. [PlaybackInfoDto](https://typescript-sdk.jellyfin.org/interfaces/generated-client.PlaybackInfoDto.html),
[MediaInfoHelper.GetMaxBitrate](https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Helpers/MediaInfoHelper.cs)

Mechanisms investigated for a later phase:

- server remote-client bitrate configuration: global and negotiation-time;
- per-user `RemoteClientBitrateLimit`: persistent, coarse, and affects future
  negotiation for that user, not a session-specific shared pool;
- client-supplied playback-info `MaxStreamingBitrate`: useful at negotiation
  but controlled by the client unless mediated by a trusted server component;
- the session `SetMaxStreamingBitrate` general command: client capability and
  behavior vary, so it is not a universal server-side guarantee; and
- stopping/restarting or asking a client to renegotiate: potentially effective
  but user-visible and explicitly out of scope.

No supported evidence was found that changing a user/server policy reliably
downshifts an already active direct or HLS stream in place. The enforcement
design must therefore distinguish new playback from existing playback and must
not claim seamless active-stream reallocation.

For a new B stream while A is using 22 Mbps, the future sequence is: observe B;
wait for count stability; compute roughly 12 Mbps each; apply a negotiation-time
limit to B; and, only under separately approved enforcement policy, request A
to renegotiate or interrupt/restart A if its client cannot honor an in-session
command. Until that behavior is validated per supported client, dry run reports
both sessions and flags A without changing either.

When a stream ends, the remaining stream's larger candidate waits for stability
and cooldown. A future enforcement design must obtain trustworthy
longer-window or source-attributed non-Jellyfin traffic before allowing it to
shrink the pool during playback. Unknown sessions never receive a local
exemption.

## Failure modes

- TrueNAS reporting unavailable/malformed: physical rate is null; remote
  measurement may still be usable; quality is partial.
- Tailscale endpoint unreachable, unsupported version, first sample, reset, or
  stale interval: remote rate and derived other/headroom values are null.
- Jellyfin token missing/rejected or payload malformed: sessions unavailable;
  policy quality unavailable.
- Session endpoint missing/obscured: classification unknown and chargeable;
  quality partial.
- Reported bitrate absent: Jellyfin aggregate and reconciliation stay null.
- Any remote/unknown Jellyfin session is present: other-remote attribution is
  null and quality is partial; the signed delta remains diagnostic where valid.
- Reported Jellyfin exceeds current remote wire rate: signed delta is negative,
  which is expected during buffered playback and never becomes a negative
  service rate.
- More sessions than the floor can support: the result is explicitly
  infeasible; dry run does not pretend the configured floor can fit.
- Agent/hub schema mismatch: the authenticated connection fails closed because
  the exact action set and schema version differ.

## Portal and future time-series metrics

The portal displays remote usage/capacity, safe pool, explicitly labeled
Jellyfin reported rate, attributable other remote traffic, physical headroom,
remote/unknown counts, held dry-run target, quality, mode, and compact remote
stream cards. `null` renders as Unavailable, never `0 Mbps`. Local sessions do
not enter the browser response.

Once a reviewed exporter path exists, use fixed measurement/field names for:

- total NAS TX/RX Mbps;
- Tailscale TX/RX Mbps;
- remote Jellyfin reported Mbps;
- other remote estimated Mbps;
- reconciliation delta Mbps;
- active remote and unknown stream counts;
- safe budget, target, utilization, and headroom.

Do not tag points with title, session ID, user, device, or arbitrary client
strings. At most use fixed low-cardinality tags such as host identity, policy
mode, quality, and source.

## Production validation result

The read-only stage completed with `network.enabled=true`,
`session_monitoring_enabled=true`, and `policy_mode="dry_run"`. The fixed
Jellyfin session read uses a separately revocable NAS-local API key file owned
by UID/GID 568 with mode `0400`. Jellyfin API keys are globally powerful rather
than endpoint-scoped; containment is therefore provided by the agent's fixed
GET, exact projection, filesystem secret, and hardened runtime. The token never
entered a command argument, log, protocol response, Butters, or the browser.

Idle, LAN-transcode, remote-transcode, pause/start, and stop states passed. The
LAN session raised physical TX but not Tailscale TX and never entered the remote
pool. The remote session raised Tailscale TX and entered the pool. A second
simultaneous remote client and a real above-target Direct Play session were not
naturally available and were intentionally not fabricated; those remain
pre-enforcement hardware tests.

The NAS Agent Custom App and the changed Butters web service were the only
restarted production components. Jellyfin, Tailscale, power services, and the
Windows desktop were not restarted or contacted. Existing TrueNAS status and
shutdown identities were unchanged. No shutdown or wake action was performed.
The user visually accepted the portal after the explicit role assignment, and
no enforcement call occurred.

## Rollback

Rollback is configuration/image based:

1. restore the production immutable NAS Agent image digest and its prior config;
2. restore the matching schema-1 Butters deployment if schema 2 was staged;
3. restart only the NAS Agent and changed Butters services; and
4. verify the existing heartbeat, status, portal, wake, and fixed shutdown path.

No Jellyfin policy, Tailscale setting, TrueNAS identity, router QoS, or traffic
control state needs restoration because this phase changes none of them.

## Enforcement gate

An enforcement phase requires new explicit authorization after live
measurement/classification acceptance. It must introduce a separate immutable
mutation protocol, authorization/RBAC, audit and rollback design, supported
client behavior matrix, new-vs-existing stream semantics, and real Direct Play
renegotiation tests. It must default off and cannot be activated by a browser.
