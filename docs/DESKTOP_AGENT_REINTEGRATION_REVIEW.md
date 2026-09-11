# Desktop Agent Reintegration Review

Status: review only. No historical code is reintegrated by this document.

Reviewed against `origin/main` at `bcaab83` ("Merge conversational planner
foundation") on 2026-09-11. The historical lineage reviewed is
`origin/feature/desktop-remote-management-v2` at `c4ffe0f`.

This document answers one question: **what is the smallest safe way to restore
the interactive Windows Desktop Agent on top of the architecture `main` has
today**, given that `main` evolved independently for forty commits after the
lineage diverged.

---

## 1. Repository topology

### 1.1 Refs

| Ref | Head | Relationship to `main` |
| --- | --- | --- |
| `origin/main` | `bcaab83` | authoritative |
| `origin/feature/desktop-remote-management-v2` | `c4ffe0f` | merge base `4925c64`; `main` +40, branch +7 |
| `origin/feature/desktop-remote-management-v2-continuation` | `826fc9a` | **already an ancestor of `main`** (0 ahead) |
| `origin/feature/butters-remote-interaction-completion` | `5459942` | **already an ancestor of `main`** (0 ahead) |
| `origin/diagnostic/windows-sleep-stability` | `a6c3cc1` | based on `826fc9a`; +6 unreviewed diagnostic commits |

The fetch could not be refreshed in this session (`git fetch` failed with
`Permission denied (publickey)` — the ssh-agent socket is not exported here), so
the audit was performed against the local `origin/*` refs. `main` and
`origin/main` are identical at `bcaab83`, and the divergence arithmetic above is
self-consistent, so the conclusions are not sensitive to a stale fetch. Re-run
`git fetch --all` with the agent socket before acting on the slice plan.

### 1.2 The important correction

`main` already contains `292d9f6 "Merge Desktop Remote Management v2 into current
main"` and `826fc9a "Fix on-demand Parsec remote orchestration"`. **A large part
of the desktop-remote-management programme is already in production.** What is
missing is specifically the *interactive agent* half.

The genuinely unmerged lineage is exactly seven commits:

| Commit | Subject | Files | Character |
| --- | --- | --- | --- |
| `1f59d48` | Add interactive Windows desktop agent | 50 (+4984) | Feature, largely self-contained, but entangled with the UI-era `service.py`/`app.py` |
| `1aa02f0` | Make Butters deployments verifiable and complete | 6 (+340) | **Clean, agent-independent** |
| `36c2010` | Stabilize Admin authentication, Tools visibility, desktop state | 5 (+595/-87) | Tightly coupled: half `AgentHub` hardening, half Admin JS |
| `484d061` | Admin stabilization regression tests + operations guide | 4 (+855) | Tests/doc for `36c2010` |
| `9adcbf4` | Expose the existing wake action in Admin → Tools | 6 (+706) | Pure UI-over-existing-capability |
| `5c66593` | Admin → Tools responsiveness + shutdown | 8 (+1720) | Pure UI, plus an `assistant.toml` shutdown gate |
| `c4ffe0f` | Document Shutdown Desktop and its two gates | 1 (+103) | Doc only |

**Commit boundaries are not reintegration boundaries.** `36c2010` in particular
must be split: its `actions/agent.py` hunks are load-bearing security fixes (the
`last_seen = None` sentinel, the named `state()` machine) that belong with the
agent core, while its 513 lines of `admin.js` belong with the UI slice.

### 1.3 Files unique to the historical lineage

Present on `c4ffe0f`, absent from `main`:

```
butters-agent/                                  (18 files, entire package)
butters/src/butters/actions/agent.py            AgentHub
butters/src/butters/actions/agent_ingress.py    TLS terminator
butters/src/butters/actions/compute.py          SSH catalog + agent adapter
butters/src/butters/actions/streaming.py        Wake→Parsec composition
butters/src/butters/skills/desktop_agent.py     skill registration
butters/src/butters/deployment.py               tree-digest deployment verifier
butters/config/agent-ingress.toml
butters/config/butters-agent-ingress.service
butters/config/desktop-compute.toml
butters/config/desktop-ssh.example.conf
butters/scripts/deploy-desktop-agent.py
butters/scripts/update-desktop-mdns.py
butters/scripts/verify-desktop-ssh.py
butters/scripts/verify-deployment
butters/windows/configure-git-bash-ssh.ps1
butters/ADMIN_OPERATIONS.md
butters/DESKTOP_COMPUTE.md
docs/DESKTOP_AGENT_DESIGN.md                    (1142 lines)
docs/DESKTOP_AGENT_IMPLEMENTATION_REVIEW.md
docs/DESKTOP_AGENT_REMOTE_VALIDATION.md
butters/tests/test_desktop_agent.py             (19 tests)
butters/tests/test_desktop_compute.py
butters/tests/test_admin_stabilization.py
butters/tests/test_admin_wake_control.py
butters/tests/test_admin_shutdown_control.py
butters/tests/test_admin_tools_feedback.py
```

### 1.4 Files the lineage touched that `main` has since changed

This is the rewrite surface.

| File | Base → `main` | Consequence |
| --- | --- | --- |
| `butters/src/butters/web/service.py` | 2235 → 2561 lines | Historical `execute_desktop_action` **cannot be cherry-picked**; rewrite |
| `butters/src/butters/web/app.py` | 1913 → 2010 lines | Route block moved; re-apply by hand, small |
| `butters/src/butters/actions/broker.py` | changed substantially | The one-line PowerShell quoting fix must be re-derived |
| `butters/config/assistant.toml` | changed | Gate additions must be re-derived, not merged |
| `butters/scripts/install-beta1` | changed | Installer hunks must be rewritten |
| `butters/tests/test_beta1_installer_permissions.py` | changed | Ditto |
| `butters/windows/README.md` | changed | Trivial |

### 1.5 Files the lineage touched that `main` did **not** change

`main` is byte-identical to the merge base `4925c64` for all three Admin UI
assets:

- `butters/src/butters/web/static/assets/admin.js` (136 lines, unchanged)
- `butters/src/butters/web/static/admin.html` (103 lines, unchanged)
- `butters/src/butters/web/static/assets/styles.css` (unchanged)
- `butters/windows/desktop-control.ps1` (unchanged)

This is a useful asymmetry: the ~1170-line Admin UI grown by `36c2010`,
`9adcbf4` and `5c66593` would apply textually without conflict. Its *backend
contracts* would not — every endpoint it calls lives in the heavily-rewritten
`service.py`. Textual cleanliness here is a trap, not a green light.

---

## 2. Component inventory

### A. Desktop Agent protocol and core — `butters-agent/`

| Module | Lines | Assessment |
| --- | --- | --- |
| `protocol.py` | 152 | **Reuse unchanged.** HMAC-SHA256 over canonical JSON, ±90 s freshness, connection-id binding, closed `SCHEMAS` map, UUIDv4 request/idempotency ids, bounded `ReplayCache` with fingerprint conflict detection. No Butters imports. |
| `engine.py` | 135 | **Reuse unchanged.** Registry-only dispatch; transport supplies a *name*, never a path. Single-flight lock, `require_visible` settling, cancel-aware. |
| `client.py` | 161 | **Reuse unchanged.** Outbound-only WSS, SPKI pin verified *before* the token is sent, exponential backoff with a 60 s penalty for auth failures, never formats exceptions into logs. |
| `platform/win32.py` | 162 | **Reuse unchanged.** `WTSGetActiveConsoleSessionId` session facts, `shell=False` launch from the registry path, session-id verification after launch, DPAPI credential storage, ACL validation of the registry file. |
| `platform/fake.py` | 28 | **Reuse unchanged.** Makes the whole package testable on the Pi. |
| `provision.py`, `selftest.py`, `__main__.py` | 128 | **Adapt.** Paths and service naming need review against current deployment. |
| `install-task.ps1`, `prepare-install.py` | 135 | **Adapt.** Windows-side, needs a real host to validate. |
| `apps.example.toml`, `config.example.toml`, `vms.example.toml` | 16 | **Reuse; `vms.example.toml` drop.** |

The package has exactly two third-party dependencies (`websockets`,
`cryptography`) and **zero imports from `butters`**. Direction of dependency is
correct: Butters imports `butters_agent.protocol`, never the reverse.

### B. Butters agent ingress

| Component | Assessment |
| --- | --- |
| `actions/agent.py` (`AgentHub`, 228 + 52 lines) | **Adapt — small.** Imports only `butters.diagnostics.sanitizer.sanitize_value`, which still exists on `main` with a compatible signature. The `36c2010` hardening must be folded in as the starting state, not applied later. |
| `actions/agent_ingress.py` (118 lines) | **Reuse, one constant to re-check.** Standalone `asyncio` process; hardcodes upstream `127.0.0.1:8090`, which still matches `assistant.toml` `[web] host/port`. Binds only private IPv4s on named adapters, refuses wildcard/public, forwards exactly one path, strips every identity header. |
| `config/butters-agent-ingress.service` | **Adapt.** Re-derive hardening against `main`'s current unit conventions in `butters/systemd`. |
| `config/agent-ingress.toml` | **Reuse.** |
| `WebSocketRoute("/agent/v1/session", runtime.desktop_agent.socket)` | **Rewrite in place** — one line, but against `main`'s current route block. |

### C. Desktop action abstraction

| Component | `main` equivalent | Assessment |
| --- | --- | --- |
| `skills/desktop_agent.py` | none | **Adapt.** The registration loop is right, but it must be reconciled with `main`'s far richer `SkillSpec` (see §4.2). |
| `actions/compute.py` `DesktopActions` (456 lines) | partly `integrations/desktop.py` | **Split.** The named-command SSH catalog is useful and has no equivalent; the `DesktopAgent` Protocol/`UnavailableDesktopAgent` pair is useful; `catalog()`'s UI shape is stale. |
| `actions/streaming.py` `StreamingWorkflow` | partly `DesktopWorkflow.start_remote_session` | **Defer, then rewrite.** Explicitly out of scope per the task brief. |
| VM abstraction (`VmBackend`, `UnavailableVmBackend`, `desktop.vm.*`) | none | **Drop.** Four protocol actions that permanently return `vm_unavailable`. Dead weight that widens the protocol surface and drags a `FRESH`/confirmation path along for `desktop.vm.stop`. |
| `vms.example.toml` | none | **Drop.** |

### D. SSH / headless compute

`main` already has: `BrokerDesktopOperations.ssh_ready`, host-key pinning to
`<key directory>/known_hosts` with a start-time refusal when an SSH operation is
enabled without it, and `FixedBrokerOperations` invoking a fixed
`desktop-control.ps1` over SSH.

Historical `compute.py` adds something `main` genuinely lacks: an
**operator-registered named-command catalog** (`[commands]`, `[projects]`) where
the caller supplies a `[a-z][a-z0-9_]{0,63}` key and the shell text is trusted
operator configuration in a root-owned file. That is the correct shape, and it
is the only way "run the build on the desktop" becomes a deterministic
registered action rather than a command string.

It is, however, **a second SSH implementation** alongside the broker's. Restoring
it as-is would give Butters two SSH paths with two host-key policies. **Verdict:
adapt, and only after the agent core lands** — route the catalog through the
existing broker rather than through `compute.py`'s own `_capture`.

### E. Broker / WOL

`main` is ahead here. `BrokerOperation` already enumerates `desktop.wake`,
`desktop.monitors_off/on`, `desktop.parsec_status/ensure/restart`,
`desktop.lock/sleep/restart/shutdown`, `nas.wake`, the four environment
operations and three host operations, each behind a default-`false`
broker-local gate in `action-broker.example.toml`, on top of the
`assistant.toml` gate.

**Everything the historical lineage did to WOL is redundant.** Two residues:

1. `1f59d48` changed the broker's PowerShell invocation from
   `-File C:\ProgramData\...` to `-File "C:/ProgramData/..."`. `main`'s
   `broker.py` has diverged; whether the quoting defect still exists must be
   re-checked against `main` directly rather than assumed.
2. `1f59d48` changed `action-broker.example.toml` `host` from the IP to
   `DESKTOP-G4CFVL1.local`. **Do not adopt.** `main` pins `192.168.1.209`, and
   mDNS resolution is a weaker identity binding than a pinned address plus a
   pinned host key.

### F. Admin Tools UI

`main`'s Admin page already has a **Tools** tab, but it is a read-only
`#tool-list` fed by the skills catalog. The historical work turned it into a
control surface with wake and shutdown buttons, progress feedback and a
two-gate shutdown confirmation.

**Assessment: separate from the agent core entirely, and last in the order.**
The UI slice's value depends on backend capability existing first, and its 1170
lines of `admin.js` target `service.py` endpoints that no longer exist in that
shape. Landing it early would force the agent core to keep a UI-shaped API.

### G. Shutdown

Inspected on `main` directly, as instructed. `main` already has, unassumed:

- `BrokerOperation.DESKTOP_SHUTDOWN` wired to `desktop_shutdown()`;
- `"desktop.shutdown" = false` in the broker-local `[operations]` gate;
- `shutdown_enabled = false` in `assistant.toml` `[desktop]`;
- a registered `shutdown_desktop` skill in `skills/actions_v2.py`;
- `shutdown_desktop` in `PLANNER_CONFIRM_ACTIONS` in `web/service.py`.

So the two-gate architecture from memory is present in the committed tree, with
a third gate in the root-owned production file outside git.

One historical change deserves a look but must **not** be smuggled in here:
`1f59d48` changed `desktop-control.ps1` from `shutdown.exe /s /t 5` to `/t 0`,
on the grounds that a positive `/t` implies `/f` on Windows and force-closes
applications with unsaved work. `main` still carries the `/t 5` form byte-for-
byte from the merge base. If that reading is correct it is a real
data-loss defect in a currently-gated-off operation. **Recommendation: verify
independently and fix in its own commit, never inside a reintegration slice.**
It is deliberately not changed by this review.

### H. Deployment / install tooling

| Component | Assessment |
| --- | --- |
| `src/butters/deployment.py` (150 lines) | **Reuse nearly unchanged — highest value-per-risk in the whole lineage.** Computes a SHA-256 tree digest over `.py/.js/.css/.html`, compares the deployed tree against a `DEPLOYMENT` manifest, and derives an asset version. `/opt/home-sensor` is an rsync target rather than a git checkout, so there is currently no way to prove what is deployed. Entirely independent of the agent. |
| `scripts/verify-deployment` (95 lines) | **Reuse.** |
| `install-beta1` hunks | **Rewrite.** `main`'s installer has diverged. |
| `scripts/deploy-desktop-agent.py` | **Adapt.** Agent-specific; lands with the agent. |
| `scripts/verify-desktop-ssh.py`, `update-desktop-mdns.py` | **Drop `update-desktop-mdns.py`** (mDNS is rejected in §E). `verify-desktop-ssh.py` is **adapt** — useful, overlaps the broker's own pinning. |
| `windows/configure-git-bash-ssh.ps1` | **Adapt.** Windows-side; requires hardware validation. |

---

## 3. Dependency graph

```
butters-agent/protocol.py        ← no dependencies
   ↑            ↑
   │            └── butters-agent/{engine,client}.py ──→ platform/{win32,fake}.py
   │                                                       (win32 requires Windows)
   │
   └── butters/actions/agent.py (AgentHub) ──→ butters.diagnostics.sanitizer   [exists on main]
            ↑              ↑
            │              └── butters/skills/desktop_agent.py ──→ butters.skills.{model,policy,registry}
            │                                                         [all exist on main, signatures changed]
            │
            ├── butters/web/app.py  WebSocketRoute("/agent/v1/session")   [rewrite in place]
            ├── butters/web/service.py  BetaAssistantService.__init__     [rewrite in place]
            └── butters/actions/compute.py  DesktopActions.agent          [optional, defer]

butters/actions/agent_ingress.py ← standalone process; depends only on stdlib + a TLS keypair
                                   couples to main only via the constant 127.0.0.1:8090

butters/actions/streaming.py ──→ AgentHub + DesktopActions + BrokerClient   [defer entirely]

butters/src/butters/deployment.py ← no dependencies on any of the above      [fully independent]
butters/web/static/assets/admin.js ──→ service.py endpoints                  [last]
```

Two properties make this tractable:

1. `butters-agent/` is a leaf with no Butters imports. It can land, be tested and
   be reviewed with zero risk to the running system.
2. `deployment.py` is disjoint from the agent graph entirely.

One property makes it awkward: **`butters_agent` is not importable from the
Butters test suite as `main` is configured.** `pytest.ini` sets
`pythonpath = server/backend`, and `butters/tests/conftest.py` inserts only
`butters/src`. `test_desktop_agent.py` imports `butters_agent.engine` directly,
so the historical branch must have relied on an editable install that is not
recorded in the repository. **Any slice that makes Butters import
`butters_agent` must first make it importable in CI** — one line in
`butters/tests/conftest.py`.

---

## 4. Current-`main` equivalents, and what the agent would unlock

### 4.1 Equivalents already present

| Historical capability | `main` equivalent | Still needed? |
| --- | --- | --- |
| WOL | `BrokerOperation.DESKTOP_WAKE` + `wake_desktop` skill | No |
| Reachability / SSH observation | `DesktopState.network_reachable`, `.ssh_ready` | No |
| Parsec service control | `desktop.parsec_{status,ensure,restart}` | No |
| Shutdown | `desktop.shutdown` + `shutdown_desktop` + planner confirm | No |
| Monitor power / headless | `request_headless_mode`, `monitors_enabled` | No |
| Wake→SSH→Parsec workflow | `DesktopWorkflow.start_remote_session` | No |
| Registered-action authorization | `SkillRegistry` + `ActionCoordinator` + `PolicyValidator` | No |
| Gating discipline | two-layer `assistant.toml` + broker `[operations]` | No |
| **Interactive session observation** | **none** | **Yes** |
| **Agent liveness observation** | **none** | **Yes** |
| **Allowlisted GUI app launch** | **none** | **Yes** |
| **Deployment verification** | **none** | **Yes** |
| **Named SSH command catalog** | **none** | Useful, later |

### 4.2 What `docs/CONVERSATIONAL_ASSISTANT_ARCHITECTURE.md` says is blocked

The architecture document was written against a `main` without the agent, and it
names the gap precisely. §2.7: *"No interactive-session or agent-liveness
observer exists in `main`, because the Desktop Agent is absent. Those facets in
section 7 are therefore **proposed**, not present, and nothing in `main` can
supply them today."*

Restoring the agent core makes exactly these implementable, and nothing else:

| §7 facet | Values the doc specifies | Supplied by |
| --- | --- | --- |
| `desktop.interactive_session` | `present`, `absent`, `unknown` | `AgentHub.status()["interactive_session"]`, from the heartbeat `session` block |
| `desktop.agent` | `not_configured`, `disconnected`, `awaiting_heartbeat`, `heartbeat_stale`, `heartbeat_aging`, `connected` | `AgentHub.state()` |

The six `desktop.agent` values in the document are **verbatim** the six return
values of `AgentHub.state()`. The architecture was specified against this code.
That is the strongest available evidence that the agent core is the right first
slice, and that it should land *as a state observer* before it lands as an
effector.

§4 prerequisite vocabulary similarly lists `interactive_agent` as *"**proposed**:
no observer in `main`"* with the resolution policy *"never remediated
automatically; an absent agent is reported"* — which the historical `AgentHub`
already honours: it has no wake path and no reconnect-forcing API.

### 4.3 What must not move

Per the brief, and restated here as a constraint on every slice below:

- Planner policy is **not** modified to accommodate historical code.
- `llm/catalog.py`'s `MODEL_SAFE_ACTION_CLASSES` continues to admit only
  `READ_ONLY` and `ANALYTICAL`, and continues to reject anything with side
  effects, explicit-intent, confirmation, authentication or the administrator
  audience. Every agent mutation is `ACTION` + `ADMINISTRATOR` + explicit
  intent, so the model-visible catalog is unchanged by construction.
- The planner foundation's refusal of arbitrary multi-step physical composition
  is untouched. The agent supplies *facets* and *registered actions*; it does
  not supply a composition.

### 4.4 A note on `SkillSpec` drift

`main`'s `SkillSpec` has grown fields the historical `register_agent_skills` does
not set: `version`, `input_schema` (set), `output_schema`, `result_description`,
`positive_examples`/`negative_examples`, `source_reference`,
`validation_status`, `max_result_bytes`, `local_console_allowed`, `configured`,
`available`, `unavailable_reason`. Several have parity tests behind them —
`main`'s `get_parsec_status` registration, for instance, sets `configured`,
`available` and `unavailable_reason` from settings so a disabled capability is
*named* rather than hidden.

The historical registration must be rewritten to that standard, not merged. In
particular `configured`/`available`/`unavailable_reason` must be driven from the
agent's configuration state so that "no agent configured" is visible in the
catalog rather than appearing as a silently missing skill.

---

## 5. Obsolete historical components

Dropped outright:

| Component | Reason |
| --- | --- |
| `desktop.vm.{list,status,start,stop}` protocol actions | No backend exists or is planned. `UnavailableVmBackend` returns `vm_unavailable` unconditionally. Removing them shrinks the signed protocol surface by four actions and removes the only `FRESH`/confirmation-requiring agent skill. |
| `VmBackend`, `UnavailableVmBackend`, `vms.example.toml` | Same. |
| `scripts/update-desktop-mdns.py` and the `.local` host change | `main` pins `192.168.1.209` plus a pinned host key. mDNS is a weaker identity binding. |
| Historical `BetaAssistantService.execute_desktop_action` | See §6.1 — it is a second action entry point. Rewrite, do not port. |
| Historical `DesktopActions.catalog()` UI payload | Shaped for the historical Admin page; `main` renders from skill metadata. |
| `actions/streaming.py` as written | Superseded in part by `DesktopWorkflow`; out of scope by instruction; would need rewriting against the registered-action path anyway. |

Kept but deferred: `compute.py`'s SSH catalog (§2.D), the Admin Tools UI (§2.F),
`configure-git-bash-ssh.ps1`.

---

## 6. Security review

### 6.1 Finding S-1 — the historical UI path is a second action entry point (High)

`BetaAssistantService.execute_desktop_action` in `1f59d48` branches on the action
name and, for non-mutating agent actions, calls
`self.assistant.skills.execute(action, parameters, administrator=True)`
**directly**, bypassing `ActionCoordinator`. It then writes its own audit record
with `authentication=AuthenticationLevel.NONE` and
`method="tailnet_identity"`. For mutations it hand-rolls plan freezing,
inline-imports `uuid` and `dataclasses` inside the method, and decides
`pending_confirmation` from a literal `action == "desktop.vm.stop"`.

This directly contradicts the architecture document's §2.5, *"There is exactly
one action entry point today"*, which is the property the planner design depends
on.

**Required:** the reintegrated path must go through `SkillRegistry` +
`ActionCoordinator` exactly as `wake_desktop` and `shutdown_desktop` do, with
authentication and confirmation declared on the `SkillSpec` rather than decided
at the call site. This is the single largest rewrite in the reintegration, and
it is the reason the Admin UI slice must come last.

### 6.2 Finding S-2 — machine authentication is sound and correctly separated (Pass)

The historical design keeps the browser and machine trust domains genuinely
independent:

- **Transport identity.** The agent connects outbound over WSS and verifies the
  server's **SPKI SHA-256 pin** before sending anything. `check_hostname=False`
  and `verify_mode=CERT_NONE` are not a weakening — the pin *is* the identity,
  which is the right choice for a self-signed LAN service. The comment *"No
  Authorization header: token is first sent AFTER pin verification"* records the
  ordering deliberately.
- **Agent identity.** A 64-hex-character bearer token, compared by
  `hmac.compare_digest` against a stored `token_sha256`. The cleartext token
  never exists on the Butters side.
- **Frame integrity.** Every post-handshake frame in both directions is
  HMAC-SHA256 signed over canonical JSON with a 32-byte shared command key,
  including the `welcome`.
- **Replay and staleness.** `verify()` enforces ±90 s issuance freshness and
  binds every frame to the `connection_id` minted by
  `secrets.token_hex(32)` at accept time, so a captured frame cannot be replayed
  onto a later connection (`superseded_connection`). `request()` additionally
  tightens the window to the request's own `timeout_seconds` (1–30 s).
- **Browser credentials are rejected explicitly.** `AgentHub.socket` closes with
  1008 if the `Origin` header is present at all. The ingress handshake validator
  allows exactly five headers and rejects any `Cookie` or `Authorization`.
- **Secret storage.** The command key file is rejected if mode `& 0o027`; the
  config file if mode `& 0o022`; the agent stores its credentials under DPAPI.
- **Logging.** `client.py`'s comment — *"Never format exceptions: network
  exceptions may contain headers/tokens"* — is enforced: only enumerated
  `ProtocolError` codes or bare exception class names are logged. `AgentHub`
  follows the same rule. Agent results pass through `sanitize_value` before
  crossing into Butters.

**Verdict: appropriate for the current architecture, reuse unchanged.**

Two items to confirm at provisioning time rather than in code:

- **S-2a.** The command key is symmetric and shared, so the agent can forge
  Butters-signed frames to itself. That is acceptable — it authenticates the
  channel, not a privilege hierarchy — but it means the key must be per-machine
  and never reused across agents.
- **S-2b.** The ingress binds a private IPv4 on `eth0`/`wlan0` and refuses
  wildcard/public/non-private addresses. That must be re-verified against
  Butters' current LAN configuration, since memory records a LAN migration since
  this code was written.

### 6.3 Finding S-3 — arbitrary execution is genuinely foreclosed (Pass)

Checked end to end, because this is the invariant that matters most.

- `protocol.SCHEMAS` is a closed map; every parameter value must match
  `^[a-z][a-z0-9_]{0,63}$`. **No path, argument, or shell string can traverse
  the transport.**
- `Engine.__init__` reads the app registry from a local file the *platform*
  validates, requiring absolute Windows paths with a `.exe` suffix for both
  `path` and every `images` entry, and quarantines bad entries as
  `invalid_registry_entry` rather than failing open.
- `Platform.validate_registry` refuses a registry file unless the file **and its
  parent directory** are owned by `SYSTEM`/`Administrators` with no write-ish
  ACE (`0xD0156`) for anyone else. The path is passed to PowerShell
  base64-encoded, so even the local path is not interpolated into a script.
- `Platform.launch` uses `subprocess.Popen([entry["path"]], shell=False, …)`
  from the registry, then verifies via `ProcessIdToSessionId` that the child
  landed in the console session, raising `wrong_session` otherwise.
- `app_status`'s PowerShell is a fixed literal with **no interpolation at all**;
  image matching happens in Python over `os.path.normcase`.
- `compute.py`'s SSH catalog takes a name, not a command: `_name()` enforces the
  same identifier pattern, and the shell text lives only in a root-owned
  `/etc/butters/desktop-compute.toml` that is rejected if group/world writable.

Neither the browser, the planner, nor a user utterance can supply an executable
path, a command line, a shell fragment, an MQTT topic or a PowerShell string.
**The invariant holds.**

### 6.4 Finding S-4 — state truthfulness is a strength, with one thing to preserve (Pass)

`AgentHub.state()` deliberately refuses to collapse into a boolean and
distinguishes `not_configured` / `disconnected` / `awaiting_heartbeat` /
`heartbeat_stale` / `heartbeat_aging` / `connected`. The in-code comment
explains why: *"the desktop was shut down and the socket is gone" must not look
the same as "the agent is attached but its heartbeat has aged out"*.

Three details that must survive the rewrite:

- **`last_seen = None`, never `0`.** The comment records a real, fixed bug:
  `time.monotonic()` is time since boot, so within the first 45 s of uptime a
  `0` sentinel yielded `age < 45` and an *unauthenticated* socket reported
  itself as a live agent. Preserve the sentinel and its comment.
- **`_disconnect()` clears the session snapshot.** A cleanly shut-down desktop
  must not leave Butters reporting the interactive session it last saw.
- **`session` is reported as `{"state": "UNKNOWN"}` when not connected**, and
  `capabilities.gui_launch` requires a heartbeat younger than 30 s — a tighter
  bound than the 45 s liveness threshold, so "can I launch a GUI app" is never
  answered from staler evidence than "is the agent alive".

This maps onto the architecture document's §7 rule that `unknown` is never
rendered as `off` and `stale` is never rendered as current. `AgentHub` is
already compliant; it must be surfaced as `StateFacet`s with
`confidence="observed"/"unknown"` rather than flattened.

### 6.5 Finding S-5 — idempotency is well covered at the effector (Pass, one gap)

- Each request carries a UUIDv4 `request_id` **and** a separate UUIDv4
  `idempotency_key`.
- The agent's `ReplayCache` keys terminal results under both, with a
  SHA-256 fingerprint over `(action, target, parameters)`. A key reused with
  *different* parameters raises `duplicate_request` and **never executes** —
  conflicts fail closed rather than returning a stale result.
- The cache is bounded (512 entries, 300 s TTL), in-memory only, with no durable
  queue — so a side effect is never resurrected across an agent restart.
- The client admits **one live operation at a time**; a second concurrent
  request gets `busy` rather than being queued, so there are no queued side
  effects.
- `AgentHub.invoke` takes a non-blocking `threading.Lock` and returns `busy`,
  mirroring the same rule on the Butters side.
- A duplicate frame for an already-active request is compared canonically and
  re-`ack`ed rather than re-executed.
- On the cancel path, `engine` returns `side_effect_committed: True` when a
  launch was already issued — the result does not claim nothing happened.
- Launch itself is idempotent in effect: an already-running app returns
  `state: "already_running"` without launching a second process.

**Gap:** this is all *effector* idempotency. The architecture document's §9
layer 1, utterance-level dedupe, remains unimplemented, and the document states
it *"becomes a blocker as soon as a non-idempotent `REVERSIBLE` action joins the
catalog"*. `desktop.app.launch` is idempotent in effect, so restoring it does
not trip that blocker — but this should be re-checked before any *further*
agent action is registered.

### 6.6 Finding S-6 — the ingress is a transport, and must stay one (Pass, with a constraint)

`agent_ingress.py` is deliberately not an API: it terminates TLS, validates a
single HTTP upgrade against a five-header allowlist and a literal request line,
and pipes bytes to `127.0.0.1:8090`. It caps concurrency at 4, times out the
handshake at 5 s, bounds reads at 32 KiB with a 60 s idle timeout, and swallows
handshake exceptions without logging (*"Never log handshake headers/
credentials"*).

**Constraint on reintegration:** Butters' web daemon must remain loopback-only.
The ingress is the *only* thing that may bind the LAN, and it must run as its
own unit with its own user. Mounting `/agent/v1/session` on the Starlette app
does not by itself expose it, because the app is not LAN-bound — but that
property is now load-bearing and should be asserted by a test.

### 6.7 Finding S-7 — an `error` frame can be matched to the wrong request (Low)

In `AgentHub.socket`:

```python
if kind == "error" and pending is None and len(self.pending) == 1:
    pending = next(iter(self.pending.values()))
```

An `error` frame with no `request_id` is attributed to the sole in-flight
request. It is guarded by `len(self.pending) == 1` and by the fact that only an
HMAC-verified agent can send it, and `AgentHub.invoke` serialises on a lock so
there is normally only one. It is nonetheless a heuristic that resolves a frame
to a request by count rather than by identity, and it converts into
`agent_rejected_request` — a *terminal* result. Tighten or document explicitly
during the rewrite.

### 6.8 Finding S-8 — shutdown timer semantics (Informational, do not act here)

See §2.G. `desktop-control.ps1` on `main` uses `shutdown.exe /s /t 5`; the
historical commit changed it to `/t 0` because a positive `/t` implies `/f`. If
correct, this force-closes applications with unsaved work on an operation that
is currently gated off. Verify independently; fix in its own commit; do not
enable the gate to test it.

---

## 7. Recommended integration slices

Ordered by dependency and by risk to the running system. Each is independently
mergeable and independently revertible.

### Slice 0 — deployment verification (optional, fully independent)

- **Capability:** prove which tree is deployed at `/opt/home-sensor`, which is an
  rsync target with no git metadata.
- **Reuse:** `butters/src/butters/deployment.py`, `butters/scripts/verify-deployment` — near-unchanged from `1aa02f0`.
- **Rewrite:** the `install-beta1` and `test_beta1_installer_permissions.py` hunks.
- **Dependencies:** none. Does not touch the agent graph.
- **Security boundary:** read-only digesting; no new privilege, no new listener.
- **Tests:** unit tests for `tree_digest`/`describe`/`asset_version`; a manifest round-trip.
- **Deployment impact:** adds a `DEPLOYMENT` manifest to the rsync payload.
- **Windows changes:** none. **Hardware validation:** none.

Listed first because it is the lowest-risk useful thing in the lineage, but it
is *not* the recommended first Codex task — it does not advance the agent.

### Slice 1 — Desktop Agent package and protocol only

- **Capability:** none at runtime. Vendors a reviewable, tested, importable
  `butters-agent/` package. Nothing in Butters imports it yet.
- **Reuse unchanged:** `protocol.py`, `engine.py`, `client.py`, `platform/fake.py`, `platform/win32.py`.
- **Rewrite:** drop all `desktop.vm.*` from `SCHEMAS`/`MUTATIONS`, `VmBackend`,
  `UnavailableVmBackend`, the `desktop.vm.` branch in `Engine._invoke`, and
  `vms.example.toml`. Adapt `provision.py`/`selftest.py`/`__main__.py` paths.
- **Dependencies:** none inside Butters. Add `butters-agent/src` to
  `butters/tests/conftest.py`.
- **Security boundaries:** §6.3 (allowlist) and §6.5 (idempotency) are fully
  testable inside this slice, on the Pi, with `platform/fake.py`.
- **Tests:** the protocol/engine/replay-cache portion of `test_desktop_agent.py`
  — signature verification, staleness, connection binding, malformed frames,
  idempotency-key conflict, unknown-app rejection, registry validation, launch
  allowlisting, `already_running`.
- **Deployment impact:** none. Not installed on the Pi.
- **Windows changes:** none required to land; `win32.py` is untestable here and
  ships unexercised.
- **Hardware validation:** not required to merge.

### Slice 2 — authenticated ingress and the state model

- **Capability:** Butters can *observe* a connected agent. Adds the
  `desktop.agent` and `desktop.interactive_session` facets. **No effector.**
- **Reuse:** `agent_ingress.py`; `AgentHub` with `36c2010`'s hardening folded in
  as the starting state.
- **Rewrite:** the `WebSocketRoute` line and `BetaAssistantService.__init__`
  wiring against current `main`; the systemd unit; a `[desktop.agent]` gate in
  `assistant.toml` defaulting to disabled.
- **Amputate:** land `AgentHub` with `invoke()` unreachable — registration of
  mutating skills belongs to Slice 3. Status only.
- **Dependencies:** Slice 1.
- **Security boundaries:** §6.2, §6.6; tighten §6.7. A test must assert the web
  daemon stays loopback-bound and that an `Origin`-bearing socket is refused.
- **Tests:** the `AgentHub` half of `test_desktop_agent.py`, plus new tests for
  the `state()` machine across all six values, the `last_seen`-sentinel
  regression, session clearing on disconnect, and handshake-allowlist rejection.
- **Deployment impact:** a new systemd unit, a TLS keypair, an
  `/etc/butters/agents.toml`, and one LAN-bound port. First real deployment step.
- **Windows changes:** none to merge; a real agent is needed to validate.
- **Hardware validation:** **required before enabling the gate.**

### Slice 3 — registered desktop-agent actions

- **Capability:** `desktop.app.launch` / `desktop.app.status` / `desktop.app.list`
  as registered skills behind `SkillRegistry` + `ActionCoordinator`.
- **Reuse:** the shape of `skills/desktop_agent.py`.
- **Rewrite:** the registration to `main`'s `SkillSpec` (§4.4), with
  `configured`/`available`/`unavailable_reason` driven from agent config.
- **Dependencies:** Slices 1–2.
- **Security boundaries:** §6.1 is resolved *here* by construction — there is no
  second entry point because there is no new endpoint.
- **Tests:** catalog parity (the model-visible catalog must be unchanged);
  authorization for each action; `busy`; `agent_unavailable`; `unsupported_action`.
- **Deployment impact:** none beyond Slice 2. **Windows/hardware:** validation
  required before enabling.

### Slice 4 — deployment and install verification for the agent

`deploy-desktop-agent.py`, the Windows installer, `configure-git-bash-ssh.ps1`,
`verify-desktop-ssh.py`. Requires the real desktop. Depends on Slices 1–3.

### Slice 5 — Admin Tools integration

The ~1170 lines of `admin.js`/`admin.html`/`styles.css` from `36c2010`,
`9adcbf4`, `5c66593`, plus `484d061`'s regression tests and `ADMIN_OPERATIONS.md`.
Textually clean against `main`, contractually stale. Rewrite every backend call
against Slice 3's registered actions. **Explicitly not required by any earlier
slice.**

### Slice 6 — named SSH command catalog

`compute.py`'s `[commands]`/`[projects]` catalog, routed through the existing
broker rather than a second SSH implementation (§2.D).

### Slice 7 — Parsec workflow composition

Out of scope by instruction. Prerequisites (`desktop.agent`,
`desktop.interactive_session`, `desktop.app.launch`) do not exist until Slice 3
lands and is hardware-validated. Not to be started before then.

---

## 8. Testing strategy

| Layer | Where | Runs on the Pi? |
| --- | --- | --- |
| Protocol: signing, staleness, connection binding, malformed frames | `butters-agent`, pure | Yes |
| Replay/idempotency: key reuse, fingerprint conflict, TTL, capacity | `butters-agent`, pure | Yes |
| Engine: allowlist, unknown app, invalid registry entries, `already_running`, `require_visible`, cancel | `butters-agent` + `platform/fake.py` | Yes |
| `AgentHub` handshake: bad token, bad agent id, `Origin` present, unknown action in `hello` | `butters/tests` | Yes |
| `AgentHub` state machine: all six `state()` values, the `last_seen` sentinel, session clearing | `butters/tests` | Yes |
| Ingress: handshake allowlist, path rejection, oversize, private-bind refusal | `butters/tests` | Yes |
| Skill registration: authorization, catalog parity, unavailable reasons | `butters/tests` | Yes |
| `platform/win32.py`: WTS session facts, launch, ACL validation | manual | **No — Windows only** |
| End-to-end: agent connects, heartbeats, launches Git Bash | manual | **No — hardware** |

Baseline to preserve: 426 backend + 914 Butters tests passing, 4 pre-existing
websocket/uvicorn deprecation warnings, no skips on this host.

Additional gate: a test asserting the model-visible tool catalog is byte-identical
before and after each slice. The planner must gain **no** new model-executable
capability from any of this.

## 9. Deployment and manual-validation requirements

Nothing in Slice 0 or Slice 1 reaches production. From Slice 2 on:

1. **TLS keypair** for the ingress, with its SPKI pin recorded for the agent.
2. **`/etc/butters/agents.toml`** root-owned, mode `0600` (rejected if
   group/world-writable), with `agent_id`, `token_sha256`, `command_key_file`.
3. **Command key file** root-owned, mode `0600` (rejected if `& 0o027`).
4. **A new systemd unit** `butters-agent-ingress.service`, separate user,
   re-hardened against current conventions.
5. **One LAN-bound port (8443)** on a private IPv4 only — re-verify the adapter
   names against the post-migration LAN configuration.
6. **The Windows agent** installed as a scheduled task in the interactive
   session, with DPAPI-stored credentials.
7. **Gates stay off** until (1)–(6) are verified together. Enabling the
   `[desktop.agent]` gate is a deliberate, separately-reviewed act.

Manual validation needs the real `DESKTOP-G4CFVL1`
(`34:5A:60:D7:4C:2C`, `192.168.1.209`, Git Bash as the OpenSSH default shell).
Machine-specific values remain configuration, never planner-supplied parameters.

Do not enable `desktop.shutdown` to exercise any of this.

---

## 10. Exact first Codex implementation slice

**Task: vendor the Desktop Agent package, VM abstraction removed, with its
protocol and engine test suite. Nothing in Butters imports it.**

- **Branch:** `feature/desktop-agent-package`, isolated worktree, from
  `origin/main` at `bcaab83`.

**In scope — add:**

```
butters-agent/.gitignore
butters-agent/README.md
butters-agent/pyproject.toml
butters-agent/apps.example.toml
butters-agent/config.example.toml
butters-agent/src/butters_agent/__init__.py
butters-agent/src/butters_agent/__main__.py
butters-agent/src/butters_agent/protocol.py
butters-agent/src/butters_agent/engine.py
butters-agent/src/butters_agent/client.py
butters-agent/src/butters_agent/provision.py
butters-agent/src/butters_agent/selftest.py
butters-agent/src/butters_agent/platform/__init__.py
butters-agent/src/butters_agent/platform/fake.py
butters-agent/src/butters_agent/platform/win32.py
butters-agent/tests/test_protocol.py        (new)
butters-agent/tests/test_engine.py          (new)
```

**In scope — modify (exactly two files):**

- `butters/tests/conftest.py` — append `butters-agent/src` to `sys.path`.
- `README.md` — one line in the Documentation list for this review.

Source: `1f59d48`, adapted. Nothing from the other six commits.

**Mandatory deletions from the historical source:**

- `desktop.vm.list`, `desktop.vm.status`, `desktop.vm.start`, `desktop.vm.stop`
  from `SCHEMAS`; `desktop.vm.start`/`stop` from `MUTATIONS`.
- `VmBackend`, `UnavailableVmBackend`, `Engine.vm`, and the
  `action.startswith("desktop.vm.")` branch in `Engine._invoke`.
- `vms.example.toml`.

**Must be preserved verbatim, with their comments:**

- The `±90 s` issuance window and `connection_id` binding in `verify()`.
- `ReplayCache`'s fingerprint-conflict behaviour: a reused key with different
  parameters raises `duplicate_request` and does not execute.
- `verify_pin` running *before* the token is sent, and the comment saying so.
- `client.run`'s refusal to format non-`ProtocolError` exceptions into logs.
- `launch`'s `shell=False` plus the `ProcessIdToSessionId` check.
- `validate_registry`'s base64 path encoding and ACL check.

**Explicitly out of scope:**

`butters/actions/agent.py`, `agent_ingress.py`, `compute.py`, `streaming.py`,
`skills/desktop_agent.py`, `deployment.py`, every `web/` change, every config or
systemd file, every Windows install script, the Admin UI, `assistant.toml`,
`broker.py`, `desktop-control.ps1`, and anything Parsec.

**Definition of done:**

1. `pytest butters/tests butters-agent/tests server` — 426 backend and 914
   Butters tests still pass, plus the new agent tests; no new warnings, no skips.
2. `python -c "import butters_agent.protocol, butters_agent.engine"` succeeds
   on the Pi (`platform.win32` is not imported on Linux).
3. `grep -rn "butters_agent" butters/src` returns **nothing** — Butters does not
   yet depend on the package.
4. `grep -rn "desktop.vm" butters-agent` returns nothing.
5. `git diff --check` is clean.
6. No file under `butters/src`, `butters/config`, `butters/systemd`,
   `butters/scripts`, `server/`, or `esp/` is modified.

**Why this slice:** it is the leaf of the dependency graph, it carries the two
security properties most worth reviewing in isolation (§6.3 allowlisting and
§6.5 idempotency), it is fully testable on the Pi with `platform/fake.py`, it
needs no UI, no Windows host, no Parsec, no new listener, no new privilege and
no deployment change — and it changes nothing about how the running system
behaves. It can be reverted by deleting a directory.

---

## 11. Summary of dispositions

| Component | Disposition |
| --- | --- |
| `butters-agent/{protocol,engine,client}.py`, `platform/{fake,win32}.py` | **Reuse unchanged** (minus VM) |
| `butters/src/butters/deployment.py`, `scripts/verify-deployment` | **Reuse near-unchanged** |
| `actions/agent_ingress.py`, `config/agent-ingress.toml` | **Reuse, re-verify bind + upstream** |
| `actions/agent.py` (`AgentHub`) | **Adapt** — fold in `36c2010`, tighten §6.7 |
| `skills/desktop_agent.py` | **Adapt** to current `SkillSpec` |
| `butters-agent` provisioning/installer, `configure-git-bash-ssh.ps1` | **Adapt**, hardware-gated |
| `actions/compute.py` SSH catalog | **Adapt**, route via broker, later |
| Admin UI (`36c2010`/`9adcbf4`/`5c66593`/`484d061`) | **Rewrite** backend contracts; last |
| `BetaAssistantService.execute_desktop_action` | **Rewrite** — never port (§6.1) |
| `actions/streaming.py` | **Rewrite**, deferred, out of scope |
| `install-beta1` hunks, systemd unit | **Rewrite** against current `main` |
| VM abstraction, `vms.example.toml`, `update-desktop-mdns.py`, `.local` host | **Drop** |
| WOL / broker / shutdown / Parsec service / headless work | **Drop as redundant** — `main` is ahead |
