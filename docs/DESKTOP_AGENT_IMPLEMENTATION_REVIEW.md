# Desktop Agent Implementation Review

**Reviewed:** `docs/DESKTOP_AGENT_DESIGN.md` against the confirmed foundation state of 2026-09-07
**Role:** design/review only. No code changed; no Codex-owned file touched.
**Evidence base:** `butters/DESKTOP_COMPUTE.md`, `butters/config/assistant.toml`, `butters/config/desktop-compute.toml`, `butters/src/butters/actions/{broker,compute,coordinator,store}.py`, `butters/src/butters/web/{app,service}.py`, `butters/windows/`.

The design's core shape survives the foundation work intact: agent-initiated connection, registry-only launching, SSH for headless, broker for agent-independent power/session control, one Action API for UI and voice. Three things in it are now **factually stale**, and one of them is load-bearing for Phase 1.

---

## Critical implementation risks

**R1 — The design's transport endpoint does not exist and cannot exist as described.** The design says "reuses the existing Butters web service" at `wss://butters.lan:8443`. In fact the web daemon is loopback-only and plain HTTP (`config/assistant.toml`: `[web] host = "127.0.0.1"`, `port = 8090`), with TLS supplied exclusively by private Tailscale Serve (`origin = "https://sensor-pi.tail9644cc.ts.net"`), and the desktop is not on Tailscale. Every existing WebSocket route (`web/app.py:1167`, `:1538`) gates on `require_origin` + session cookie + CSRF + `Tailscale-User-Login` — browser-shaped checks an agent cannot satisfy. So there is **no listener the agent can reach**, and adding an agent route to the existing app means either weakening those gates or growing a second auth system inside the same ASGI app. This is the single largest gap between design and reality; see D1 for the recommended resolution.

**R2 — Duplicate Parsec control paths.** `desktop.parsec_status/ensure/restart`, `lock`, `sleep`, `restart`, `shutdown` are gated **off** at both layers today (`config/assistant.toml:29-35`, `config/action-broker.example.toml:32-36`). If Codex finds the broker path disabled, the path of least resistance is to implement Parsec launching inside the agent — producing two implementations with divergent semantics, two audit shapes, and a `streaming_ready` predicate that disagrees with `desktop.parsec_status`. The agent must not gain its own Parsec/service-control logic; Parsec service state stays with the broker helper, and the agent contributes only *user-session* facts (is `parsecd.exe` present in this session, is the session unlocked).

**R3 — Two host identities for one desktop.** The broker targets `192.168.1.209` (`assistant.toml:17` region, `[desktop] host`), while compute targets hostname `DESKTOP-G4CFVL1` (`config/desktop-compute.toml`), with `HostKeyAlias 192.168.1.209` in the operator config. The state model in the design assumes one target identity per machine. After a DHCP change the broker axis fails while the SSH axis succeeds, and `desktop.status` will report a self-contradictory composite. Fix the address (DHCP reservation) and/or make each axis report which identity it was observed through.

**R4 — The `DefaultShell` change may have silently broken the broker's Windows helper, and the unit tests cannot detect it.** `broker.py:579` sends one command string over SSH:
`powershell.exe … -File C:\ProgramData\Butters\desktop-control.ps1 -Operation <Op>`.
That string is now delivered to `bash.exe -c` (`DefaultShell` = `C:\Program Files\Git\bin\bash.exe`, `DefaultShellCommandOption` = `-c`, `DefaultShellEscapeArguments` **absent** — `DESKTOP_COMPUTE.md`). Whether the backslashes survive depends on sshd's argument-escaping behavior for non-cmd shells, which varies by Win32-OpenSSH version; if they do not, bash collapses `\P\B\d` and PowerShell receives `C:ProgramDataButtersdesktop-control.ps1`, i.e. every broker desktop-control operation fails as `operation_failed`. The tests at `tests/test_action_broker_v2.py:467`, `tests/test_broker_privilege_boundary.py:687`, `tests/test_desktop_remote_management_v2.py:241` assert the argv string against a mocked runner, so they pass either way. The reported validation covered `compute.py` actions and SSH Test, not the broker helper. **Re-validate live before trusting any workflow step that calls it.**

**R5 — The agent runs as an administrator account.** The service SSH key was added to `administrators_authorized_keys` for user `Daniel` with `restrict` but **no forced command** (`DESKTOP_COMPUTE.md`), so the SSH boundary is already arbitrary-execution-capable as an admin; "no arbitrary shell" is enforced only in Butters' Python layer. The agent will run in that same account's session. An agent token compromise therefore equals admin code execution *unless* the registries are genuinely the only path to `CreateProcess`. This raises the stakes on registry ACLs (see W7) from hygiene to primary control.

---

## Design assumptions to re-check

**D1 — Transport landing place (blocking).** Pick one before writing transport code:
- **(a) Separate LAN-bound listener, own systemd unit, own self-signed cert, agent SPKI-pins it; it talks to the web daemon over a Unix socket** — mirrors the existing broker pattern exactly, keeps the web daemon loopback-only, keeps browser auth untouched. Recommended if the desktop stays off Tailscale.
- **(b) Put the desktop on Tailscale and use Tailscale Serve.** Deletes the entire TLS/pinning/token-provisioning layer, reuses the identity boundary the system already trusts, and the agent still dials outbound. Materially less code and less new attack surface; the cost is one Tailscale install on Windows. Worth reconsidering now that transport is the phase-1 critical path — the design assumed "not on Tailscale" as fixed, and it is not.
- **(c) Agent route inside the existing app on a second bound port.** Not recommended: it puts non-browser auth inside the app that currently guarantees origin+cookie+CSRF on every socket.

**D2 — Streaming workflow depends on disabled gates.** `desktop.streaming.prepare` cannot be delivered until `parsec_*`/`lock`/`sleep` are enabled in both `assistant.toml` and `/etc/butters/action-broker.toml`. Treat gate-enablement + live validation as an explicit prerequisite step in the roadmap, not an afterthought.

**D3 — "SSH remains hostname-based" vs. the broker's literal IP.** Stated in the foundation summary but not true of the broker's config. Reconcile (R3).

**D4 — `os = AVAILABLE` means "sshd authenticates".** Still correct, and now cheap to assert honestly: `compute.py:_status` reports `ssh_authenticated: None` for `desktop.status` and only `desktop.ssh_test` proves authentication. Keep that distinction in the axes; do not let a TCP banner promote the axis to AVAILABLE.

**D5 — Bash is now the SSH shell.** `compute.py`'s `(cd -- … && ( … ))` composition now matches the actual shell, which is good. But any future SSH command that embeds a Windows path must be single-quoted or POSIX-form (`/c/...`); the previous cmd.exe assumption is dead everywhere, including in docs and examples.

**D6 — No compute project is registered and `OPENAI_API_KEY` is unset.** Both consistent with the design (LLM optional, projects operator-registered). No change needed; just don't let agent work assume either.

---

## Windows-specific concerns

**W1 — Session 0.** Non-negotiable: Scheduled Task with `LogonType=InteractiveToken`, `RunLevel=Limited`, never a service. Verification that actually catches a regression: after `app.launch`, assert the new PID's session id equals the agent's own session id. A launch that "succeeded" into session 0 must report failure, not success.

**W2 — Scheduled Task behavior.** Use logon + unlock triggers plus a repeating 5-minute tick with `MultipleInstances=IgnoreNew` (free supervision). Watch three specifics: `DisallowStartIfOnBatteries` defaults **true** and will silently prevent starts; `StartWhenAvailable` is needed for missed triggers after sleep; and `ExecutionTimeLimit` defaults to 3 days, which will kill a long-lived agent — set `PT0S`. Note the existing installer only creates `\Butters\LockDesktop` and `\Butters\SleepDesktop` (`windows/README.md`); the agent task must be additive and separately rollback-documented in the same style.

**W3 — Login race.** Do not send `hello` until the session query returns ACTIVE and the profile is loaded; a 5 s delay plus a readiness gate is enough. Reporting AGENT_READY during logon-shell initialization is how a first launch lands nowhere.

**W4 — Logout/logoff.** The process dies with the session; that is correct behavior, not a bug to work around. Send `agent.shutting_down` best-effort on `WM_QUERYENDSESSION`/console-ctrl so Butters flips to OFFLINE immediately rather than waiting out the heartbeat.

**W5 — UAC.** The agent must never trigger a consent prompt: it appears on the secure desktop and hangs until timeout, which looks exactly like a hung action. Hyper-V is the trap here (`Hyper-V Administrators` membership, or an elevated pre-registered task). Any action that would need elevation returns `precondition_failed` naming the missing install step.

**W6 — Multiple sessions / RDP.** With Parsec in play, a second interactive session is a realistic everyday state, not an edge case. Report `session: MULTIPLE` and **refuse** GUI launches unless the configured user owns the active console session. Refusing beats launching onto the wrong desktop.

**W7 — Registry file ACLs (raised to primary control by R5).** Do not rely on `%ProgramData%` inheritance — `New-Item` under `C:\ProgramData` can leave ACEs that permit non-admin writes depending on configuration, and `install-desktop-control.ps1` already stores an executable script there that the broker runs as `Daniel`. Set ACLs explicitly with `icacls` (Administrators+SYSTEM full, agent user read-only, remove inherited write for Users), refuse to load a registry file that is writable by non-administrators (mirroring `compute.py`'s existing group/world-writable check), and log a hash of each registry at startup so edits are visible in the audit trail.

**W8 — Process detection.** Registry-declared image names only; filter to the agent's own session/user; optionally verify the image path prefix. Parsec specifically needs `process_and_service` — the existing helper already distinguishes `system_host_process_present` from `user_host_process_present` (`broker.py:_PARSEC_STATE_KEYS`), and the agent should mirror that distinction rather than collapsing it to a boolean.

**W9 — DPAPI scope.** `CryptProtectData` **without** `CRYPTPROTECT_LOCAL_MACHINE` (user scope) is required. Machine scope would let any local process decrypt the token. Consequences to design for: the blob is undecryptable after a password reset without recovery, it does not roam, and it must be provisioned in the agent user's own context — an elevated installer running as Administrator cannot create a blob that `Daniel` can read. Provision it as `Daniel`, and have the agent report `ERROR` with a specific `credential_unavailable` reason rather than looping on decrypt failure.

---

## Transport/security concerns

**T1 — Reconnect.** Exponential backoff with jitter, capped 60 s, forever; use the **longest** backoff for `unauthorized` and pin-mismatch, since retrying fast on a config error only buries logs. Butters should rate-limit `hello` per `agent_id` and collapse a crash loop into one `ERROR`, not fifty state flaps.

**T2 — Half-open connections.** The default failure after sleep or a Wi-Fi change is a socket that looks open and delivers nothing. The 45 s heartbeat deadline must be authoritative over socket state — if Codex trusts the socket, the panel will confidently show AGENT_READY for a desktop that is asleep.

**T3 — Duplicate requests.** `request_id` LRU returning **cached terminal results** (not re-execution, not a bare rejection) is what makes workflow retry safe after a lost result frame. Same `request_id` + different parameters must be `duplicate_request`, never honored.

**T4 — Stale commands.** Three independent guards, all required: ±90 s skew window; 30 s freshness for SESSION_CONTROL/DESTRUCTIVE; rejection of IDs from a superseded connection. Also log the offset from `welcome.server_time` and report `DEGRADED` past 30 s — a bad clock silently disables both replay protection and freshness.

**T5 — TLS/SPKI pinning.** Pin the SPKI hash, not the certificate, and ignore the Windows trust store entirely. Fail **closed** and do not transmit the token on mismatch. If option (b) in D1 is chosen, this whole item disappears — another argument for it.

**T6 — HMAC/replay.** Ship the `sig` field from day one even with `require_signatures = false`; enabling it later must be a config flip, not a protocol break. Canonical form and JSON canonicalization must be byte-identical on both sides — put the canonicalizer in one shared implementation with shared test fixtures, or it will drift.

**T7 — Credential storage and scope.** Token and command key are separate values, referenced by path in config, never inline. On Butters: hashes only, `root:butters 0640`, secrets under `/etc/butters/secrets/` at `0600 root`. The token authenticates *one agent to one endpoint* and must grant nothing else — in particular it must not be accepted by any browser route or the broker socket.

**T8 — Do not build a second permission or audit system.** Authorization maps onto the existing `AuthenticationLevel` (NONE/ELEVATED/FRESH) and `ActionCoordinator.freeze_plan`, and audits go through the existing `store.audit(...)`. Permission must be computed from `(action, parameters)` — `vm.stop{force}` is DESTRUCTIVE while `vm.stop{shutdown}` is SESSION_CONTROL. Registry `permission` fields may only raise the floor, never lower it. Note the passkey model already in place (single active credential, `elevation_seconds = 600`): FRESH actions will require a real passkey step-up, so confirm that flow works before wiring DESTRUCTIVE agent actions to it.

**T9 — Audit content.** Parameters are safe to log *only because* the namespace keeps them to registry keys and enumerated values. If any free-text parameter is ever added, it must be declared sensitive and redacted. Everything still passes through `diagnostics/sanitizer.py`. Never log the token, its prefix, or the command key.

---

## State-model concerns

**S1 — Five axes, capability predicates, staleness stamps.** Callers branch on `capabilities.*`, not on the composite enum. This is the design's most important correctness property and the easiest to quietly drop under time pressure.

**S2 — SSH available must never imply GUI ready.** With no auto-login, `WINDOWS_AVAILABLE` + `gui_launch: false` is the correct steady state after a reboot, possibly for hours. The panel must render that as a disabled Launch button *with the reason*, not as an error and not as ready.

**S3 — `streaming_ready` must be conjunctive and honest:** `agent READY` **and** `session ACTIVE` (not LOCKED) **and** Parsec healthy per the broker's `plausibly_ready`-style state **and** heartbeat < 2 intervals. Do not let a running Parsec *service* alone satisfy it — the existing helper already exposes exactly the fields needed to avoid that mistake.

**S4 — Axis provenance.** Given R3, each axis should record which host identity and mechanism produced it. A composite state assembled from two different addresses is worse than an unknown.

**S5 — Every state carries `observed_at`/`age_ms`/`confidence`.** Anything older than 3 heartbeat intervals reports `stale`, never as fact.

---

## Parsec/VM concerns

**P1 — Ownership split.** Broker owns the Parsec *service* (install/startup/service state, and the lock/sleep/restart operations that must work with no agent). Agent owns *session* facts. `desktop.streaming.prepare` composes both; neither reimplements the other (this is R2).

**P2 — Gates first.** Enable and live-validate the `parsec_*` gates at both layers, after resolving R4, before the workflow is written.

**P3 — Workflow honesty.** `desktop.wake` reports "magic packet sent", never "waking". The `wait_for` failure message must name the actual suspect ("WOL sent; host unreachable after 90 s — check NIC WOL and Fast Startup"), and Fast Startup / hybrid shutdown belongs in the runbook prerequisites. Note the recorded recovery procedure already uses MAC-targeted discovery rather than assuming `.209` — keep that property in the workflow.

**P4 — VM phase discipline.** Implement the `VmBackend` protocol plus exactly **one** backend — whichever hypervisor is actually installed. `available()` returning `(False, reason)` must degrade the agent to `DEGRADED` and leave every other action working. Never auto-escalate a graceful stop to force; `allow_force_stop` is per-VM opt-in and DESTRUCTIVE. `vm.start` is convergent (already-running is success; `STARTING` waits rather than issuing a second start). If Hyper-V is the backend, W5 applies before any code is written.

---

## Recovery/idempotency concerns

| Event | Required observable behavior |
|---|---|
| Desktop reboot, no login | `WINDOWS_AVAILABLE`, `gui_launch: false`, `headless_compute: true`; GUI actions → `session_inactive`. Never "ready". |
| Desktop reboot with login | Axes promote in order; AGENT_READY only after the session gate (W3). |
| Butters reboot | Agent reconnects on its own; jobs left `running` are marked `interrupted` at startup — no job stays `running` forever. |
| Agent crash | Restarted by the task within ≤ 5 min; crash breadcrumb in local JSONL; > 5 reconnects/10 min reports one `ERROR`, not flapping. |
| Network drop | Backoff redial; old in-flight requests resolved `transport_error`; retry only if idempotent, reusing the original `idempotency_key`. |
| Logout / login | OFFLINE + `session NONE` with headless still true; agent returns on the logon trigger. |
| Butters unavailable | Agent holds **no** queue; it keeps running and keeps its local audit only. |

**I1 — Retry lives in Butters.** The agent reports `retryable` and an idempotency class; it never retries itself. Butters retries only idempotent + retryable failures, reusing the original key.

**I2 — Idempotency must be verified by observation, not asserted.** The test that matters is "exactly one process/VM resulted", not "the second call returned success".

**I3 — Concurrent workflows.** A second `streaming.prepare` for the same target returns `busy`. Note `compute.py` already single-flights desktop actions with a non-blocking lock; the agent needs the same property per app/VM.

---

## Manual Tools-panel integration review

- The panel must render Applications and VMs **from** `desktop.app.list` / `desktop.vm.list`, so a TOML edit adds a row with no UI change. A hard-coded Parsec/VS Code/Git Bash button list is the most likely shortcut and it defeats the whole registry design.
- Surface the agent block: version, protocol, connected-since, last-heartbeat age, reconnect count. Show `installed: false` and `invalid: true` registry entries **before** someone needs them.
- `DesktopActions.catalog()` already publishes `desktop_agent` (`compute.py`), and `web/service.py:162` constructs it — the panel gains agent state with no contract change. Keep that seam.
- Disable rather than hide unavailable actions, always with the reason. A greyed button teaches the state model; a missing one looks like a bug.
- Danger treatment by permission category, not by feel: DESTRUCTIVE collapsed into a separate "Power & destructive" section, never adjacent to routine buttons, typed target confirmation, brief enable delay. ADMIN not in the panel at all.
- Compute controls must stay usable while the agent is OFFLINE, with a note saying why that is expected.
- The panel calls the Action API directly and must work with no cloud model configured — currently the real state (`OPENAI_API_KEY` unset), so this is testable today.

---

## Explicitly out of scope for this phase

Cloud/LLM planning (the local router and the panel must carry the phase alone); a second hypervisor backend; VM snapshot/checkpoint operations; NAS/Minecraft/environment namespace consolidation; audit-query UI and retention job; multi-user or multi-desktop support; config hot-reload beyond an explicit signal; any durable command queue on the agent; any agent-side retry or orchestration; file transfer; screenshotting or input injection; and any change to ESP32 firmware, ESP-NOW/MQTT formats, or node provisioning.

---

## Codex verification checklist

Transport/auth
- [ ] D1 decided and written down; if (a), the agent listener is a separate unit and the web daemon stays loopback-only
- [ ] Wrong SPKI pin → token never sent, longest backoff, clear log
- [ ] Wrong token → `unauthorized`, no hot reconnect loop
- [ ] Second connection with same `agent_id` → old closed `superseded`, in-flight resolved `transport_error`
- [ ] Protocol version negotiated in `hello`/`welcome`; N and N−1 accepted
- [ ] Half-open socket (silently dropped packets) → OFFLINE within 45 s
- [ ] Clock skewed 5 min → `DEGRADED` + fail closed on signed requests
- [ ] Malformed/oversized frame → agent survives, nothing executes
- [ ] Agent's garbage `result` → job `failed`, never reported success

Windows
- [ ] Launched PID's session id == agent's session id (W1)
- [ ] Task survives logout/login/reboot/battery/sleep; `ExecutionTimeLimit=PT0S`, `DisallowStartIfOnBatteries=false`
- [ ] `hello` withheld until session ACTIVE (W3)
- [ ] No code path can raise a UAC prompt (W5)
- [ ] Second interactive session → `MULTIPLE`, GUI launches refused (W6)
- [ ] `icacls` output for `C:\ProgramData\Butters` recorded; non-admin-writable registry refused; registry hashes logged (W7)
- [ ] DPAPI blob is user-scoped and provisioned as the agent user; failure → `credential_unavailable`, not a loop (W9)

Actions/registries
- [ ] No action accepts a command, path, argv, or executable name
- [ ] `unknown_app` / `app_not_installed` / `launch_failed` / `timeout` are distinguishable
- [ ] Malformed `apps.toml` entry → that app `invalid`, agent still starts
- [ ] Singleton relaunch → `already_running`, one process observed
- [ ] `app.stop` refused when `stoppable = false`
- [ ] Duplicate `request_id` → cached result, exactly one side effect; different params → `duplicate_request`
- [ ] Stale DESTRUCTIVE request (45 s) → `stale_request`
- [ ] `wrong_target` rejected
- [ ] Permission derived from `(action, parameters)`; FRESH path exercised against the live passkey step-up
- [ ] Audit rows contain source/identity/target/transport/permission/outcome and **no** secrets (grep the fixtures)

State/workflow
- [ ] Reboot-without-login renders as `WINDOWS_AVAILABLE` + disabled Launch with reason
- [ ] `streaming_ready` false when session LOCKED even with Parsec service running
- [ ] Every state carries `observed_at`/`age_ms`/`confidence`
- [ ] `streaming.prepare` idempotent fast path when already ready (< 5 s, no relaunch)
- [ ] Partial failure reports per-step status and never overall success
- [ ] Cancellation lists steps that had already committed side effects
- [ ] Second concurrent workflow for one target → `busy`

Foundation re-validation (do first)
- [ ] R4: live `desktop.parsec_status`, `desktop.lock`, `desktop.sleep` through the broker after the `DefaultShell` change
- [ ] R3: single host identity, or per-axis provenance
- [ ] D2: gates enabled at both layers and validated

---

## Stop-Ship Issues

1. **R4 — broker desktop-control over the new Bash default shell is unvalidated.** `broker.py:579` passes `C:\ProgramData\Butters\desktop-control.ps1` through `bash -c` with `DefaultShellEscapeArguments` absent, and the existing tests mock the runner so they cannot detect breakage. If this path is broken, WOL→lock/sleep/Parsec — the entire agent-independent recovery layer, and the reason the design tolerates an agent that dies with the session — is broken. Must be exercised live before the Desktop Agent is called complete.

2. **R1/D1 — no reachable, authenticated endpoint for the agent.** The web daemon is loopback-only HTTP behind tailnet-only TLS. Shipping an agent that "connects" via a route bolted onto that app, or via any listener that relaxes the existing origin/session/CSRF guarantees, is not acceptable. Decide (a) separate LAN listener + unit + pinned cert, or (b) desktop on Tailscale, and implement it deliberately.

3. **W1 — a GUI launch that lands outside the interactive session must fail, not succeed.** If `app.launch` reports success without verifying the new process is in the agent's own session, every state and workflow above it is untrustworthy, and the failure mode is invisible (no window, no error).

4. **R2 — no second Parsec (or session-control) implementation in the agent.** Two divergent Parsec paths with two audit shapes and a `streaming_ready` that disagrees with `desktop.parsec_status` is a defect that gets much more expensive after the workflow layer is built on top of it.

5. **R5/W7 — registry files must be provably non-admin-unwritable, and refused if they are not.** The agent runs in an administrator's session and the registries are the only thing standing between a token and arbitrary execution. Explicit `icacls`, a load-time writability check, and startup registry hashes in the audit log — not inherited `%ProgramData%` permissions.

6. **T8 — no parallel permission or audit system.** Agent actions authorize through the existing `AuthenticationLevel`/`ActionCoordinator` and audit through the existing store. A second scheme would fork the security model of the whole orchestrator, and voice control lands on it next.
