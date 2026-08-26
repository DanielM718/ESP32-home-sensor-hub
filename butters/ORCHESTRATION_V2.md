# Butters Cloud Orchestration and Skill Framework v2

## Execution model

Normal chat follows `PLAN → POLICY → EXECUTE → OBSERVE → REASON`.
Deterministic Tier 0 routing remains first and never invokes a model. Complex
print/environment requests are deterministically planned into a bounded local
analytical skill; only the reduced structured evidence is offered to the cloud
reasoner for interpretation.

Every executable capability is a `SkillSpec`. V2 metadata includes a stable
name/version, strict input and output schemas, `READ_ONLY`, `ANALYTICAL`, or
`ACTION` classification, audience, timeout, output-byte ceiling, side-effect
description, explicit-intent/confirmation requirements, and safe metadata for
admin traces. There is no shell, generic SSH, arbitrary script, arbitrary host,
database-query, printer-control, or service-control skill.

`ACTION` execution requires an `ActionAuthorization` produced from the current
user turn and naming the exact skill. The model cannot create this object.
Direct deterministic routes cover only named desktop, host, NAS, and configured
environment actions. Advice or troubleshooting language does not authorize an
action. The cloud catalog categorically omits every ACTION skill.

## Authentication and frozen actions

Intent, authentication, policy, and execution are separate boundaries. Skill
metadata declares one of `NONE`, `ELEVATED`, or `FRESH`, plus an explicit
`local_console_allowed` exception. Unknown actions and ACTION skills lacking an
authentication declaration cannot be registered. `LOCAL_CONSOLE` is an
ephemeral runtime context, not a browser credential or a passkey level.

Browser step-up uses the maintained `webauthn` package. RP ID and origin are
fixed trusted configuration (`sensor-pi.tail9644cc.ts.net` and its exact HTTPS
origin); request forwarding headers never establish WebAuthn trust. Challenges
are 32 random bytes, single-use, short-lived, ceremony/session/identity bound,
and bounded in SQLite. User verification is required. The server stores only
credential ID, public verification key, stable random user ID, label, timing,
revocation, and authenticator metadata. Elevation lasts ten minutes by default,
does not slide, is server authoritative, and is bound to the browser session and
tailnet peer identity.

Before authentication, exact typed action arguments are canonicalized into an
immutable, expiring, one-use pending plan with a random nonce and SHA-256 digest.
The browser cannot modify it. An ELEVATED assertion resumes the exact frozen
plan. A FRESH assertion is bound to that plan digest and cannot authorize a
different action or create general elevation. Plans contain at most four steps;
the deterministic router supports the explicit computer-plus-environment case,
while inferred extra steps must instead be proposed to the user.

The first passkey requires a root-only local CLI bootstrap token. The database
stores only its hash. Later additions and revocations require fresh assertions.
Local recovery requires root plus an explicit CLI confirmation, removes all
server-side public credential records, clears ceremonies/elevation, and restores
the first-passkey bootstrap precondition. Neither bootstrap nor recovery exists
as a skill or remote API.

The physical listener calls `note_physical_wake()` only on a detector wake event.
That creates a short-lived, physical-path-bound context for one voice
interaction. Allow-listed ELEVATED actions are frozen and require an exact
deterministic confirmation (`yes`, `yeah`, `confirm`, `do it`; or the explicit
negative set) inside that same local voice session, without another wake phrase.
Stale, unrelated, browser, cloud, and other-session text cannot confirm it. A
new wake cancels the old pending confirmation. FRESH actions always direct the
user to a passkey device.

## Cloud and tool bounds

The general Responses API loop exposes only relevant registered skills and at
most one tool call per model turn. Current defaults are four tool rounds, eight
total tool calls, five cloud requests, 90 seconds total wall time, 45 seconds per
provider request (reduced to the remaining request deadline), 1,200 output
tokens, one retry, 8 KiB cloud tool-result serialization, 64 KiB provider
request context, and configured request/daily/monthly cost ceilings. Duplicate
tool calls, unknown tools, malformed or extra arguments, policy denial, skill
timeout, oversized results, and provider failures stop cleanly. V2 skill
implementations run behind a hard local timeout with a cancellation event.

Cloud context is minimized to the current request, at most four recent bounded
messages (2,000 characters each), relevant tool schemas, and compact local
observations. Credentials, script paths, MAC addresses, SSH users/keys, IP
addresses, and database query text are not skill arguments or tool results.

## Privileged broker and jobs

Unprivileged Butters never executes user scripts. Mutations cross a versioned,
bounded JSON request over `/run/butters-action-broker/broker.sock`. The request
contains exactly protocol version, random request ID, and one enumerated
operation. The broker validates message size, encoding/schema/types, operation,
replay, peer UID (`SO_PEERCRED`), and a second root-owned per-operation enable
gate. Responses are also strictly bounded. There is no request argument capable
of selecting argv, shell text, host, MAC, path, service, entity, topic, or
payload. Fixed subprocesses use explicit argv with `shell=False` semantics.

Each accepted plan creates a session/identity-owned job. Jobs expose bounded
state, stage, progress, result/failure, timing, and cancellation. Cancellation
stops polling and later stages; it cannot undo an already accepted irreversible
operation. Timed environmental overrides remain in a persistent table and a
cancel releases the fixed OFF operation. Interrupted jobs are failed by their
own execution surface; recovery of an active override must issue its fixed OFF
operation and fails closed if the broker is unavailable.

Sensitive outcomes are retained in a bounded SQLite audit. Identity and session
references are hashed, arguments are canonical/sanitized, and no conversation,
credential material, challenge, bootstrap token, or infrastructure secret is
stored.

## Desktop boundary

The retired direct-link/display-task inventory was:

- `/home/dmejiame/scripts/wake-desktop.sh`: former fixed WOL helper.
- `/home/dmejiame/scripts/ssh_begin_remote`: fixed SSH identity, desktop target,
  and the now-removed `Enter Remote Mode` scheduled task.
- `/home/dmejiame/scripts/ssh_restore_local`: the now-removed inverse display
  task.

Those two display helpers and all `169.254.x.x` direct-link values are obsolete.
The current fixed network identities are:

- WOL: `wakeonlan -i 192.168.1.255 34:5A:60:D7:4C:2C`
- normal-LAN SSH: `ssh -i ~/.ssh/windows_remote_mode Daniel@192.168.1.209`
- physical monitor power: Home Assistant only, targeting exactly
  `switch.desktop_gigabyte` and `switch.desktop_oled`

The workflow owns sequencing, network and TCP/SSH readiness, separate polling
deadlines, cancellation, structured stages, error handling, and tracing. Ping
is never treated as Windows readiness. WOL and monitor power remain independent.
The composed Parsec/headless workflow wakes when necessary, waits for normal-LAN
readiness, and then turns off both physical monitors through one fixed Home
Assistant service call. It never changes Windows displays or the Parsec Virtual
Display Adapter.

The broker config fixes host, user, key, MAC, broadcast, and loopback Home
Assistant URL, is root-owned mode `0600`, and separately enables each operation.
The HA token is read only from the existing root-controlled
`/etc/home-sensor/printer.env`; it is never stored in source or returned in a
result. The monitor operations accept no entity argument. Optional generic
fixed SSH actions retain the pinned-host/no-forwarding/no-PTY boundary and must
use the existing `windows_remote_mode` key material; do not regenerate it.

Destructive desktop skills exist behind `FRESH` but ship disabled. Lock is
ELEVATED and also disabled. They remain unavailable until the dedicated
credential/forced-command boundary has been provisioned and reviewed.

## Host, NAS, and environment capabilities

Read-only host skills return bounded uptime/load/memory/root-disk/temperature,
the fixed Butters service state/restart count, selected dependency health, and
broker provisioning state. Host restart/reboot/shutdown are individually
enumerated FRESH actions and ship disabled. Restart is scheduled two seconds
later through a fixed transient unit so the request can be acknowledged first.

NAS status/wake use one configured target only. No target is currently
configured, so both state and wake remain explicitly unprovisioned. The broker
can send the fixed NAS WOL operation only when root config and both enable gates
are present.

Heater, dehumidifier, and ventilation expose typed status and `on`/`off` actions
with an optional configured maximum duration. Entity/topic/service identifiers
never enter the skill or broker protocol. The current deployment has no actuator
adapter, so all three ship unavailable. Enabling one later requires a fixed
broker implementation, both local gates, maximum runtime, and any required
safety sensor/freshness configuration. Missing required safety configuration or
stale/missing sensor data fails closed. Timed ON waits in a cancellable job and
issues the fixed OFF operation on expiry or cancellation; restart recovery must
release persisted overrides before further control.

## Repository deployment artifacts

`systemd/butters-action-broker.socket` creates a `0600` Unix socket owned by the
Butters service identity. The root broker service is socket-activated and
hardened. `config/action-broker.example.toml` ships every operation disabled.
`scripts/install-action-broker` installs repository artifacts only when a local
administrator later runs it; it does not generate credentials or configure
targets. It was not run during implementation.

Provisioning sequence after independent review:

1. Install the reviewed application and Python dependency set.
2. Create `/etc/butters/action-broker` root-owned mode `0700`.
3. If optional fixed SSH actions are needed, provision the existing
   `windows_remote_mode` private key at the configured root-readable path and a
   verified pinned `known_hosts`; do not replace or regenerate the key.
4. Create `/etc/butters/action-broker.toml` root-owned mode `0600`; replace
   placeholders and enable only reviewed operations.
5. Match the unprivileged `[broker]` and `[actions]` capability gates without
   copying infrastructure identifiers into that file.
6. Ensure `/etc/home-sensor/printer.env` supplies `HOME_ASSISTANT_TOKEN`, then
   enable only `desktop.monitors_on` / `desktop.monitors_off` after verifying the
   two compiled entity IDs.
7. Install/enable the socket artifacts, verify peer rejection and fixed
   operations with deployment-specific acceptance tests, then enable actions.
8. Run `butters-passkey bootstrap` locally as root and enroll the first passkey
   through the exact configured HTTPS origin.

## Printer and environmental evidence

Printer state uses the existing dashboard Bambu/X2D observer. Structured current
and recent sessions retain only available identifiers, job name, start/end,
duration, progress, material, observed temperatures, state, and provenance.
No printer actuation is registered.

Sensor history uses only the dashboard's typed `/api/readings` parameters. It
allows registered entities/metrics, 1h/24h/7d/30d lookbacks or an ISO interval
within 30 days, fixed bucket choices, stable sorting, and at most 256 returned
points. No Influx token, bucket credential, Flux, InfluxQL, SQL, or arbitrary
query text crosses the skill boundary.

Local analytics calculate count, min, max, mean, median, start/end, absolute and
percentage delta, least-squares slope per hour, median-deviation spikes, guarded
Pearson correlation, and baseline/print window comparisons. Correlation requires
at least five paired samples and is always labeled non-causal. Print analysis
uses either an explicitly requested pre-print duration or an immediately
preceding equal-duration window (bounded to seven days).

Structured analytical results label `observed`, `calculated`, `inferred`, and
`unknown` evidence. The cloud model performs interpretation only; it is told
that observed association cannot establish printer causation.

## Extension contract

A new sensitive capability requires all of: a typed semantic SkillSpec and
strict schema; ACTION classification; explicit authentication requirement and
local-console decision; deny-by-default local policy; fixed adapter and an
enumerated broker operation when privilege is needed; local safety/timeout and
cancellation constraints; sanitized trace/audit representations; capability
availability gates; and policy/schema/failure tests. Adding a capability never
changes WebAuthn ceremony logic and never adds a generic execution mechanism.

## Cloud configuration

The API key environment variable is exactly `OPENAI_API_KEY`. Production loads
secrets from `/etc/butters/butters.env` via the systemd `EnvironmentFile`; the
key must not be placed in the repository or `/etc/butters/butters.conf`.
Text reasoning requires both `[cloud].enabled = true` and
`[cloud].allow_paid_calls = true`. Models come from the reviewed
`luna_model`/`terra_model`/`sol_model` configuration; orchestration does not add
or hardcode another model. With cloud disabled or the key absent, Tier 0 and
local skills remain functional and analytical chat reports that cloud
interpretation was not performed.
