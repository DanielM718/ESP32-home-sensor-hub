# Desktop Agent Slices 1 and 2 — Implementation Review

Status: review only. Nothing is merged, deployed, or enabled by this document.

Reviewed on 2026-09-11 against `origin/main` at `bcaab83`, the reintegration
audit in `docs/DESKTOP_AGENT_REINTEGRATION_REVIEW.md`, and the live production
constraint recorded in §7 below.

| Subject | Ref | Head |
| --- | --- | --- |
| Slice 1 | `origin/feature/desktop-agent-package` | `b293258` |
| Slice 2 | `origin/feature/desktop-agent-ingress-state` | `3ef985d` |
| Baseline | `origin/main` | `bcaab83` |

---

## 1. Ancestry

`git merge-base origin/main origin/feature/desktop-agent-ingress-state` is
`bcaab83`, which is `origin/main` itself. Slice 2 is therefore a fast-forwardable
descendant of current `main` with exactly three commits:

```
3ef985d Expose passive agent facets in Admin overview
d92dca6 Add observer-only Desktop Agent ingress state
98d7e3e Add standalone Desktop Agent package
```

**Slice 1 is not an ancestor of Slice 2**; it was reapplied. The reapplication is
exact, by two independent measures:

- identical tree: `b293258^{tree}` and `98d7e3e^{tree}` are both
  `81ec749096...`;
- identical `git patch-id --stable`: `d955c99017...` for both.

`origin/feature/desktop-remote-management-v2` is **not** an ancestor of Slice 2.
No historical desktop-management lineage is dragged in. The 36-file diff against
`main` contains only `butters-agent/`, the ingress/state modules, their config,
unit, installer, docs, and tests.

**Merging Slice 2 alone brings Slice 1 in its entirety.** A separate Slice 1
merge is unnecessary and would only replay an already-present patch.

## 2. Observer-only claim — verified

The complete set of references to the hub anywhere outside its own module and
tests is:

```
web/service.py:195   self.desktop_agent = AgentHub(settings.agent_ingress)
web/service.py:282   self.desktop_agent.snapshot().safe_dict()
web/app.py:420       runtime.desktop_agent.snapshot().safe_dict()
web/app.py:1616      WebSocketRoute("/agent/v1/session", runtime.desktop_agent.socket)
```

`AgentHub` defines no `invoke`, `send`, `request`, `command`, `dispatch`,
`launch`, or `execute` method; no pending-request map, no future/callback
registry, no `subprocess` use. Its only outbound frame is the signed `welcome`
(one `send_text`, `agent.py:199`). Nothing registers a skill with
`SkillRegistry`, nothing touches `ActionCoordinator`, and no
`desktop.agent*`/`app.launch` entry appears in `CONVERSATIONAL_PLANNER_ACTIONS`.
No `/api/desktop/*` route exists.

**Dormant code that could later become an effector**, classified:

- `butters_agent.protocol.request()`, `ReplayCache`, and `MUTATIONS`
  (`{"desktop.app.launch"}`) ship in the standalone package. They are the
  *agent-side* request path and are unreferenced from Butters.
- `butters_agent.engine` / `platform.win32` contain the actual launcher. Not
  imported by Butters; `AgentHub._protocol()` imports `butters_agent.protocol`
  only, and uses it solely for `decode`/`verify`/`sign`/`envelope`/`SCHEMAS`.
- The material consequence: `AgentHub` already holds a live socket, the command
  key, and the signing primitives. Turning it into an effector is a
  small addition at a single call site. The absence of an effector is a
  **deliberate omission, not a structural barrier**. Slice 3 review must treat
  the appearance of any second caller of `protocol.envelope`/`sign` inside
  `AgentHub` as the security-relevant change.

`hello.actions` is validated against `SCHEMAS` but the accepted list is then
**discarded** — it is not stored, not exposed, and not used for dispatch.

## 3. Machine authentication — verified

| Property | Where | Result |
| --- | --- | --- |
| Token digest comparison | `agent.py` `_authenticate_hello` | `hmac.compare_digest(sha256(token).hexdigest(), stored)`; length-64 enforced |
| One identity | `_load_credentials` | `agent_id` hardcoded to `"desktop"` |
| Signed frames | `protocol.sign`/`verify` | HMAC-SHA256 over canonical JSON of all non-`sig` fields |
| Freshness | `protocol.verify` | `abs(now - issued_at) > 90` rejected; NaN/inf rejected |
| Connection binding | `protocol.verify` | frame `connection_id` must equal the server-minted 32-byte value |
| Replay/sequence | `agent.py` loop | `seq` strictly increasing, `type(seq) is int` (excludes `bool`) |
| Superseding | `agent.py` | new connection re-mints id, closes old with 1012; `_is_current` stops the old loop from clobbering new state |
| Disconnect cleanup | `_disconnect` | socket, id, timestamps, `_session`, `version` all cleared |
| Origin rejection | `agent.py` `socket()` | checked **before** `accept()`, closes 1008 |
| Browser-auth separation | proxy `validate_handshake` | header allowlist of exactly five names; Cookie/Authorization/Origin/Tailscale/CSRF/X-Forwarded-* all rejected as `unexpected_header` |
| Strict upgrade | `validate_handshake` | exact request line, `Upgrade: websocket`, `Connection` token, version `13`, 16-byte base64 key; request is **rebuilt** from the allowlist, not forwarded |

### Upgrade-before-auth nuance — acceptable

At the ASGI layer the WebSocket is `accept()`ed before the `hello` is
authenticated. This is acceptable here:

1. `Origin` is rejected *before* accept, so no browser can reach the accepted
   state.
2. Accept mutates no hub state. `self._socket`, `connection_id`, and
   `connected_at` are assigned only after `_authenticate_hello` succeeds, so an
   unauthenticated peer cannot supersede a live agent or move `desktop.agent`
   off `disconnected`.
3. `hello_timeout_seconds` (1–15, default 5) bounds the unauthenticated window.
4. The route exists only when the gate is on, and is reachable only via the
   proxy, which independently strips browser semantics and caps concurrency at
   four connections.

### Minor: verification order leaks a distinguishable reason

`protocol.verify` checks freshness and `connection_id` **before** the signature.
An unauthenticated peer can therefore distinguish `stale_request` /
`superseded_connection` from `invalid_signature`. No state changes on any of
these paths and the reason is never surfaced (see §6), so the impact today is
nil. Checking the signature first would be strictly more conservative.

## 4. Configuration and secrets — verified, one gap

- **Both gates default false.** `AgentIngressSettings.enabled = False`;
  committed `butters/config/assistant.toml` has `[agent_ingress] enabled = false`;
  `agent-ingress.example.toml` has `enabled = false`, parsed as
  `data.get("enabled") is True`.
- **Disabled application gate means the route is absent.** `web/app.py:1614`
  appends the `WebSocketRoute` only under `if configured.agent_ingress.enabled`.
  Not a 403 — the path does not exist.
- **Not auto-enabled.** `install-agent-ingress` enables/starts only with explicit
  `--enable`/`--start`; `install-beta1` contains no reference to the agent or the
  ingress at all.
- **No committed secrets.** The templates carry
  `REPLACE_WITH_64_HEX_SHA256_OF_AGENT_TOKEN` and file paths only; no key
  material, no 64-hex literal, no PEM block anywhere in the diff.
- **Digest, never plaintext.** Only `token_sha256` is stored server-side;
  validated as 64 hex characters and lowercased.
- **HMAC key fails closed.** `command_key_file` must be absolute and
  `stat().st_mode & 0o027 == 0` (so `root:butters 0640` passes, group-write or
  any world bit fails); must decode to exactly 32 bytes. The identity config
  itself is rejected if group/world-writable (`& 0o022`). Any failure returns
  `None`, leaving `configured == False` and the state `not_configured`.
- **Unit exposes no secrets.** `butters-agent-ingress.service` carries paths
  only, plus `UMask=0077`, `CapabilityBoundingSet=`, `ProtectSystem=strict`,
  `NoNewPrivileges`, and a restricted address family set.
- **Bind validation prevents public/wildcard exposure.** `bind_addresses`
  resolves named interfaces via `SIOCGIFADDR` or a host lookup, then requires
  every resulting address to be IPv4, `is_private`, and not `is_unspecified`
  (which is what rejects `0.0.0.0`, since it otherwise reports as private).
  `upstream_host` is restricted to loopback and both ports to 1024–65535.

### Gap: the TLS private key's permissions are never validated

`load_config` checks only `private_key.is_absolute()`. Unlike the HMAC command
key, the TLS private key's mode is never inspected before
`context.load_cert_chain`. The installer protects the *directory*
(`/etc/butters/desktop-agent`, `0750 root:butters`) but never the key file,
which the operator provisions by hand. The reported property "TLS/key material
root-managed … permissions fail closed" is therefore true of the HMAC key and
**not** of the TLS key. Recommended follow-up: apply the same `& 0o027` check to
`private_key` in `load_config`.

## 5. State truthfulness — correct, but two states are untested

Correct by inspection:

- `_agent_state` returns `connected` only when a socket is attached **and**
  `last_authenticated_activity is not None`. The listener existing is not
  sufficient; `configured` alone yields `disconnected`.
- An authenticated `hello` alone yields `awaiting_heartbeat`
  (`last_authenticated_activity` is explicitly reset to `None` at handshake).
  Asserted at `test_desktop_agent_ingress_state.py:159`.
- `desktop.interactive_session` is derived **only** from the signed heartbeat's
  `session.interactive_session` boolean, and is forced to `unknown` whenever
  `agent_state != "connected"`. No power, network, SSH, or Parsec signal feeds
  it anywhere.
- `_disconnect` clears `_session`, so a dropped session clears the observation;
  asserted at line 164.
- Clocks are injectable (`monotonic`, `wall_clock` constructor parameters), so
  the thresholds *are* deterministic.

### Finding: `heartbeat_aging` and `heartbeat_stale` are never exercised

The suite asserts `not_configured`, `disconnected`, `awaiting_heartbeat`, and
`connected` (the last via `agent_connected is True`). Neither aging state is
asserted anywhere, and `_hub()` never injects the clocks that would make driving
them deterministic. The audit's testing strategy called for "the `state()`
machine across all six values"; four of six are covered. Untested along with
them: the `confidence == "stale"` downgrade, the `incomplete` tuple, and the
interactive-session downgrade to `unknown` on aging.

Compounding this, with the **committed defaults** `heartbeat_stale_seconds = 45`
and `socket_idle_seconds = 45` are equal, so the idle read timeout fires at the
same instant `heartbeat_stale` becomes reachable — the state is effectively
racy-to-dead under the shipped configuration, and is only genuinely observable
when an operator widens `socket_idle_seconds`. `validated()` permits this
(`stale <= idle`). Either tighten the invariant to `stale < idle`, or test the
state with injected clocks, or both.

This is a test/coverage defect, not a correctness defect: the reachable states
are truthful.

### Minor: a superseded connection can clobber `reason`

When a new connection supersedes an old one, `_is_current` correctly prevents
the old task from calling `_disconnect`. It does **not** prevent the old task's
`except` handler from writing `self.reason`. A live, connected agent can
therefore carry a stale failure string in `reason`. Today `reason` is surfaced
nowhere (`status()` has no caller outside tests, and `snapshot()`/`safe_dict()`
omit it), so the impact is nil — but it should be guarded before `reason` is
ever exposed.

## 6. Passive Admin overview — verified read-only

`3ef985d` is 29 added lines across three files. It adds one key to the existing
read-only `/api/admin/overview` response. It touches **no** `admin.js`,
`admin.html`, or `styles.css`; adds no route, no button, no mutation, and no
POST handler. None of the historical Admin action paths reappear.

Exposure is bounded structurally: `StateSnapshot.safe_dict()` emits only
`facets` (each `name`, `value`, `confidence`, `observed_at`, `age_seconds`),
`assembled_at`, and `incomplete`. Confirmed absent: connection IDs, tokens,
digests, HMAC details, the agent-reported action list, request IDs, and all
transport details. `reason` — which can carry a protocol error string — is
deliberately not part of `safe_dict()`. A regression test asserts
`"connection_id" not in json.dumps(snapshot)`.

`BetaAssistantService` also begins passing the same `safe_dict()` into
`PlannerRequest.current_state`, replacing `{}`. This is plumbing only: no
planner provider reads `current_state` (the field has no consumer anywhere in
`butters/src`), and the default provider is `DisabledPlannerProvider`. The
model-visible action catalog is unchanged. It is worth stating precisely: Slice 2
adds planner *state* plumbing, and no planner *action* exposure.

## 7. Production constraint and merge safety

Live Butters intentionally runs `feature/desktop-remote-management-v2` at
`5c66593`, which carries the richer Desktop/Admin UI. A previous deployment from
current `main` removed those controls and was rolled back. Consequently
**`install-beta1` must not be run from a current-main-based branch** — including
Slice 2 — until later slices restore that UI parity. Slice 2 does not change
this; it also does nothing to help it, because Admin UI restoration is Slice 5.

Merging Slice 2 to `main` is a separate question from deploying it, and with
gates off the active-behavior delta is:

| Change | Effect with gates off |
| --- | --- |
| `WebSocketRoute("/agent/v1/session", …)` | not registered; path does not exist |
| `AgentHub(settings.agent_ingress)` at service init | constructed; no credential load, no `butters_agent` import, no listener |
| `/api/admin/overview` gains `desktop_agent_state` | additive key, always `not_configured`/`unknown` |
| `PlannerRequest.current_state` | populated but unconsumed |
| `butters-web.service` `PYTHONPATH` gains `/opt/butters-agent/src` | a nonexistent path entry; ignored by Python |
| `pytest.ini` `pythonpath` | test-time only |
| `server/backend/**` | **untouched** (empty diff) |

No new listener, no new privilege, no new model-executable capability, and no
change to any existing action. A dormant merge is behavior-preserving.

Merging does not itself deploy: `/opt/home-sensor` and `/opt/butters` are rsync
targets updated only by an explicit installer run. The one thing a merge *does*
change is that the next person who runs `install-beta1` from `main` would ship
the modified `butters-web.service`. That is already unsafe for the UI-parity
reason above, so it adds no new hazard — but it makes the existing "do not run
`install-beta1` from main" constraint more important to record, not less.

**Nothing requires hardware validation before a dormant merge.** The audit's own
Slice 2 entry says hardware validation is "required before *enabling the gate*",
not before merge.

## 8. Hardware-validation timing

Recommendation: **A, with the preparatory half of B.**

Merge Slice 2 dormant now. Postpone real-Windows validation until it can be done
without replacing production, and do not gate the merge on it.

Rejecting C: requiring validation now forces either an `install-beta1` run from a
current-main branch — which regresses the live Admin UI and repeats the
rollback — or a hand-placed parallel install. The former is explicitly off the
table. The latter is B.

B is the right *destination* but is not free: a second Pi-side install root, a
second TLS keypair, a second LAN port, and an agent-side SPKI pin pointing at the
staging ingress. The ingress is already well-suited to it — it is a standalone
process reading `BUTTERS_AGENT_INGRESS_CONFIG`, with a configurable port and
`upstream_port`, so a staging instance can run beside production against a
separate loopback web daemon without touching `/opt/butters`. Build that when
Slice 3's effectors make validation genuinely load-bearing.

## 9. Slice 3 readiness

Slice 3 may **begin** before Slice 2 is hardware-validated, and may land dormant,
subject to one condition that should not be waived.

Arguments that it is safe: the effectors are gated by the same two default-false
gates; with the gates off there is no listener, so an effector has no transport
and is unreachable by construction; `SkillRegistry` + `ActionCoordinator` is the
single existing entry point, which is precisely what resolves audit finding S-1;
and the pure-Python half (registration, authorization, catalog parity, `busy`,
`agent_unavailable`) is fully testable on the Pi against `platform/fake.py`.

The real risk is not that an effector lands unvalidated — it is that the
*request/response half of the transport* has never run against a real agent, so
Slice 3 would be written against an unexercised wire contract. `AgentHub`
currently exercises only `hello` → `welcome` → `heartbeat`. The `request` / `ack`
/ `result` / `error` / `cancel` path in `client.py` has never been executed
end-to-end by anything.

So: implement Slice 3, keep it dormant, and treat its merge as reviewable on the
same dormant-merge basis as Slice 2 — but **validate the ingress against a real
Windows agent before enabling any gate**, and expect Slice 3's wire contract to
need adjustment after that first real connection. Do not let effector code reach
an *enabled* gate on a transport that has never carried a real request.

## 10. Test substantiation

Re-run on this host from a detached worktree at `3ef985d`:

| Suite | Reported | Observed |
| --- | --- | --- |
| `butters-agent/tests` | 39 passed | **39 passed** |
| `butters/tests/test_desktop_agent_ingress_state.py` | 23 passed | **23 passed** |
| `butters/tests` | 935 passed, 2 skipped | **935 passed, 2 skipped, 4 warnings** |
| `server/backend` | 426 passed | **not executed here** — see below |

The 2 skips are environmental (`sherpa-onnx` runtime/model absent from this
venv: `test_live.py:542`, `test_stt.py:226`), not a slice regression. The audit's
"no skips on this host" baseline was recorded on a host with those models
installed; that line is stale rather than violated.

`server/backend` could not be executed here — the available interpreter lacks
`flask`, `dotenv`, and `influxdb_client`, and the backend venv has no `pytest`.
The claim is nonetheless substantiated structurally:
`git diff origin/main origin/feature/desktop-agent-ingress-state -- server/` is
**empty**, so the backend suite cannot have changed.

`systemd-analyze verify butters-agent-ingress.service` exits 0. Its single
complaint — `/opt/butters/.venv/bin/python is not executable: Permission denied`
— is an artifact of running the check unprivileged, not a unit defect.

## 11. Verdicts

### Slice 1 — APPROVE

The package is dormant and unreferenced by Butters; no listener, no effector
reachable from the Pi. VM abstraction is fully removed (zero `vm`/`VmBackend`
references anywhere under `butters-agent/`). Protocol signing, ±90s freshness,
and connection binding are intact and tested. Audit finding S-7 is genuinely
fixed: `client.py` validates `request_id` independently into `request_identity`
and omits the field from an `error` frame when it cannot be validated, so an
error can never be matched to the wrong request. Repository-test importability is
provided by `pytest.ini` `pythonpath` with no editable install, and asserted by
`test_importability.py`.

### Slice 2 — APPROVE WITH MINOR FOLLOW-UP

The observer-only, authentication, secrets, state-truthfulness, and passive-Admin
claims all hold under inspection. Three non-blocking follow-ups, none of which
affects a gates-off deployment:

1. Validate the TLS private key's file mode in `load_config`, matching the
   `& 0o027` check already applied to the HMAC command key (§4).
2. Cover `heartbeat_aging` and `heartbeat_stale` with injected clocks, and
   tighten `validated()` to `heartbeat_stale_seconds < socket_idle_seconds` so
   `heartbeat_stale` is actually reachable under the committed defaults (§5).
3. Prevent a superseded connection's `except` handler from writing `reason`
   over a live connection's, before `reason` is ever surfaced (§6).

Optionally, verify the signature before freshness and connection binding in
`protocol.verify` (§3).

`routes.insert(-3, …)` in `web/app.py` is positionally brittle; the path is
unique so ordering does not currently matter, but an explicit append would be
less fragile.

### Answers

1. **Merge Slice 2 alone.** It contains Slice 1 byte-for-byte (identical tree and
   patch-id). A separate Slice 1 merge is redundant.
2. **Yes — merge Slice 2 to `main` now, dormant.** It fast-forwards from
   `bcaab83`, drags in no historical lineage, and changes no active behavior with
   both gates off.
3. **No.** Hardware validation is not required before merge. It is required
   before enabling either gate.
4. **No.** Slice 3 may start before Slice 2 is hardware-validated.
5. **Yes.** Slice 3 may be implemented and merged dormant. Its *gates* must stay
   off until the ingress has carried a real request from a real agent, and its
   wire contract should be expected to change after that.
6. Safest sequence:
   1. Land the three §11 follow-ups on the Slice 2 branch.
   2. Merge Slice 2 to `main`, gates off. Do not deploy.
   3. Record in the deployment documentation that `install-beta1` must not be run
      from `main` until Slice 5 restores Admin UI parity.
   4. Implement Slice 3 (`desktop.app.list` / `.status` / allowlisted `.launch`)
      via `SkillRegistry` + `ActionCoordinator`, with a catalog-parity test.
      Merge dormant. Do not deploy.
   5. Build the isolated staging ingress (option B): separate install root,
      separate TLS keypair and port, separate loopback web daemon. Production
      `/opt/butters` untouched.
   6. Hardware-validate the real `DESKTOP-G4CFVL1` agent against staging only:
      hello, welcome, heartbeat, then a `desktop.app.list` request. Fix the wire
      contract as needed.
   7. Slice 5 — restore the Admin Tools UI against Slice 3's registered actions,
      recovering production feature parity.
   8. Only once parity is restored and validated: deploy from `main` with
      `install-beta1`, gates still off.
   9. Enable the two gates as a deliberate, separately-reviewed act.
   10. Slices 4, 6, and 7 thereafter. Do not enable `desktop.shutdown` to
       exercise any of this.
