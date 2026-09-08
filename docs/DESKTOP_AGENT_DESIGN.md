# Butters Windows Desktop Agent + Orchestration Architecture

**Status:** design specification, not implemented
**Date:** 2026-09-07
**Scope:** the next layer after the current Codex tracks (desktop SSH foundation, Action API, Tools panel, ESP32 firmware)
**Non-scope:** this document does not modify ESP32 firmware, ESP-NOW/MQTT formats, node provisioning, the current SSH implementation, the current Action API, or the current Tools panel. Where it needs something from those areas, it states an **interface assumption** instead.

---

## 1. Executive architecture recommendation

Butters gains one new component: a **user-session Windows agent** that owns everything requiring an interactive desktop, and nothing else. It is a peer transport behind the existing Action API, not a second control plane.

```
        voice / wake word            manual web control panel
                |                              |
                v                              v
        local intent router  ------->  Butters Action API  <---- automation / schedules
                |  (miss)              (registry + policy + audit)
                v                              |
        cloud planner (optional)  --plan-->    |
                                               |
        +--------------------+-----------------+------------------+
        |                    |                 |                  |
        v                    v                 v                  v
   SSH compute        Desktop Agent      root broker         IoT / MQTT
   (headless)         (interactive)      (privileged fixed)  NAS / servers
```

Four principles:

1. **One action registry.** Voice, UI, automation and workflows all call the same registered actions with the same validation, permissions and audit. Transport choice (SSH vs agent vs broker vs MQTT) is an implementation detail of an action, never something a caller selects.
2. **The agent is a capability, not a dependency.** Every headless action must keep working when the agent is offline (Windows logged out, agent crashed, user session locked-out). Actions that genuinely need the interactive session fail with a precise, non-misleading error.
3. **Three desktop transports coexist, by purpose.**
   - **SSH** (existing, Codex track 1): compilation, git, tests, scripts, file ops, headless tools.
   - **Desktop Agent** (this document): GUI launches, session state, app/VM state, rich telemetry, workflow participation.
   - **Root broker** (existing `butters/src/butters/actions/broker.py`): privileged fixed operations that must work *without* the agent — WOL, lock, sleep, restart, shutdown, monitor power, Parsec service ensure. This is deliberately the out-of-band path: it is how Butters recovers a desktop whose agent cannot run yet.
4. **Deterministic execution.** The LLM proposes structured plans; it never executes. Plans are frozen, validated and authorized by the existing `ActionCoordinator` before anything runs.

### How this slots into what exists today

`butters/src/butters/actions/compute.py` already declares the seam:

```python
class DesktopAgent(Protocol):
    def status(self) -> dict[str, object]: ...
    def run_registered(self, application: str) -> dict[str, object]: ...

class UnavailableDesktopAgent:  # current default
    ...
```

This design fills in that Protocol with a real implementation (`AgentSessionClient`) and widens it (§4.6). `DesktopActions.catalog()` already publishes `desktop_agent: self.agent.status()`, so the Tools panel gains agent state with no contract change. Existing `BrokerOperation` members (`desktop.wake`, `desktop.parsec_ensure`, `desktop.lock`, …) remain the agent-independent fallback and become the first steps of the streaming workflow.

---

## 2. Communication / transport recommendation

### Recommendation: a single persistent **agent-initiated WebSocket over TLS** to Butters, carrying framed JSON messages.

- Agent (Windows, user session) dials **out** to `wss://butters.<lan-or-tailnet>:8443/agent/v1/session`.
- One long-lived, multiplexed connection carries: command requests (Butters→agent), acknowledgements and results (agent→Butters), heartbeats, and unsolicited state events.
- Butters holds at most one live session per `agent_id`; a new authenticated connection supersedes the old one (last-writer-wins, old socket closed with `superseded`).

### Why outbound WebSocket

| Concern | Outbound WSS (recommended) | HTTPS REST server on Windows | MQTT |
|---|---|---|---|
| Windows firewall | No inbound rule, no listener; outbound 8443 is allowed by default | Needs inbound rule + listener surviving Windows profile changes | No inbound rule (also outbound) |
| NAT / IP change | Agent redials; Butters needs no address for the desktop | Butters must track desktop IP; breaks on DHCP change | Fine |
| Tailscale later | Change one config value (`butters_url` host) — the desktop needs no inbound tailnet ACL | Requires desktop on tailnet *and* inbound ACLs | Fine |
| Offline detection | Immediate: socket close, plus heartbeat deadline | Polling only; "unreachable" conflated with "asleep" | Needs LWT + keepalive tuning |
| Command ack + result correlation | Native request/response over one ordered stream | Two-way callbacks needed for long actions | Needs correlation topics + manual ordering |
| Latency | Warm connection, no TLS handshake per command (~1–3 ms LAN) | Handshake per call unless pooled | Broker hop |
| Extra infrastructure | Reuses the existing Butters web service | New Windows listener | A broker in the command path for a single peer |
| Streaming state events | Native push | Requires polling or webhooks back to Butters | Native push |

**Why not MQTT**, despite MQTT already existing for IoT: MQTT is excellent for many small fire-and-forget devices, and it should keep owning ESP32 telemetry (untouched, Codex track 2). It is a poor fit for a *single* interactive peer that needs request/response with per-command acks, ordered multi-step workflow interaction, cancellation, and a crisp "is the session alive right now" answer. Putting desktop commands through the broker also makes the broker a hard dependency of desktop control and couples the desktop's blast radius to the IoT bus. Keep the buses separate.

**Why not inbound HTTPS to Windows**: it inverts the trust and reachability problem for no gain, requires a firewall rule and a listener bound in the user session (which disappears at logout — precisely when you most need to know the state), and gives no push channel.

**Cost of the recommendation, stated honestly:** Butters becomes the server, so the *agent* cannot be reached at all when Butters is down (acceptable — Butters is the only client), and Butters must run a TLS endpoint with connection-state management. That endpoint already exists in `butters/src/butters/web/`.

### Fallback and layering

- If the socket is down, `desktop.*` GUI actions fail fast with `agent_unavailable` (retryable) — they do **not** silently degrade to SSH GUI launches. Launching GUI apps through sshd is explicitly out of scope: sshd's service session is not the interactive session, and `Session 0` isolation means the window either never appears or appears invisibly.
- Privileged/agent-independent operations continue through the existing root broker over its Unix socket, unchanged.
- Optional later: a tiny UDP/HTTP "presence beacon" is *not* needed — heartbeats cover it.

### Message framing

Text frames, one JSON object per frame, `type` discriminated:

| `type` | Direction | Purpose |
|---|---|---|
| `hello` | agent→Butters | identity, versions, capabilities (first frame) |
| `welcome` | Butters→agent | accepted, server time, negotiated protocol version, config epoch |
| `request` | Butters→agent | an action invocation |
| `ack` | agent→Butters | request accepted/rejected, before work begins |
| `result` | agent→Butters | terminal outcome of a request |
| `event` | agent→Butters | unsolicited state change (app exited, session locked, VM stopped) |
| `heartbeat` | agent→Butters | periodic liveness + light metrics |
| `cancel` | Butters→agent | cancel an in-flight `request_id` |
| `error` | either | protocol-level failure |

Ordering: `ack` always precedes `result` for a given `request_id`. Butters treats a missing `ack` within `ack_timeout` (default 3 s) as transport failure, and a missing `result` within the action's `timeout` as `timeout` (retryable only if the action is idempotent).

---

## 3. Security and authentication design

### Recommendation: pinned-TLS server identity + per-device token + signed request envelope, with request-ID replay protection.

Concretely, three independent layers:

**(a) Butters authenticates itself to the agent — anti-Butters-impersonation.**
Butters serves the agent endpoint with a long-lived self-signed certificate generated on the Pi. The agent pins the **SPKI SHA-256 hash** (not the whole cert) in its config and rejects any other peer, ignoring the Windows trust store entirely. A LAN device that spoofs DNS/ARP for `butters.local` cannot present the pinned key. Pinning SPKI (not the cert) lets the cert be re-issued without touching the agent.

*Rejected:* a home certificate authority. It adds a CA key to protect, an issuance workflow and expiry management for one server and one client. SPKI pinning gives the same guarantee for this topology with a single 44-character config value.

**(b) The agent authenticates itself to Butters — anti-desktop-impersonation.**
A 256-bit random `agent_token`, provisioned once, presented in the `hello` frame. Butters compares with `hmac.compare_digest` against the stored hash. Storage:
- **Windows:** DPAPI-protected blob under `%LOCALAPPDATA%\Butters\agent.cred`, encrypted to the *user* scope (`CryptProtectData` with `CRYPTPROTECT_LOCAL_MACHINE` unset), so it is unreadable by other user accounts. Windows Credential Manager (`CredRead` with `CRED_TYPE_GENERIC`) is an equally acceptable store; DPAPI is simpler to back up and to script.
- **Butters:** `/etc/butters/agent-tokens.toml`, `root:butters 0640`, containing only `argon2`/`sha256` hashes plus metadata — never plaintext, never in the repo, never in `assistant.toml`.

**(c) Every request carries a signed, non-replayable envelope.**
Butters HMAC-SHA256s the canonical form `request_id|action|target|issued_at|sha256(canonical_json(parameters))` with a per-device `command_key` (separate from the auth token) and puts it in `sig`. The agent verifies before *parsing intent*. This means:
- A replayed frame is rejected by the agent's own `request_id` cache even if TLS is somehow terminated by something else.
- `issued_at` outside ±90 s (configurable `clock_skew_seconds`) is rejected → bounded replay window even against a cold cache.
- Result frames are signed the same way in reverse, so Butters cannot be fed a forged success.

Signing may be **deferred to Phase 3** — pinned TLS plus a token is already meaningfully secure for a trusted LAN — but `sig` should be in the wire format from day one (`sig: null` accepted while `require_signatures = false`), so enabling it later is a config flip, not a protocol break.

*Rejected:* mutual TLS. It is the textbook answer and it does work, but it means a client cert, a private key in the Windows store, an expiry you will be surprised by in 12 months, and renewal tooling — for exactly one client. The token+HMAC pair achieves device authentication and message integrity with a rotation story that is "write a new value into two files."

*Rejected:* SSH-derived identity for the agent. Reusing the SSH key/`known_hosts` trust (already pinned, per `home-sensor-deployment-topology`) is tempting, but the agent lives in the interactive session while sshd's key material is machine/service-scoped; binding them would either leak the automation key into user space or make the agent depend on sshd being up. Keep the two trust paths independent — that independence is what lets Butters distinguish "SSH up, agent down."

### Non-negotiable authorization rules

- **No arbitrary shell.** The agent exposes no action that takes a command string, path, argument list, or executable name. Nothing on the wire is ever passed to `CreateProcess` as a path.
- **Registered names only.** `parameters.app` selects a key in the operator-owned application registry; `parameters.vm` selects a key in the VM registry. An unknown key is `unknown_app` / `unknown_vm`, never a fallback.
- **Strict validation.** Action name must be in the agent's compiled-in action table. Parameters are validated against a per-action schema: exact key set (no extras, following the existing `set(parameters) != expected` pattern in `compute.py`), typed, and every identifier matched against `^[a-z][a-z0-9_]{0,63}$`.
- **No config over the wire.** The agent never accepts registry/config updates from Butters. Registries are operator-owned files on the Windows box, changed by a human, at `%ProgramData%\Butters\` with an ACL denying write to non-administrators (mirroring the "operator-owned allowlist" rule already used for `desktop-compute.toml`).
- **Replay protection.** `request_id` is a UUIDv4 minted by Butters. The agent keeps an LRU of the last 512 `request_id`s with their terminal results for `idempotency_window` (default 300 s). A duplicate returns the **cached result** with `duplicate: true` and does not re-execute.
- **Least privilege.** The agent runs as the normal (non-elevated) logged-in user. Anything needing elevation is either (i) refused, or (ii) delegated to the existing root broker / a pre-registered Scheduled Task with `RunLevel=Highest` created once by an administrator — never a runtime UAC prompt (§22).

---

## 4. Desktop Agent architecture

### 4.1 Process model

A single Python process, started by a **Scheduled Task** with:

- Trigger: *At log on* of the specific user, **plus** *On workstation unlock*, plus a repeating *every 5 minutes* safety trigger with `StartWhenAvailable`.
- `RunLevel = Limited` (non-elevated), `LogonType = InteractiveToken` — this is what puts it in the interactive session with access to the desktop and to per-user process context.
- Settings: `MultipleInstances = IgnoreNew` (the 5-minute trigger becomes a free supervisor: if the process died, the next tick restarts it; if it is alive, the tick is ignored), `RestartCount = 3`, `RestartInterval = PT1M`, `ExecutionTimeLimit = PT0S` (unbounded), `DisallowStartIfOnBatteries = false`.

*Rejected:* a Windows **Service**. Services run in session 0. They can enumerate processes but cannot launch a visible GUI app into the user session without `CreateProcessAsUser` + token duplication + `WTSQueryUserToken` gymnastics — which is exactly the complexity this agent exists to avoid. Session isolation is the whole reason the agent exists; do not fight it.

*Rejected:* `shell:startup` shortcut. Works, but gives no restart supervision, no unlock trigger, and no logging of start failures.

### 4.2 Internal structure

```
main.py            process lifecycle, signal/console-ctrl handling, supervisor loop
config.py          load + validate config and registries; fail closed on invalid
transport/         ws_client.py (connect/redial/backoff), framing.py, envelope.py (sign/verify)
security/          credentials.py (DPAPI), pinning.py (SPKI verify), replay.py (LRU), validate.py
actions/           dispatch.py (name -> handler + schema), plus one module per domain
applications/      registry.py (parse/validate), launcher.py, detect.py
vm/                base.py (VmBackend protocol), hyperv.py, vmware.py, virtualbox.py, registry.py
state/             session.py (session/lock/idle), machine.py (uptime, OS build), snapshot.py
platform/          win32.py  <-- the ONLY module allowed to import pywin32/ctypes/WMI
telemetry/         heartbeat.py, metrics.py (CPU/mem/GPU)
audit/             log.py (rotating JSONL)
```

**Windows isolation rule:** every Win32 call lives in `platform/win32.py` behind small, typed, intent-named functions (`session_state()`, `processes_by_name(names)`, `launch_detached(argv, cwd)`, `gpu_utilization()`). A `platform/fake.py` implements the same functions for tests, selected by `BUTTERS_AGENT_PLATFORM=fake`. This is the single seam that makes the agent testable and CI-able on the Pi without a Windows box. Everything above `platform/` is plain Python.

Concurrency: one asyncio event loop. Requests dispatch to a bounded worker pool (`max_concurrent_requests = 4`) so a slow `vm.start` cannot stall heartbeats. Per-action mutexes prevent two concurrent launches of the same app.

### 4.3 Request lifecycle inside the agent

1. Frame received → signature verified (if required) → `issued_at` skew checked.
2. `request_id` checked against the replay LRU → cached result returned if seen.
3. Action name looked up in the dispatch table → unknown ⇒ `ack{accepted:false, error_code:"unknown_action"}`.
4. Parameters validated against the action schema → invalid ⇒ `ack{accepted:false, error_code:"invalid_parameter"}`.
5. Preconditions checked (session active? backend available?) → `ack{accepted:false, error_code:"precondition_failed"}`.
6. `ack{accepted:true}` sent immediately, with `expected_duration_ms`.
7. Handler runs under `asyncio.wait_for(timeout)`; `cancel` frames set a cancellation event the handler polls.
8. `result` sent, cached in the replay LRU, and appended to the audit log.

### 4.4 Timeouts, retries, idempotency

- Every action declares a **default timeout** and a **max timeout**; a request may lower it, never raise it above max.
- The agent **never retries on its own.** Retry policy is Butters' business (§10) because only Butters knows whether an action is idempotent and whether the workflow still wants it.
- Idempotency is a per-action property in the dispatch table: `IDEMPOTENT` (`app.launch` — already running is success), `IDEMPOTENT_CONVERGENT` (`vm.start` — converges on a desired state), or `NON_IDEMPOTENT` (`app.restart`). Butters only auto-retries the first two.

### 4.5 Audit information the agent records

Rotating JSONL at `%ProgramData%\Butters\logs\agent-YYYYMMDD.jsonl` (10 MB × 7 files): timestamp, `request_id`, action, sanitized parameters, `accepted`, terminal `state`, error code, duration, and the effective decision reason. Local logs matter because they are the only record of requests that arrived while Butters later lost the result frame.

### 4.6 The `DesktopAgent` Protocol, widened

The existing Protocol in `compute.py` stays source-compatible and gains methods. **Interface assumption for Codex:** keep `status()` and `run_registered(application)`; the agent-backed implementation adds:

```python
class DesktopAgent(Protocol):
    def status(self) -> dict[str, object]: ...                       # existing
    def run_registered(self, application: str) -> dict[str, object]: ...  # existing
    def invoke(self, action: str, parameters: dict) -> dict: ...     # new: generic, validated
    def snapshot(self) -> dict: ...                                  # new: last heartbeat + derived state
```

`run_registered(app)` becomes a thin wrapper over `invoke("desktop.app.launch", {"app": app})`, so existing callers keep working.

---

## 5. Action namespace

### Convention

```
<domain>.<subject>[.<subsystem>].<verb>
```

- Lowercase `a–z`, `0–9`, `_`; dot-separated; ≤ 5 segments; total ≤ 64 chars (matches the existing `^[a-z][a-z0-9_]{0,63}$` identifier rule).
- **The last segment is always a verb** drawn from a closed set: `status`, `list`, `launch`, `start`, `stop`, `restart`, `wake`, `lock`, `sleep`, `shutdown`, `prepare`, `build`, `test`, `history`, `alerts`, `backup`, `on`, `off`, `set`, `cancel`.
- **Variation goes in parameters, never in the name.** `desktop.app.launch{app}` — not `desktop.launch_parsec`. This is the single most important rule for keeping the namespace from rotting: every new app, VM, project or sensor is a *registry entry*, not a new action.
- A **new name is justified only when the semantics differ**, not when the object differs. `desktop.streaming.prepare` earns its own name because it is a multi-step workflow with its own success criterion ("streaming ready"), not a parameterization of `launch`.
- `status` is read-only and must never mutate. Anything that converges state uses `prepare`/`start`/`ensure`-like verbs.

### Namespace

```
# --- desktop: the Windows machine and its interactive session ---
desktop.status                     # composite state (§7); works with agent offline
desktop.wake                       # WOL via broker; no agent required
desktop.lock                       # broker (works agent-down)
desktop.sleep
desktop.restart
desktop.shutdown
desktop.session.status             # logged-in user, locked/unlocked, idle seconds  [agent]
desktop.app.list                   # registry contents + per-app running state      [agent]
desktop.app.launch      {app}                                                       [agent]
desktop.app.status      {app}                                                       [agent]
desktop.app.stop        {app}      # graceful close, registry must opt in           [agent]
desktop.vm.list                                                                     [agent]
desktop.vm.start        {vm}                                                        [agent]
desktop.vm.stop         {vm, mode: "shutdown"|"save"|"force"}                       [agent]
desktop.vm.status       {vm}                                                        [agent]
desktop.streaming.prepare {client?}   # workflow (§10)
desktop.streaming.status              # derived: is remote streaming actually usable

# --- compute: headless work, SSH transport (existing Codex track 1) ---
compute.project.list
compute.build           {project}
compute.test            {project}
compute.build_and_test  {project}
compute.command.run     {command}     # registered command NAME only (existing desktop.run_registered)

# --- nas / servers ---
nas.status
nas.wake
nas.service.status      {service}      # jellyfin | torrent | ...   (parameterized!)
nas.service.start       {service}
nas.service.stop        {service}

minecraft.status
minecraft.start
minecraft.stop
minecraft.backup

# --- IoT / environment (MQTT + Home Assistant; formats owned by Codex track 2) ---
sensor.status           {node?}
sensor.history          {node, metric, window}
sensor.alerts
environment.device.set  {device, state: "on"|"off"}   # dehumidifier | heater | ventilation | fan
environment.status

# --- butters itself ---
butters.status
butters.restart
butters.audit.query     {since?, action?, limit?}
```

Note `environment.device.set{device,state}` collapses six existing broker operations into one action with two enumerated parameters, and `nas.service.*{service}` collapses an open-ended family. **Compatibility assumption:** the existing `BrokerOperation` enum members stay as-is at the broker boundary; the namespace above is the *Action API* surface, and an action maps to one or more broker operations internally. No broker change is required by this design.

### Parameters vs. names — the test

Ask: *would adding the next object require code changes?* If yes, it belongs in a parameter. `desktop.app.launch{app:"parsec"}` adds VS Code by editing a TOML file. `desktop.launch_parsec` + `desktop.launch_vscode` adds a code change, a permission entry, a UI entry, an audit label and an LLM-facing description **per app** — and 30 near-identical actions is exactly the mess to avoid.

---

## 6. Request / response schemas

### 6.1 Request (Butters → agent)

```json
{
  "type": "request",
  "protocol": "1",
  "request_id": "9f1c0d2e-6a4b-4f1e-9c3a-2b7d5e8f0a11",
  "action": "desktop.app.launch",
  "target": "desktop",
  "parameters": { "app": "parsec" },
  "issued_at": "2026-09-07T18:22:03.412Z",
  "timeout_ms": 20000,
  "idempotency_key": "9f1c0d2e-6a4b-4f1e-9c3a-2b7d5e8f0a11",
  "origin": { "source": "manual_ui", "identity": "daniel", "session_id": "web-7f21", "workflow_id": null },
  "sig": "base64url(hmac-sha256)"
}
```

- `target` is a logical machine name (`desktop`, `nas`, `pi`), resolved by Butters to a transport. The agent verifies it matches its own identity and rejects `wrong_target` — this is what stops a request meant for a second machine being honored by the first.
- `idempotency_key` defaults to `request_id`; a workflow retrying the *same logical step* reuses the original key so the agent's cache suppresses double execution.
- `origin.source` ∈ `manual_ui | voice | automation | workflow | internal | cli`.

### 6.2 Ack (agent → Butters), immediate

```json
{ "type": "ack", "request_id": "9f1c…", "accepted": true,
  "expected_duration_ms": 4000, "duplicate": false,
  "error_code": null, "error_message": null }
```

Rejection sets `accepted:false` with an `error_code` and no `result` frame ever follows.

### 6.3 Result (agent → Butters), terminal

```json
{
  "type": "result",
  "protocol": "1",
  "request_id": "9f1c…",
  "action": "desktop.app.launch",
  "target": "desktop",
  "success": true,
  "state": "RUNNING",
  "result": {
    "app": "parsec",
    "already_running": false,
    "pids": [12844],
    "process_names": ["parsecd.exe"],
    "launched": true
  },
  "error_code": null,
  "error_message": null,
  "retryable": false,
  "started_at": "2026-09-07T18:22:03.500Z",
  "completed_at": "2026-09-07T18:22:07.041Z",
  "duration_ms": 3541,
  "metadata": { "agent_version": "0.3.1", "protocol": "1", "duplicate": false },
  "sig": "base64url(hmac-sha256)"
}
```

Field discipline:
- `success` is the boolean the UI and the LLM branch on. `state` is the *resulting* domain state (`RUNNING`, `STOPPED`, `READY`, `LOCKED`, `UNKNOWN`) — an action can succeed and leave a state the caller must still react to.
- `result` is a per-action typed payload. Do not put transport metadata in it.
- `error_code` is a closed enumeration (§6.5); `error_message` is a human sentence, safe to display, never containing raw command output, paths outside the registry, or secrets.
- `retryable` is asserted by the *agent*, which alone knows whether it failed before or after side effects.
- No stdout/stderr fields. The agent is not a shell; that is SSH's job, and `compute.py` already handles bounded capture with sanitization.

### 6.4 Event (agent → Butters), unsolicited

```json
{ "type": "event", "protocol": "1", "event": "app.exited",
  "at": "2026-09-07T19:04:11.002Z", "seq": 4193,
  "data": { "app": "parsec", "pid": 12844, "exit_code": 0 } }
```

Events: `session.locked`, `session.unlocked`, `session.logoff`, `app.started`, `app.exited`, `vm.state_changed`, `agent.shutting_down` (sent best-effort on clean exit so Butters marks it offline instantly), `config.reloaded`. `seq` is monotonic per connection so Butters can detect gaps.

### 6.5 Error codes (closed set)

`unknown_action`, `invalid_parameter`, `wrong_target`, `unauthorized`, `signature_invalid`, `stale_request`, `duplicate_request`, `precondition_failed`, `session_inactive`, `unknown_app`, `app_not_installed`, `launch_failed`, `app_not_running`, `unknown_vm`, `vm_backend_unavailable`, `vm_state_conflict`, `timeout`, `cancelled`, `busy`, `internal_error`, `agent_unavailable` (synthesized by Butters, never by the agent), `transport_error`.

### 6.6 Asynchronous work

Any action longer than ~5 s (`vm.start`, `streaming.prepare`) is already async in this protocol: `ack` returns immediately and `result` arrives later on the same socket, so the HTTP caller in Butters is served by the existing job model in `actions/store.py` (`queued → running → completed`). No separate polling endpoint is needed on the agent side. Long actions may emit `progress` events (`{"type":"event","event":"request.progress","data":{"request_id":…,"stage":"waiting_for_guest","fraction":0.4}}`) which map onto the existing job `stage`/`progress` fields.

---

## 7. State model

A single flat enum lies. "Pingable" and "can launch Parsec" are different questions, so model **independent axes** and derive the composite.

### 7.1 Axes (each observed by a different mechanism)

| Axis | Values | Observed by |
|---|---|---|
| `power` | `UNKNOWN`, `OFF`, `WAKING`, `ON` | ICMP + last WOL time |
| `network` | `UNREACHABLE`, `REACHABLE` | ICMP / TCP connect |
| `os` | `UNKNOWN`, `BOOTING`, `AVAILABLE` | TCP 22 banner (`SSH-`), SSH auth |
| `session` | `UNKNOWN`, `NONE` (logged out), `LOCKED`, `ACTIVE`, `MULTIPLE` | agent `session.status` |
| `agent` | `OFFLINE`, `CONNECTING`, `READY`, `BUSY`, `DEGRADED` | socket + heartbeat |

`os.AVAILABLE` deliberately means "sshd answers and authenticates" — a fact Butters can establish without the agent, and the one that distinguishes "Windows booted" from "the interactive session is usable."

### 7.2 Derived composite (what UI and LLM see)

```
OFFLINE            power=OFF|UNKNOWN, network=UNREACHABLE
WAKING             WOL sent < 120 s ago, still unreachable
HOST_REACHABLE     network=REACHABLE, os != AVAILABLE          (ICMP only — promises nothing)
WINDOWS_BOOTING    port 22 open, auth not yet succeeding
WINDOWS_AVAILABLE  ssh authenticates; agent OFFLINE            (headless compute OK, GUI NOT)
SESSION_INACTIVE   agent READY and reports session NONE/LOCKED (agent up, GUI actions unsafe)
AGENT_CONNECTING   socket up, hello not yet accepted
AGENT_READY        agent READY, session ACTIVE                 <-- the only GUI-capable state
BUSY               AGENT_READY, >= max_concurrent in flight
DEGRADED           agent READY but a subsystem failed (e.g. VM backend absent)
ERROR              auth failure, protocol mismatch, or repeated crash loop
```

Add a **staleness stamp** to everything: `{state, observed_at, age_ms, confidence: "live"|"cached"|"stale"}`. A state older than `3 × heartbeat_interval` is reported `stale`, never as fact. This is what prevents the UI cheerfully showing `AGENT_READY` for a desktop that lost power 40 seconds ago.

### 7.3 Capability predicates — the actual contract

Callers should not switch on the composite state; they should ask for a capability. Butters computes:

```json
"capabilities": {
  "headless_compute": true,     // os=AVAILABLE
  "gui_launch": false,          // agent=READY AND session=ACTIVE
  "session_control": true,      // broker reachable (lock/sleep work agent-down)
  "vm_control": false,          // agent=READY AND vm backend present
  "streaming_ready": false      // gui_launch AND parsec process/service healthy AND session ACTIVE
}
```

**Definition of "desktop ready":** `agent = READY` **and** `session = ACTIVE` (a real interactive, unlocked logon by the configured user) **and** the last heartbeat is younger than `2 × heartbeat_interval`. Nothing weaker qualifies. **"Streaming ready"** additionally requires the Parsec process/service in a healthy state per its registry entry, and — because Parsec cannot serve a locked session usefully — `session = ACTIVE`, not `LOCKED`.

---

## 8. Application registry

Operator-owned TOML on the **Windows** side, `%ProgramData%\Butters\apps.toml`, ACL: administrators write, agent user read. Never modifiable over the wire, never by an LLM.

```toml
schema_version = 1

[apps.parsec]
display_name   = "Parsec"
executable     = 'C:\REPLACE_ME\Parsec\parsecd.exe'   # operator supplies the real path
arguments      = []
working_dir    = ""                                    # optional
process_names  = ["parsecd.exe"]                       # detection, not launch
service_name   = "Parsec"                              # optional: service-backed app
singleton      = true                                  # already running => success, no relaunch
launch_timeout_seconds = 20
ready_check    = "process"                             # process | service | process_and_service | none
stoppable      = false                                 # desktop.app.stop refused unless true
permission     = "routine"

[apps.git_bash]
display_name  = "Git Bash"
executable    = 'C:\REPLACE_ME\Git\git-bash.exe'
arguments     = []
process_names = ["mintty.exe", "bash.exe"]
singleton     = false                                  # multiple windows are legitimate
ready_check   = "process"
stoppable     = false
permission    = "routine"

[apps.vscode]
display_name  = "Visual Studio Code"
executable    = 'C:\REPLACE_ME\Microsoft VS Code\Code.exe'
arguments     = []
process_names = ["Code.exe"]
singleton     = true
ready_check   = "process"
stoppable     = true
permission    = "routine"
```

*(Paths are placeholders. Do not guess real install locations; the operator fills them in at install time and the agent validates them.)*

### Validation (at startup and on `config.reloaded`)

1. Key name matches `^[a-z][a-z0-9_]{0,31}$`; no duplicates.
2. `executable` is an **absolute** path, no `%VAR%` expansion, no `..`, no wildcards, extension in `{.exe, .bat, .cmd, .lnk}` — and `.bat`/`.cmd` are launched with `shell=False` and an argv list, never a composed command line.
3. `executable` must **exist and be a file** at load time. A missing path marks the app `installed: false` (surfaced in `desktop.app.list` and the UI) rather than failing the whole agent — one bad entry must not take the agent down.
4. `arguments` is a list of strings, each ≤ 256 chars, no argument may begin with a path separator supplied at runtime (there is no runtime argument injection at all: arguments come only from this file).
5. `process_names` non-empty when `ready_check` involves process detection; each matches `^[A-Za-z0-9._-]{1,64}$`.
6. Cross-check: `service_name` required if `ready_check` mentions `service`.
7. Any structural violation ⇒ that app is dropped with a logged reason and reported in `desktop.app.list` as `invalid: true`. The agent still starts.

### Process detection

Match on **image name** from a snapshot of processes owned by the agent's own session/user (`platform/win32.processes_by_name`), because a process with the same name in another session is not *this* user's app. Optionally verify the image path prefix matches the registry `executable` directory to defeat name collisions. Detection returns `{running, pids, count, session_ids}`.

For service-backed apps, additionally query the service state (`Running`/`Stopped`/`StartPending`). Parsec is the motivating case: the existing setup runs it as a service set to Automatic, so `ready_check = "process_and_service"` gives an honest answer — service running but no `parsecd.exe` in the user session means "installed and up but not serving this desktop," which is different from "ready."

### Already-running behavior

- `singleton = true`: return `success: true, already_running: true, launched: false`. **Idempotent by construction** — this is what lets a workflow call `launch` unconditionally.
- `singleton = false`: launch another instance; report `launched: true, count: n`.

### Launch behavior

`platform/win32.launch_detached()` — `CreateProcess` with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, `cwd` from the registry, and an environment inherited from the agent's session so the app appears on the user's desktop. The agent **does not wait for exit**; it polls `ready_check` every 250 ms up to `launch_timeout_seconds`.

### Failure reporting

| Situation | `error_code` | `retryable` |
|---|---|---|
| App key not in registry | `unknown_app` | false |
| Registry path missing on disk | `app_not_installed` | false |
| `CreateProcess` failed | `launch_failed` (+ Win32 error number in `result.win32_error`) | true |
| Process never appeared before timeout | `timeout` | true |
| Service required but stopped and cannot start | `launch_failed` | true |
| No active session | `session_inactive` | true (after login) |

---

## 9. VM abstraction

**No hypervisor is chosen here.** The agent selects a backend per VM entry, and an absent backend is a first-class, honestly-reported condition rather than a crash.

```python
class VmBackend(Protocol):
    name: str
    def available(self) -> tuple[bool, str]: ...        # (ok, reason)
    def status(self, ref: VmRef) -> VmStatus: ...       # RUNNING|STOPPED|SAVED|PAUSED|STARTING|STOPPING|UNKNOWN
    def start(self, ref: VmRef) -> VmStatus: ...
    def stop(self, ref: VmRef, mode: StopMode) -> VmStatus: ...
```

| Backend | Availability probe | start | stop (`shutdown` / `save` / `force`) | status |
|---|---|---|---|---|
| `hyperv` | `Get-Command Start-VM` present **and** agent user in `Hyper-V Administrators` | `Start-VM -Name` | `Stop-VM` / `Save-VM` / `Stop-VM -TurnOff` | `Get-VM \| Select State` |
| `vmware` | `vmrun.exe` at configured path | `vmrun start <vmx> nogui` | `stop <vmx> soft` / `suspend` / `stop <vmx> hard` | `vmrun list` + guest tools |
| `virtualbox` | `VBoxManage.exe` at configured path | `VBoxManage startvm <name> --type headless` | `controlvm acpipowerbutton` / `savestate` / `poweroff` | `showvminfo --machinereadable` |

Backend invocations are built from **enumerated verbs plus a registry-supplied VM identifier**, always as an argv list with `shell=False`. The VM name on the wire is a registry *key*, not the hypervisor's VM name, so nothing user-supplied reaches a command line.

```toml
# %ProgramData%\Butters\vms.toml
schema_version = 1

[backends.hyperv]
kind = "hyperv"
requires_elevation = true          # see §22: pre-registered elevated Scheduled Task, no UAC prompt
[backends.vmware]
kind = "vmware"
vmrun_path = 'C:\REPLACE_ME\VMware\VMware Workstation\vmrun.exe'

[vms.dev_linux]
display_name = "Dev Linux"
backend      = "hyperv"
vm_name      = "REPLACE_ME"        # exact hypervisor VM name
start_timeout_seconds = 120
stop_timeout_seconds  = 180
default_stop_mode = "shutdown"
allow_force_stop  = false          # force stop is DESTRUCTIVE; must be opted in per VM
permission = "session_control"

[vms.win_test]
display_name = "Windows Test"
backend      = "vmware"
vm_name      = 'C:\REPLACE_ME\win-test\win-test.vmx'
allow_force_stop = true
permission = "session_control"
```

Semantics:
- `vm.start` is **convergent**: already `RUNNING` ⇒ `success, already_running: true`. `SAVED` ⇒ resume. `STARTING` ⇒ wait for the deadline rather than issuing a second start.
- `vm.stop` with `mode="force"` is refused (`vm_state_conflict`) unless `allow_force_stop = true`; `force` is classified DESTRUCTIVE (§11).
- Backend not available ⇒ `vm_backend_unavailable`, `retryable: false`, with the probe's reason string. The agent reports `DEGRADED`, not `ERROR`, and every other action keeps working.
- Elevation is never requested interactively (§22).

---

## 10. Workflow / orchestration layer

Workflows live **on Butters**, never in the agent. A workflow is a declarative sequence of *existing registered actions* plus waits and predicates — it contains no transport code and no duplicated launch logic. This is the rule that keeps `streaming.prepare` from becoming a second implementation of `app.launch`.

### Step kinds

- `action` — invoke a registered action (any transport).
- `wait_for` — poll a **capability predicate** (§7.3) until true or deadline.
- `assert` — a predicate that must already hold, else the workflow fails at that step.
- `branch` — run a step only if a predicate holds (this is how "send WOL *if needed*" is expressed).

### `desktop.streaming.prepare`

```yaml
name: desktop.streaming.prepare
permission: routine
total_timeout: 300s
cancellable: true
steps:
  - id: probe        kind: action    action: desktop.status
  - id: wol          kind: action    action: desktop.wake
                     when: "not network.REACHABLE"
                     retry: {attempts: 3, backoff: 20s}      # WOL is idempotent
  - id: host         kind: wait_for  predicate: network.REACHABLE            timeout: 90s
  - id: windows      kind: wait_for  predicate: capabilities.headless_compute timeout: 120s
  - id: agent        kind: wait_for  predicate: agent.READY                   timeout: 90s
  - id: session      kind: assert    predicate: session.ACTIVE
                     on_fail: {error_code: session_inactive,
                               message: "No user is logged in and unlocked on the desktop."}
  - id: parsec_state kind: action    action: desktop.app.status  params: {app: parsec}
  - id: parsec_up    kind: action    action: desktop.app.launch  params: {app: parsec}
                     when: "not parsec_state.result.running"
                     retry: {attempts: 2, backoff: 5s}        # singleton => idempotent
  - id: verify       kind: wait_for  predicate: capabilities.streaming_ready  timeout: 45s
report:
  success: {state: STREAMING_READY, fields: [session.user, parsec.pids, total_duration_ms]}
```

Note step `wol` uses the **broker**, `windows` waits on **SSH**, and `parsec_up` uses the **agent** — three transports, one workflow, zero transport logic in the workflow definition.

### `compute.build_and_test`

```yaml
name: compute.build_and_test
permission: routine
total_timeout: 1800s
steps:
  - id: reach   kind: assert   predicate: capabilities.headless_compute
                on_fail: {error_code: precondition_failed}
  - id: build   kind: action   action: compute.build  params: {project: "{{project}}"}
  - id: test    kind: action   action: compute.test   params: {project: "{{project}}"}
                when: "build.success"
report:
  fields: [build.success, build.duration_ms, test.success, test.duration_ms, failure_summary]
partial: report_per_step        # a failed build reports build output and marks test SKIPPED
```

It deliberately does **not** touch the agent: build and test are headless, so they must keep working with the user logged out. It reuses `compute.build` / `compute.test` as-is (existing Codex implementation), adding only sequencing.

### Cross-cutting policy

- **Timeouts** are three-tiered: per-action (agent-enforced), per-step (Butters-enforced, may exceed nothing), and per-workflow (`total_timeout`, hard stop). A step timeout never silently extends the workflow budget; exceeding the workflow budget cancels the in-flight step.
- **Retry** is allowed only for steps whose action is `IDEMPOTENT`/`IDEMPOTENT_CONVERGENT` **and** whose failure is `retryable: true`. Exponential backoff, capped attempts, and the retry **reuses the original `idempotency_key`** so a lost-result-frame retry cannot double-launch.
- **Cancellation** is cooperative and propagated: Butters sends `cancel{request_id}`, marks the workflow `cancelled`, and records which steps had already committed side effects. Already-completed steps are **not** rolled back — there is no compensation logic, and pretending otherwise would be worse than reporting honestly.
- **Partial failure** always yields a per-step report (`completed`/`failed`/`skipped`/`cancelled`) plus the first `error_code`. A workflow never reports overall success on partial completion.
- **Idempotency** at the workflow level: a workflow invocation carries a `workflow_id`; re-invoking the same workflow while one is in flight for the same target returns `busy` rather than racing.
- **Dependency checking** is the `assert`/`wait_for` predicates — a workflow declares what it needs and fails with a specific, human-meaningful message instead of attempting a step doomed to fail.

---

## 11. Permission model

Five categories, mapped onto the **existing** `AuthenticationLevel` in `butters/src/butters/skills/model.py` so nothing new has to be invented in the coordinator:

| Category | Meaning | Existing auth level | Manual UI | Voice |
|---|---|---|---|---|
| `READ_ONLY` | Observes only | `NONE` | direct | direct |
| `ROUTINE` | Reversible, low-consequence | `ELEVATED` | direct | direct |
| `SESSION_CONTROL` | Affects the live session or a VM's run state | `ELEVATED` | **explicit confirm** | **confirm + read-back** |
| `DESTRUCTIVE` | Data loss or forced power state | `FRESH` | **typed/second-factor confirm** | **refuse → require UI or a second explicit turn** |
| `ADMIN` | Changes Butters/agent config or credentials | `FRESH` | UI only | **never** |

Mapping:

```
READ_ONLY        *.status, *.list, sensor.history, sensor.alerts, butters.audit.query,
                 desktop.session.status, desktop.streaming.status
ROUTINE          desktop.wake, desktop.app.launch, desktop.streaming.prepare,
                 compute.build/test/build_and_test, environment.device.set,
                 nas.wake, nas.service.start, minecraft.start, minecraft.backup
SESSION_CONTROL  desktop.lock, desktop.sleep, desktop.app.stop, desktop.vm.start,
                 desktop.vm.stop{mode: shutdown|save}, minecraft.stop, nas.service.stop
DESTRUCTIVE      desktop.restart, desktop.shutdown, desktop.vm.stop{mode: force},
                 butters.restart
ADMIN            credential rotation, registry/config changes, permission changes
```

Note the permission can depend on **parameters**: `vm.stop{mode:"force"}` is DESTRUCTIVE while `vm.stop{mode:"shutdown"}` is SESSION_CONTROL. The permission function therefore takes `(action, parameters)`, not just the name — and per-entry `permission` fields in the app/VM registries may only *raise* the floor, never lower it.

### Confirmation rules

- **Voice recognition is not authorization for anything above ROUTINE.** Speaker identification is probabilistic and replayable (a recording, a TV, a housemate). The existing `pending_confirmation` / `FRESH` machinery already handles this: SESSION_CONTROL over voice requires an explicit confirming utterance bound to the frozen plan digest; DESTRUCTIVE over voice is refused with "confirm that in the control panel."
- Confirmations are **bound to the frozen plan digest** (as `ActionCoordinator.execute` already enforces), so a confirmation cannot be harvested for a different action.
- Confirmation prompts must name the concrete target and consequence: "Shut down DESKTOP-G4CFVL1, ending any running build?" — not "Confirm action?"
- Confirmations expire (60 s suggested) and are single-use.

---

## 12. Manual control panel integration (design only — Codex owns the implementation)

What the panel should eventually expose. **Every control must work with no cloud model configured and no LLM in the path** — the panel calls the Action API directly.

**Desktop** — composite state badge with `observed_at`/staleness; capability chips (`headless compute`, `GUI`, `VM`, `streaming`); the five axes on hover; buttons: Wake, Lock, Sleep, Restart, Shut down; agent panel showing agent version, protocol, connected-since, last heartbeat age, reconnect count.

**Applications** — one row per registry entry (Parsec, Git Bash, VS Code, …), rendered *from* `desktop.app.list` so adding an app to the TOML makes it appear with no UI change. Row shows running/stopped/not-installed/invalid, instance count, Launch (disabled with a tooltip when `gui_launch` is false), Stop only when `stoppable`.

**Compute** — project selector from `compute.project.list`, buttons Build / Test / Build + Test, live job stage/progress from the existing job store, and bounded output panes. Explicitly available while the agent is offline (with a note saying why that's fine).

**VMs** — one row per VM registry entry: state, Start, Stop (mode selector), Force stop shown only when `allow_force_stop`, backend availability warning when `DEGRADED`.

**Servers** — NAS (status, wake, per-service rows), Minecraft (status, start, stop, backup, player count), other machines.

**Environment** — current sensor readings and automation state (read-only, sourced from the existing sensor stack), plus dehumidifier/heater/ventilation toggles via `environment.device.set`.

**Recent activity** — the last N audit entries with source (UI/voice/automation/workflow), outcome and duration. This is the fastest way to answer "did that actually work?"

### Distinguishing dangerous operations

Four escalating treatments, tied to the permission category — not to the developer's mood:
1. `READ_ONLY`/`ROUTINE`: normal buttons, no confirmation.
2. `SESSION_CONTROL`: amber outline + a modal naming the target and consequence.
3. `DESTRUCTIVE`: red, grouped in a collapsed **"Power & destructive"** section (never adjacent to routine buttons, so no mis-click reaches them), modal requiring a typed confirmation of the target name, plus a 3-second enable delay to defeat reflex clicking.
4. `ADMIN`: not in the panel at all; CLI/config only.

Also: disable rather than hide unavailable actions, and always say *why* ("No interactive session — log in on the desktop"). A greyed button with a reason teaches the state model; a missing button looks like a bug.

---

## 13. LLM / voice integration

```
utterance
   │
   ├─► local deterministic router  ── hit ──►  candidate action + params
   │   (existing butters/src/butters/routing/)
   │
   └─ miss / compound ─► cloud planner (OpenAI, optional) ─► action plan (JSON)
                                     │
                                     ▼
                      plan validation: names exist, schemas pass, permissions,
                      step count ≤ N, no ADMIN, target allowed
                                     │
                                     ▼
                      ActionCoordinator.freeze_plan()  (existing)
                                     │
                          confirmation if required (§11)
                                     ▼
                          deterministic execution  ─► results
                                     ▼
                      response: template (default) or LLM narration (optional)
```

**Routing behavior — two tiers, cheap first.** The existing `routing/` module and `_direct_desktop_action` already demonstrate the pattern: "turn on my desktop", "lock the desktop", "is the desktop on" resolve locally to a single action with no tokens spent, no network dependency, and ~zero latency. Only escalate to a cloud planner when the local router misses *and* the utterance shows compound structure (multiple imperatives, conjunctions, references like "then"/"and summarize"). Something like *"Pull the latest version of my CPU project, build it on the desktop, run the tests, and summarize what failed"* is exactly the case that earns a planner call: it needs a 3–4 step plan and a natural-language summary of build output.

Coexistence rules:
1. **Local first, always.** A local hit never consults the cloud.
2. **Escalation is explicit and observable.** Audit records `routed_by: local | cloud`, the model id, and token cost.
3. **No cloud, no problem.** With no API key configured, the local router remains fully functional and compound requests get an honest "I can do these one at a time — which first?" rather than a failure. This is a hard requirement: cloud availability is never on the critical path for control.
4. **The planner emits data, never effects.** Its output is a JSON plan of `{action, parameters}` validated against the *same* registry the UI uses. An action the planner invents, mis-parameterizes, or lacks permission for is rejected before freezing — the model cannot widen its own authority.
5. **Bounded plans.** Max steps (5 suggested), no loops, no conditionals from the model (branching belongs to operator-authored workflows), no ADMIN category, and DESTRUCTIVE steps rejected outright from a voice-originated plan.
6. **Prefer naming a workflow over re-planning one.** If the utterance matches an existing workflow ("get my desktop ready for streaming"), the planner should emit `desktop.streaming.prepare`, not re-derive nine steps. Give the model the workflow catalog for exactly this reason.
7. **Narration is optional and after the fact.** Results are summarized by templates by default; the LLM may rewrite the summary but never re-interprets whether something succeeded.

---

## 14. Audit logging model

One append-only record per action attempt, written **on Butters** (authoritative) and mirrored locally on the agent (§4.5).

```json
{
  "audit_id": "01J…",              "ts": "2026-09-07T18:22:03.412Z",
  "request_id": "9f1c…",           "workflow_id": "wf-77a1",  "step_id": "parsec_up",
  "source": "voice",               "identity": "daniel",      "session_id": "web-7f21",
  "routed_by": "local",            "model": null,
  "target": "desktop",             "transport": "agent",
  "action": "desktop.app.launch",  "parameters": {"app": "parsec"},
  "permission": "routine",         "authentication": "elevated", "confirmed": false,
  "accepted": true,                "outcome": "completed",
  "state": "RUNNING",              "error_code": null,
  "started_at": "…", "completed_at": "…", "duration_ms": 3541,
  "attempt": 1, "duplicate": false, "agent_version": "0.3.1"
}
```

`source` ∈ `manual_ui | voice | automation | workflow | internal | cli` — and a workflow's child steps carry both `source: workflow` and the originating `workflow_id`, so "who started this" is always answerable one hop up.

**Never logged:** passwords, API keys, agent tokens or their prefixes, command keys, private keys, session cookies, bearer tokens, raw voice audio, full transcripts of utterances containing credentials. Parameters are logged only because the namespace is designed so that parameters are *registry keys and enumerated values*, never free text — a real security benefit of the naming rule in §5. Any future free-text parameter must be declared `sensitive` in its schema and be redacted to `"[redacted]"`, and all messages pass through the existing `diagnostics/sanitizer.py` before storage.

**Storage:** SQLite on the Pi — the mechanism `actions/store.py` already uses (`store.audit(...)`). Reasons: transactional, queryable for the panel's "recent activity" and for `butters.audit.query`, single file to back up, no daemon. WAL mode, one `audits` table with indexes on `(ts)` and `(action, ts)`, and a retention job trimming to 180 days (or 100 k rows) with monthly rollup counts kept indefinitely. Mirror a plain-text JSONL copy under `/var/log/butters/` for `journalctl`-style grepping and off-box rsync; the agent's own JSONL covers the case where Butters never received the result.

---

## 15. Startup and recovery behavior

| Scenario | Required behavior |
|---|---|
| **Butters reboots** | Web/action service starts under systemd (`Restart=always`, existing `butters/systemd/`). All agent state is reconstructed from the agent's reconnect + first heartbeat — Butters persists no desktop state it cannot re-derive. In-flight jobs from before the reboot are marked `interrupted` at startup, never left `running` forever. The agent's redial loop means recovery needs no action on Windows. |
| **Windows reboots** | Butters observes the axes independently and in order: `network` → `os` (SSH) → `agent`. SSH commonly returns **before** any user logs in, so the correct reported state is `WINDOWS_AVAILABLE` with `gui_launch: false`. This must never be shown as "desktop ready." Auto-login, if enabled, moves it to `AGENT_READY` seconds later; if not, it correctly stays `WINDOWS_AVAILABLE` until someone logs in. |
| **User logs out** | The agent process dies with the session. Butters sees the socket close (and ideally an `agent.shutting_down` event), sets `agent: OFFLINE`, `session: NONE`, keeps `headless_compute: true`. GUI actions fail `session_inactive`. The panel disables Launch buttons with that reason. The logon trigger restarts the agent at next login. |
| **Agent crashes** | The Scheduled Task's `RestartCount` plus the 5-minute `IgnoreNew` trigger restart it. The agent also writes a crash breadcrumb (exception + version) to its JSONL so the cause survives. Butters counts reconnects; > 5 in 10 minutes ⇒ report `ERROR` with "agent crash loop" instead of flapping the UI. |
| **Network interruption** | Agent redials with exponential backoff: 1, 2, 4, 8, 15, 30, 60 s, then steady 60 s ± 10 s jitter, forever. On reconnect it re-sends `hello`, and Butters treats it as a **fresh session**: sequence numbers reset, in-flight requests from the old connection are resolved as `transport_error` (`retryable` per action idempotency) — never left dangling. |
| **Butters unavailable** | The agent **queues nothing**. It has no command backlog by design: commands arrive only over a live socket, and a command whose socket died has no result path anyway. While disconnected it keeps running (so state is fresh the instant it reconnects) and keeps its local audit log. |

### Stale command handling

Three independent guards, because each catches a different failure:
1. **Skew window** — `issued_at` older than `clock_skew_seconds` (90 s) ⇒ `stale_request`. This is what stops a command written into a dead socket buffer from executing minutes later.
2. **Per-action freshness** — SESSION_CONTROL and DESTRUCTIVE actions additionally reject anything older than 30 s. "Shut down the desktop" from four minutes ago is never what the operator wants now.
3. **Connection-scoped validity** — a `request_id` from a superseded connection is rejected. Combined with the replay LRU, a re-sent request during reconnect returns the cached result rather than re-executing.

Clocks: the agent compares against its own clock but records the offset from `welcome.server_time`; if the offset exceeds 30 s it logs a warning and reports `DEGRADED`, because bad clocks silently break both replay protection and freshness.

---

## 16. Suggested Python project layout

```
butters-agent/                      # separate deployable; ships to Windows, not into butters/src
├── pyproject.toml                  # python >= 3.12; deps: websockets, cryptography, pywin32, tomli-w(dev)
├── README.md                       # install/rollback runbook (mirrors butters/windows/README.md style)
├── install/
│   ├── install-agent.ps1           # creates Scheduled Task, ACLs %ProgramData%\Butters, provisions token
│   └── uninstall-agent.ps1
├── src/butters_agent/
│   ├── __main__.py                 # python -m butters_agent
│   ├── main.py                     # supervisor: load config -> connect -> serve -> redial
│   ├── config.py                   # dataclasses + validation; fail closed
│   ├── version.py                  # AGENT_VERSION, PROTOCOL_VERSION, ACTION_SCHEMA_VERSION
│   ├── transport/    __init__.py  ws_client.py  framing.py  envelope.py  backoff.py
│   ├── security/     credentials.py  pinning.py  replay.py  validate.py
│   ├── actions/      dispatch.py  session.py  apps.py  vms.py  system.py  schemas.py
│   ├── applications/ registry.py  launcher.py  detect.py
│   ├── vm/           base.py  registry.py  hyperv.py  vmware.py  virtualbox.py
│   ├── state/        session.py  machine.py  snapshot.py
│   ├── telemetry/    heartbeat.py  metrics.py
│   ├── audit/        log.py
│   └── platform/     __init__.py  win32.py  fake.py      # the only OS-specific code
└── tests/
    ├── unit/            # validation, schemas, replay, backoff, registry parsing
    ├── contract/        # protocol frames vs. Butters' expectations (shared fixtures)
    └── integration/     # against platform/fake.py; runnable on the Pi in CI
```

**Deliberate non-features:** no plugin loader, no DI container, no ORM, no async task queue, no metrics server, no config hot-reload beyond an explicit signal, no multi-user support. One user, one desktop. Roughly 2–3k lines total is the target; if it grows past that, something has been over-designed.

On the **Butters side**, the additions are small and stay clear of Codex's current files:

```
butters/src/butters/desktop_agent/     # new package
    client.py        # AgentSessionClient: implements the DesktopAgent Protocol
    server.py        # WS endpoint handler (registered by web/app.py)
    session.py       # per-connection state, heartbeat deadlines, seq tracking
    envelope.py      # shared signing (mirror of the agent's, same canonical form)
    state.py         # axes -> composite state + capability predicates
butters/src/butters/workflows/         # new package
    engine.py  definitions/streaming_prepare.yaml  definitions/build_and_test.yaml
```

---

## 17. Example configuration

### Agent, `%ProgramData%\Butters\agent.toml` (no secrets)

```toml
schema_version = 1

[identity]
agent_id     = "desktop-main"          # stable; matches an entry in Butters' agent-tokens.toml
display_name = "Main Windows Desktop"

[butters]
url               = "wss://butters.lan:8443/agent/v1/session"
# Optional alternates tried in order; enables the future Tailscale move with no code change.
fallback_urls     = ["wss://butters.tailnet-XXXX.ts.net:8443/agent/v1/session"]
server_spki_sha256 = "REPLACE_ME_BASE64_SPKI_HASH"      # pinned; trust store ignored
connect_timeout_seconds = 10

[credentials]
# References only. Never inline secrets.
token_source  = "dpapi"                                  # dpapi | credential_manager | env
token_ref     = 'C:\ProgramData\Butters\agent.cred'      # DPAPI blob, user-scoped
command_key_ref = 'C:\ProgramData\Butters\command.cred'

[protocol]
require_signatures  = false        # flip to true in Phase 3; wire format already carries `sig`
clock_skew_seconds  = 90
heartbeat_interval_seconds = 15
reconnect_backoff_seconds  = [1, 2, 4, 8, 15, 30, 60]

[limits]
max_concurrent_requests = 4
default_action_timeout_seconds = 30
max_action_timeout_seconds     = 600
idempotency_window_seconds     = 300
replay_cache_entries           = 512

[registries]
applications = 'C:\ProgramData\Butters\apps.toml'
vms          = 'C:\ProgramData\Butters\vms.toml'

[audit]
directory   = 'C:\ProgramData\Butters\logs'
max_bytes   = 10485760
backup_count = 7

[telemetry]
include_gpu = true                 # dropped silently if no provider is available
```

### Butters, `/etc/butters/agents.toml` (`root:butters 0640`)

```toml
schema_version = 1

[agents.desktop_main]
agent_id   = "desktop-main"
target     = "desktop"                       # logical target name used in requests
hostname   = "DESKTOP-G4CFVL1"               # for ICMP/SSH axis probing
token_hash = "argon2id$REPLACE_ME"           # hash only
command_key_ref = "file:/etc/butters/secrets/desktop-main.key"   # 0600 root-only
heartbeat_timeout_seconds = 45               # 3 x interval
protocol_min = 1
protocol_max = 1
```

Secrets live in `/etc/butters/secrets/` (`0600`, `root`), referenced by path, excluded from git via the existing ignore rules, and rotated by writing both sides and bouncing the agent. Nothing secret appears in `assistant.toml`, in this repo, or in any log line.

---

## 18. Heartbeat and status design

**Heartbeat every 15 s — small, fixed-shape, and cheap to produce.** Anything requiring process enumeration, WMI, or a hypervisor query is on-demand only, because those are the expensive calls and stale copies of them are worse than none.

```json
{ "type": "heartbeat", "protocol": "1", "seq": 811,
  "at": "2026-09-07T18:22:00.000Z",
  "agent_version": "0.3.1",
  "uptime_seconds": 41233,
  "session": { "state": "ACTIVE", "user_hash": "3f9a…", "locked": false, "idle_seconds": 42, "session_count": 1 },
  "load": { "cpu_pct": 12, "mem_pct": 47, "gpu_pct": 3 },
  "subsystems": { "apps": "ok", "vm": "unavailable" },
  "counters": { "requests": 128, "failures": 2, "reconnects": 1 } }
```

~350 bytes; at 15 s that is ~2 MB/day. The username is sent as a salted hash (`user_hash`) with the plain name available on demand via `desktop.session.status` — the heartbeat is the highest-volume, most-logged message, and it does not need to carry an account name.

| Data | Cadence | Why |
|---|---|---|
| session state, locked, idle | periodic | drives capability predicates and the UI badge; cheap |
| CPU/mem/GPU | periodic | small, and useful as a time series for "is the desktop busy?" |
| subsystem health, counters | periodic | catches `DEGRADED` and crash loops without polling |
| per-app running state | **on demand** (`desktop.app.list`) + push on change | process enumeration is expensive; events keep it fresh |
| per-VM state | **on demand** + push on change | hypervisor queries are slow (100 ms–2 s) |
| Windows version / build, hostname, CPU model | **once in `hello`** | immutable within a session |
| installed-app paths, registry contents | on demand | changes only when a human edits the TOML |

Butters marks the agent `OFFLINE` if no heartbeat arrives within `heartbeat_timeout_seconds` (45 s) even if the TCP socket looks open — half-open sockets after a sleep or Wi-Fi change are the normal failure, not a clean FIN.

---

## 19. Protocol and version strategy

Three versions, deliberately separate because they change at different rates:

1. **`PROTOCOL_VERSION`** — integer, on every frame; framing, types, envelope, auth. Bumped only on a breaking wire change. Negotiated: `hello` sends `{protocol_min, protocol_max}`; `welcome` returns the chosen version or closes with `protocol_unsupported`. Butters supports **N and N−1** for one release cycle so agent and Butters need not be upgraded simultaneously — which matters, because they upgrade by different mechanisms (rsync to the Pi vs. a manual Windows install).
2. **`ACTION_SCHEMA_VERSION`** — integer, advertised in `hello.capabilities.actions` as an explicit list of supported action names with their parameter schema hashes. Butters intersects that list with its registry and **disables actions the agent doesn't advertise** rather than failing at call time. Adding an action is a non-breaking bump; changing a parameter's meaning requires a new action name (see below).
3. **`AGENT_VERSION`** — semver, informational, in `hello`, heartbeats and audits.

Compatibility rules that keep upgrades painless:
- **Additive only** within a protocol version: new optional fields, new event types, new actions. Unknown fields are ignored by both sides (never rejected). Unknown event types are logged and dropped.
- **Never repurpose a name.** Changing the semantics of an action or a parameter means a new action (`…launch` → `…launch_v2`) or a new parameter, never a redefinition — a redefinition silently breaks stored workflows, audits and LLM prompts.
- **Never repurpose an error code.** Add codes; the closed set may only grow.
- Butters records the observed `protocol`/`agent_version` in every audit row, so post-hoc "which version did that" is answerable.
- Config files carry `schema_version` (already shown throughout §17) and the agent refuses to start on an unknown major.

---

## 20. Phased implementation roadmap

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **0 — Prerequisites** (Codex finishes) | SSH foundation, Action API, Tools panel, `DesktopAgent` Protocol seam in place | `desktop.build_and_test` works from the panel; `desktop_agent.available = false` renders correctly |
| **1 — Transport skeleton** | WS endpoint on Butters; agent connects, authenticates (pinned TLS + token), heartbeats; Scheduled Task install script; `desktop.session.status`, `desktop.status` composite + capability predicates; state axes wired into the panel badge | Agent survives logout/login/reboot/network drop unattended for 48 h; panel shows honest states in all six recovery scenarios (§15) |
| **2 — Applications** | App registry + validation; `desktop.app.list/launch/status/stop`; `AgentSessionClient` implements the widened Protocol; `run_registered` delegates to it | Parsec, Git Bash, VS Code launch visibly; already-running is idempotent; missing path reports `app_not_installed`; every §21 app test passes |
| **3 — Hardening** | HMAC envelope signing (`require_signatures = true`), replay LRU, freshness windows, permission categories wired to `AuthenticationLevel`, audit rows on Butters + JSONL on the agent | Replay, skew, wrong-target, duplicate-ID and unauthorized tests all fail closed; audit contains no secrets under a grep audit |
| **4 — Workflows** | Workflow engine; `desktop.streaming.prepare`; `compute.build_and_test` recomposed from existing actions; cancellation and partial-failure reporting | Cold-boot → streaming-ready in one action from the panel; every step's failure produces a distinct, actionable message |
| **5 — VMs** | VM abstraction + one backend (whichever hypervisor is actually installed); registry; convergent start/stop; `DEGRADED` handling | VM start/stop/status works; backend-absent path reports cleanly and breaks nothing else |
| **6 — LLM planning** | Cloud planner behind the local router; plan validation against the registry; workflow catalog exposed to the planner; templated result narration | Compound utterance produces a validated multi-step plan; **all** control still works with the API key removed |
| **7 — Breadth** | NAS/Minecraft/environment actions consolidated into the parameterized namespace; audit query UI; retention job | Namespace has no per-object action names; panel is driven entirely by registries |

Phases 1–2 are the minimum useful increment (visible GUI launching with honest state). Phase 3 should not be deferred past first daily use.

---

## 21. Detailed test plan

Layers: **unit** (pure, no Windows), **contract** (frames validated against shared JSON fixtures, run in both repos), **integration** (agent against `platform/fake.py`, runnable on the Pi), **live** (real Windows box, manual checklist with recorded evidence).

### Transport and authentication

| # | Test | Expected |
|---|---|---|
| 1 | Normal connection | `hello` → `welcome`, heartbeats at 15 s ± 2 s, `AGENT_READY`, capability chips correct |
| 2 | Bad token | Butters closes with `unauthorized`; agent backs off (no hot loop); Butters logs one audit row, no state change |
| 3 | Wrong pinned SPKI (impersonated Butters) | Agent refuses the TLS peer, logs `pin_mismatch`, does **not** send the token, keeps retrying |
| 4 | Wrong `agent_id` / unknown agent | Rejected `unauthorized`; no session created |
| 5 | Protocol too new/old | `protocol_unsupported`, clean close, actionable log line on both sides |
| 6 | Second connection with the same `agent_id` | Old socket closed `superseded`; exactly one live session; in-flight requests resolved `transport_error` |
| 7 | Signature invalid (Phase 3) | `signature_invalid`, no execution, audit row |
| 8 | `issued_at` skewed +10 min / −10 min | `stale_request`, no execution |
| 9 | Malformed frame (bad JSON, missing `type`, 10 MB frame) | `error` frame or close; agent stays alive; nothing executes |
| 10 | Malformed **response** (agent sends garbage result) | Butters marks the job `failed` `transport_error`; does not crash; does not report success |

### Lifecycle and recovery

| # | Test | Expected |
|---|---|---|
| 11 | Agent killed (`taskkill`) | Restarts within ≤ 5 min (Scheduled Task); Butters shows `OFFLINE` within 45 s, then `AGENT_READY` |
| 12 | Butters service restarted | Agent reconnects within its backoff; in-flight jobs marked `interrupted`, none stuck `running` |
| 13 | Network cable pulled / Wi-Fi toggled 60 s | Reconnect with backoff; no duplicate execution of the in-flight request |
| 14 | Half-open socket (drop packets silently) | Heartbeat timeout marks `OFFLINE` in ≤ 45 s despite an "open" socket |
| 15 | Windows logout | `agent OFFLINE`, `session NONE`, `headless_compute` still true; SSH build still succeeds |
| 16 | Windows reboot, no login | `WINDOWS_AVAILABLE`, `gui_launch: false`; `app.launch` ⇒ `session_inactive`; **never** shown as ready |
| 17 | WOL then delayed startup (90 s) | `streaming.prepare` waits through `WAKING → HOST_REACHABLE → WINDOWS_AVAILABLE → AGENT_READY`; no premature failure; total under the workflow budget |
| 18 | Desktop sleeps mid-request | In-flight ⇒ `transport_error`; retried only if idempotent; state converges to `OFFLINE` |
| 19 | Agent clock skewed by 5 min | `DEGRADED` reported, warning logged, signed requests rejected as designed (fail closed, not silently pass) |
| 20 | Crash loop (agent exits on start 6×) | Butters reports `ERROR` "crash loop", stops flapping the UI, breadcrumb in agent JSONL |

### Validation, idempotency, authorization

| # | Test | Expected |
|---|---|---|
| 21 | Duplicate `request_id` (same params) | Cached result returned, `duplicate: true`, **exactly one** launch observed |
| 22 | Duplicate `request_id`, different params | `duplicate_request` rejection (never execute under a reused ID) |
| 23 | Repeated idempotent command (`app.launch parsec` ×3, new IDs) | `already_running: true` on 2 and 3; one process |
| 24 | Repeated `vm.start` while `STARTING` | Waits, no second start issued, converges `RUNNING` |
| 25 | Invalid action name (`desktop.app.lunch`, `desktop.shell.run`) | `unknown_action`; nothing executed; audited |
| 26 | Invalid parameters: unknown key, wrong type, extra key, empty, 10 KB string, `../`, `;`, null byte, unicode homoglyph | `invalid_parameter` for each; no execution |
| 27 | Unauthorized action (DESTRUCTIVE from voice) | Refused with the confirm-in-panel message; audited with `outcome: denied` |
| 28 | SESSION_CONTROL over voice without confirmation | Requires confirmation bound to the plan digest; unconfirmed ⇒ not executed |
| 29 | Confirmation reused for a different action | Rejected (`fresh_authentication_required` — existing coordinator behavior) |
| 30 | `target` mismatch (request for `nas` sent to desktop agent) | `wrong_target`, no execution |
| 31 | Stale DESTRUCTIVE request (45 s old) | `stale_request` (30 s per-action freshness) |

### Applications

| # | Test | Expected |
|---|---|---|
| 32 | Launch app already running (singleton) | `success`, `already_running: true`, `launched: false`, one process |
| 33 | Launch non-singleton twice | Two instances, `count: 2` |
| 34 | Launch app with registry path missing | `app_not_installed`, `retryable: false`, `installed: false` in `app.list` |
| 35 | Launch unknown key | `unknown_app` |
| 36 | Launch fails (`CreateProcess` error injected) | `launch_failed` + `win32_error`, `retryable: true` |
| 37 | Process never appears within `launch_timeout_seconds` | `timeout`, honest `state: UNKNOWN`, no false success |
| 38 | Parsec launch failure (service stopped and unstartable) | `launch_failed`; `streaming_ready` stays false; workflow reports the Parsec step specifically |
| 39 | Malformed `apps.toml` entry | That app `invalid: true`; the agent still starts; other apps work |
| 40 | Same-named process in another session | Not counted as running for this session |
| 41 | `app.stop` on `stoppable = false` | Rejected `precondition_failed` |

### VMs

| # | Test | Expected |
|---|---|---|
| 42 | VM already running ⇒ `vm.start` | `success`, `already_running: true` |
| 43 | Backend binary/cmdlet absent | `vm_backend_unavailable`, `retryable: false`, agent `DEGRADED`, apps still work |
| 44 | Backend present but user lacks permission | `vm_backend_unavailable` with the permission reason (not `internal_error`) |
| 45 | `vm.stop{force}` on `allow_force_stop = false` | `vm_state_conflict`, refused |
| 46 | Unknown VM key | `unknown_vm` |
| 47 | Guest ignores graceful shutdown | `timeout` at `stop_timeout_seconds`, state honestly `RUNNING`, no automatic escalation to force |

### Workflows

| # | Test | Expected |
|---|---|---|
| 48 | `streaming.prepare` from cold (desktop off) | Full sequence; ends `STREAMING_READY`; step timings in the report |
| 49 | `streaming.prepare` when already ready | Fast path, every step idempotent, no relaunch, < 5 s |
| 50 | `streaming.prepare` with nobody logged in | Fails at `session` assert with `session_inactive` and a human message; no partial Parsec launch left behind |
| 51 | Partial failure: build fails | `build` reported failed with output, `test` `SKIPPED`, workflow `failed` (never partial-success) |
| 52 | Cancellation mid-workflow | `cancel` propagated; workflow `cancelled`; report lists which steps had committed side effects |
| 53 | Stale workflow command (queued 10 min, then delivered) | Rejected before execution; workflow `failed` `stale_request` |
| 54 | Workflow `total_timeout` exceeded | In-flight step cancelled; per-step report; no orphan job left `running` |
| 55 | Two concurrent `streaming.prepare` for one target | Second returns `busy` |
| 56 | Step retry after a lost result frame | Same `idempotency_key` ⇒ cached result, exactly one side effect |

### Integration recommendations

- **Shared frame fixtures.** One directory of canonical JSON frames (valid and hostile) consumed by tests in *both* the Butters repo and the agent repo. This is the only practical defense against the two sides drifting, and it makes the wire format the contract rather than the code.
- **`platform/fake.py` is the workhorse.** It must be able to simulate: no session, locked session, two sessions, app present/absent/invalid, launch failure with a chosen Win32 error, slow launch, VM in every state, backend absent, and clock skew. Every row in §21 except the "live" ones should run in CI on the Pi with no Windows machine.
- **A hostile-peer harness.** A test client that connects to Butters' endpoint with a wrong pin, wrong token, replayed frames, oversized frames and truncated TLS — run it in CI, not by hand.
- **Soak test.** 72 h with a scripted logout/login every 6 h, a network drop every hour, and an agent kill every 12 h; assert zero stuck jobs, zero duplicate executions, and that reported state matched reality at every sampled minute.
- **Live checklist.** The handful of tests that need real Windows (2, 3, 11, 15, 16, 17, 32, 38, and one per configured VM) get a written runbook with recorded results, following the existing precedent of physically verifying broker operations before trusting them.
- **Fault injection knobs** in the agent (`BUTTERS_AGENT_FAULT=drop_result|slow_ack|bad_sig`) so lost-result-frame and slow-ack paths are testable without a network emulator.

---

## 22. Failure modes and mitigations

| Failure mode | Why it bites | Mitigation |
|---|---|---|
| **Session 0 isolation** | A service-hosted agent launches GUI apps into an invisible session; the app "runs" but no window exists — the single most common way this project could fail | Agent runs as a Scheduled Task with `InteractiveToken` in the user session. Never a service. `app.launch` verifies the process appears **in the agent's own session**, so an invisible launch is reported as failure, not success |
| **UAC / elevation** | An elevated action from a non-elevated agent triggers an invisible consent prompt on the secure desktop and hangs until timeout | The agent is never elevated and never triggers UAC. Elevated needs go to (a) the existing root broker, or (b) a pre-registered elevated Scheduled Task created once by an administrator. Any action that would prompt is refused with `precondition_failed` and a message naming the required install step |
| **Executable paths change** (app updates, `WindowsApps` shims, drive letters) | Launch silently breaks weeks after install | Validate paths at startup and report `installed: false` in `app.list` (visible in the panel *before* someone needs it); explicit `launch_failed`/`app_not_installed` codes; paths live in one operator-owned file, so fixing is a one-line edit and a signal, not a redeploy |
| **Process detection false positives/negatives** | `Code.exe`/`bash.exe` are generic; helper processes come and go; a name match in another session isn't your app | Registry-declared `process_names` (never guessed); filter to the agent's session/user; optional image-path prefix check; `process_and_service` for service-backed apps; report counts and PIDs so the UI can show the truth rather than a boolean |
| **Login race conditions** | The agent starts before the shell/network/user profile is ready; the first launch fails or lands nowhere | Startup delay (5 s) plus readiness gate: don't send `hello` until the session query returns `ACTIVE`; the 5-minute repeating trigger recovers any failed early start; the unlock trigger covers resume-from-lock |
| **Multiple logged-in users / RDP + console** | "The" session is ambiguous; a launch could land on someone else's desktop | Config names the expected user; if another interactive session is present, report `session: MULTIPLE` and refuse GUI launches (`precondition_failed`) unless the configured user's session is the *active console* one. Refusing is right: launching onto the wrong desktop is worse than not launching |
| **Machine sleep / modern standby** | The socket stays "open" but nothing is delivered; state goes stale and lies | Heartbeat deadline (45 s) marks `OFFLINE` regardless of socket state; `confidence: stale` on any state older than 3 intervals; power-event hooks (`WM_POWERBROADCAST`) send `agent.shutting_down` best-effort before suspend |
| **WOL behavior** (disabled after driver update, "fast startup" hybrid shutdown, wrong NIC, WOL from S5 unsupported) | Wake silently does nothing and the workflow just times out unhelpfully | `desktop.wake` reports "magic packet sent" — never "desktop waking" (it cannot know); the workflow's `wait_for` produces a specific "WOL sent, host did not become reachable in 90 s — check NIC WOL settings and fast startup" message; document the NIC/BIOS/fast-startup prerequisites in the install runbook; retry WOL up to 3× (it is idempotent) |
| **Network interface changes** (VPN up, Wi-Fi↔Ethernet, new DHCP lease, Hyper-V vSwitch reshuffling adapters) | Butters' hostname-based probing breaks; the desktop appears offline while perfectly healthy | The **agent dials out**, so agent availability is immune to the desktop's address changing — this is a primary reason for the transport choice. The ICMP/SSH axes are best-effort hints only, and `agent = READY` alone is sufficient for `gui_launch`. `fallback_urls` covers Butters' own address changing (LAN → tailnet) |
| **Duplicate commands** (double-click, panel retry, workflow retry) | Two Parsec launches, two VMs, two builds | `idempotency_key` + replay LRU + `singleton` apps + convergent VM start; the UI disables a button while its job is in flight |
| **Command replay** (captured frame re-sent) | Unauthorized re-execution | TLS + HMAC envelope + `request_id` LRU + `issued_at` skew window + 30 s freshness for SESSION_CONTROL/DESTRUCTIVE + connection-scoped ID validity |
| **Reconnect storms / thundering agent** | A crash loop or a Butters restart produces a hot reconnect loop that buries logs and burns CPU | Exponential backoff with jitter, capped at 60 s; unauthorized/pin-mismatch uses the **longest** backoff (there is no point retrying fast on a config error); Butters rate-limits `hello` per `agent_id` and reports a crash loop as one `ERROR`, not fifty flaps |
| **Lost result frame** | Butters never learns the outcome of something that did happen | Agent caches terminal results by `idempotency_key` for 300 s and its local JSONL is authoritative for "did it happen"; Butters' retry with the same key returns the cached result instead of re-executing |
| **Registry file tampering** | The registries are the entire authorization boundary for what can launch | ACL them to administrators-write on `%ProgramData%`, refuse group/world-writable files (the same check `compute.py` already applies to its TOML), validate structurally at load, and log a hash of each registry at startup so a change is visible in the audit trail |
| **Secret sprawl** | Tokens copied into config, logs, or the repo | Config holds only *references*; DPAPI/Credential Manager on Windows; `0600 root` files on the Pi; sanitizer on every logged string; a CI grep for high-entropy strings in the repo and in log fixtures |

---

## Notes for Codex

Only what you need before implementing the Windows Desktop Agent. Nothing here asks you to change work already in flight.

**Seam to use (already exists).** `butters/src/butters/actions/compute.py` defines `DesktopAgent` Protocol + `UnavailableDesktopAgent`, and `DesktopActions.catalog()` publishes `desktop_agent: self.agent.status()`. Keep both. The real agent client implements that Protocol and is injected in place of `UnavailableDesktopAgent`; add `invoke(action, parameters)` and `snapshot()` to the Protocol, and make `run_registered(app)` a wrapper over `invoke("desktop.app.launch", {"app": app})` so existing callers are unaffected.

**Transport.** Agent-initiated persistent WebSocket over TLS, agent → Butters, at `wss://<butters>:8443/agent/v1/session`. The Windows side never listens. Butters keeps one live session per `agent_id`; a new authenticated connection supersedes the old. Frame types: `hello`, `welcome`, `request`, `ack`, `result`, `event`, `heartbeat`, `cancel`, `error`. `ack` always precedes `result`. Do **not** route desktop commands over MQTT — MQTT stays yours, for IoT.

**Do not launch GUI apps through sshd.** Session 0 isolation makes it either invisible or broken. SSH keeps compilation/git/tests/scripts/file-ops; the agent gets GUI, session state, app/VM state, telemetry.

**Keep the broker as the agent-independent path.** `BrokerOperation` members (`desktop.wake`, `desktop.lock`, `desktop.sleep`, `desktop.restart`, `desktop.shutdown`, `desktop.parsec_ensure`, monitors, environment) must keep working with the agent offline — they are how a cold desktop gets recovered. This design adds no broker changes and no `BrokerOperation` changes.

**Security minimums for a first cut.** (1) Agent pins Butters' TLS **SPKI SHA-256** and ignores the Windows trust store. (2) Agent presents a 256-bit token from DPAPI (user scope) / Credential Manager; Butters stores only a hash in `/etc/butters/agents.toml` (`root:butters 0640`). (3) Wire format includes a `sig` field from day one, `null` while `require_signatures = false`, HMAC-SHA256 over `request_id|action|target|issued_at|sha256(canonical_json(parameters))` when enabled. (4) `request_id` LRU (512 entries / 300 s) returning **cached results** for duplicates. (5) `issued_at` skew window ±90 s; 30 s freshness for SESSION_CONTROL/DESTRUCTIVE. No mutual TLS, no home CA, no SSH-derived identity for the agent.

**No arbitrary execution, ever.** No action accepts a command, path, argument list, or executable name. `parameters.app` / `parameters.vm` are **keys** into operator-owned TOML registries on the Windows box (`%ProgramData%\Butters\`, administrators-write ACL, refuse group/world-writable, validated at load). The agent never accepts config over the wire. Reuse the existing strictness pattern from `compute.py`: exact parameter key sets, identifiers matched against `^[a-z][a-z0-9_]{0,63}$`.

**Process model.** Scheduled Task: trigger at logon of the configured user + on unlock + a repeating 5-minute `IgnoreNew` tick (free supervision), `RunLevel=Limited`, `LogonType=InteractiveToken`, `RestartCount=3`. Never a Windows service. Never trigger UAC — elevated needs go to the broker or a pre-registered elevated task.

**State must not lie.** Model five independent axes (`power`, `network`, `os`, `session`, `agent`) and derive the composite plus capability predicates (`headless_compute`, `gui_launch`, `session_control`, `vm_control`, `streaming_ready`). Every state carries `observed_at`/`age_ms`/`confidence`. "Desktop ready" ≡ `agent READY` **and** `session ACTIVE` **and** heartbeat < 2 intervals. SSH answering means `WINDOWS_AVAILABLE` with `gui_launch: false` — it must never render as ready. Callers should branch on capability predicates, not on the composite enum.

**Naming rule.** `<domain>.<subject>[.<subsystem>].<verb>`, verb last from a closed set, and **new objects are registry entries, not new action names**: `desktop.app.launch{app}`, not `desktop.launch_parsec`. A new name is justified only by different semantics (e.g. `desktop.streaming.prepare`). Because parameters are always registry keys or enumerated values, they are safe to write into audit logs — preserve that property if you add parameters.

**Permissions map onto what exists.** `READ_ONLY → AuthenticationLevel.NONE`, `ROUTINE`/`SESSION_CONTROL → ELEVATED`, `DESTRUCTIVE`/`ADMIN → FRESH`. Permission is computed from `(action, parameters)` — `vm.stop{force}` is DESTRUCTIVE while `vm.stop{shutdown}` is SESSION_CONTROL. Voice never authorizes DESTRUCTIVE or ADMIN; `ActionCoordinator.freeze_plan` + digest-bound confirmation already provides the mechanism.

**Retry lives in Butters, not the agent.** The agent never retries on its own; it reports `retryable` and declares each action `IDEMPOTENT` / `IDEMPOTENT_CONVERGENT` / `NON_IDEMPOTENT`. Butters retries only idempotent + retryable failures, **reusing the original `idempotency_key`**.

**The agent queues nothing.** Commands arrive only over a live socket. While disconnected the agent stays running (fresh state on reconnect) and keeps its local JSONL audit, but holds no backlog. On reconnect, both sides treat it as a fresh session; Butters resolves old in-flight requests as `transport_error`.

**Workflows belong to Butters.** `desktop.streaming.prepare` and `compute.build_and_test` are declarative sequences of *existing registered actions* plus `wait_for`/`assert`/`branch` predicates. No transport code, and no reimplementation of `app.launch` or `compute.build`, inside a workflow.

**Versioning.** Three separate versions: `PROTOCOL_VERSION` (integer, negotiated in `hello`/`welcome`, Butters supports N and N−1), `ACTION_SCHEMA_VERSION` (agent advertises the action names it supports; Butters disables the rest rather than failing at call time), `AGENT_VERSION` (semver, informational). Additive changes only; never repurpose an action name, parameter meaning, or error code. Config files carry `schema_version`.

**Isolate Windows.** All pywin32/ctypes/WMI calls behind `platform/win32.py` with a `platform/fake.py` twin selected by `BUTTERS_AGENT_PLATFORM=fake`, so the whole agent is testable in CI on the Pi. Ship it as a separate `butters-agent/` deployable, not inside `butters/src/`. Target 2–3k lines: no plugin loader, no DI container, no ORM.

**Audit.** SQLite on the Pi via the existing `actions/store.py` audit path, mirrored to JSONL; agent keeps its own rotating JSONL. Record `source` (`manual_ui | voice | automation | workflow | internal | cli`), identity, target, transport, action, parameters, permission, authentication, confirmed, accepted, outcome, error code, timestamps, duration, attempt, duplicate, agent/protocol version. Never log tokens, keys, or raw output; everything goes through `diagnostics/sanitizer.py`.

**Heartbeat.** Every 15 s, ~350 bytes: session state/locked/idle, CPU/mem/GPU, subsystem health, counters, uptime, agent version, `seq`. Username as a salted hash. Per-app and per-VM state are **on demand plus push-on-change**, never in the heartbeat. Butters marks `OFFLINE` on a 45 s heartbeat timeout even if the socket looks open.
