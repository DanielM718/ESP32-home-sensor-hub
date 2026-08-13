# Butters Beta 1 Web, Administration, and Deployment

Beta 1 is a separate Starlette/Uvicorn ASGI service. It does not run inside
the critical Flask sensor dashboard. Production traffic is:

```text
iPhone/desktop browser -> private Tailscale HTTPS Serve
                       -> 127.0.0.1:8090 Butters ASGI
```

Never enable Tailscale Funnel, router port forwarding, or a non-loopback
Butters bind. Browser HTTPS is required for microphone access and production
mutations. The application trusts `Tailscale-User-Login` only while configured
loopback-only behind Serve, then checks the exact administrator allow-list on
every privileged endpoint. Missing or unauthorized identity fails closed with a
bounded 403, including for the `/admin` document itself.

Loopback is a trust boundary, not a perimeter: any process that can reach
`127.0.0.1:8090` can present an identity header. Tailnet ACLs and host hygiene
remain part of the deployment's security, and a dedicated proxy-shared-secret
or UNIX-socket binding is the next hardening step.

## Interfaces

`/` is the mobile-first normal page: conversation, one text field, Send,
hold-to-talk microphone, state indicator, clear, and stop speech. Text and the
final STT transcript enter the same `BetaAssistantService.handle_text` path.
It never shows models, tokens, tool calls, costs, credentials, or internals.

`/admin` is server-authorized and provides overview, live structured traces,
bounded sessions, one-request route/model overrides, model/STT/TTS status,
voice previews/presets, inspectable/testable/toggleable read-only skills,
diagnostic tools, persistent usage/budgets, system/log/security status, and
review-gated Codex jobs. It never displays chain-of-thought or secret material.
The admin document is served only through the authorized route; `/assets`
exposes shared CSS/JS and nothing else.

Skills declare an audience as well as an action class. Administrator-sensitive
read-only observations — repository status and detailed network views — are
refused on the ordinary conversation surface before any adapter, diagnostic, or
cloud stage runs, and are never offered to a cloud model as a tool for a
non-administrator request. Host, stack, sensor, and printer observations remain
normal read-only skills.

Browser audio is incremental PCM S16LE at the browser's allow-listed native
sample rate (16/44.1/48 kHz), one or two channels. The backend explicitly
downmixes/resamples to the existing 16 kHz mono 20 ms `AudioFrame` boundary.
Frames, utterance duration, buffer, idle/session time, concurrency, and queues
are hard-bounded. Disconnect, cancel, recognizer failure, and worker saturation
all release the voice slot: teardown runs on a dedicated path that never waits
on the admission gate it is freeing. Audio is not persisted.

## Sessions and admission control

Session allocation is admission-controlled before anything is allocated:

- production requires a same-origin browser context (`Sec-Fetch-Site`) or an
  allow-listed `Origin`;
- creation is token-bucket rate limited per caller, keyed on the proxied
  tailnet identity rather than the shared loopback socket;
- one caller may hold at most `web.max_sessions_per_peer` conversations;
- `web.admin_session_reserve` slots are reachable only by an authorized
  administrator identity, so a session flood cannot lock the operator out;
- capacity returns through idle expiry. Live conversations are never evicted to
  make room for a new one; the new request is refused instead.

## State, privacy, and budgets

- application: `/opt/butters`;
- non-secret mutable state: `/var/lib/butters` mode 0700;
- usage ledger: `/var/lib/butters/usage.sqlite3`;
- voice presets: `/var/lib/butters/state.sqlite3`;
- Codex job metadata/worktrees: `/var/lib/butters/{skill-jobs.sqlite3,codex-jobs}`;
- secrets: `/etc/butters/butters.env` root:butters mode 0640;
- non-secret deployment overrides: `/etc/butters/butters.conf`.

Sessions and detailed traces are memory-only and bounded by both count and
time. Traces quote conversation text, so they expire after
`web.trace_ttl_seconds` and are dropped when their conversation is cleared or
expires. Full transcripts, prompts, responses, evidence, audio, and keys are
absent from the usage DB. Persistent rows contain IDs, route/provider/model
categories, actual provider token counts where supplied, costs, latencies, and
safe error codes. Unknown pricing fails closed. Request, daily, monthly,
output, retry, tool, cloud-round, escalation, and wall-time ceilings remain
enforced after restart. Detailed request/provider rows retain at most 50,000
entries by default; content-free daily spend rollups retain 400 days so pruning
cannot reset an active daily/monthly budget. The administrator usage report is
computed with bounded SQL aggregates and runs on a worker thread, so it never
stalls the event loop at retention scale.

Accounting limitations, stated precisely:

- The daemon serializes the paid permit/call/account sequence, so concurrent
  requests cannot race a stale balance. That serialization is held across the
  provider call, which bounds throughput for paid work.
- Per-request estimates are *conservative preflight reservations*, not
  mathematically hard ceilings across multi-round tool use: a tool-heavy cloud
  request can exceed its single-round estimate. Daily and monthly budgets are
  checked before each escalation and remain the effective control.
- A process crash between a provider call and the accounting write loses that
  charge. A reservation/pending-charge design would close this and is deferred.
- Paid text, STT, and TTS are all disabled by default for initial deployment.

## Install and configure

Prepare the isolated environment/models first as described in README, then:

```bash
sudo ./butters/scripts/install-beta1 --enable --start
sudoedit /etc/butters/butters.env
sudo systemctl restart butters-web.service
```

The installer stages a complete tree, byte-compiles it, seals its ownership and
permissions, and only then swaps it into place with atomic renames. A failure
before the swap leaves the running installation untouched. The replaced tree is
retained at `/opt/butters.previous` for rollback. Obsolete files cannot linger:
the staged copy is authoritative.

Sealing is what makes the tree usable by the unprivileged unit: a source
checkout is private to the developer (0700 directories, 0600 files), so the
installer resets the staged tree to `root:butters` with 0750 directories and
0640 files, keeping the execute bit only where one already existed. The service
user can traverse `/opt/butters`, exec `.venv/bin/python`, and read models,
native libraries, and static assets; nobody else has any access, and the tree
stays read-only to the unit. No manual `chmod` after installation is required
or expected.

Set `BUTTERS_ADMIN_IDENTITIES` to exact comma-separated
`Tailscale-User-Login` values. `OPENAI_API_KEY` may remain blank; deterministic
routing, diagnostics, browser local STT, and local TTS still work. Adding a key
does not enable spending: text, STT, and TTS each also require their explicit
non-secret allow-paid switch and known reviewed pricing in `assistant.toml`.
The API never returns any key or key prefix.

Private HTTPS exposure on this Pi's installed Tailscale CLI:

```bash
sudo tailscale serve --bg 8090
tailscale serve status
```

Then record that exact origin, which the daemon requires in production:

```bash
sudoedit /etc/butters/butters.conf     # BUTTERS_ALLOWED_ORIGINS=https://<host>.ts.net
sudo systemctl restart butters-web.service
./butters/scripts/verify-beta1
```

Until `BUTTERS_ALLOWED_ORIGINS` is set, `/readyz` reports `not_ready` with
`checks.production_origin = "unconfigured"` and the daemon refuses to issue
sessions or accept mutations. This is deliberate: production must not fall back
to comparing a client-supplied `Origin` against a client-supplied `Host`.
Do not run `tailscale funnel`. Tailnet ACLs should restrict access; application
admin authorization remains mandatory even inside the tailnet.

## Repository inspection

An ordinary deployed daemon has no repository. `/opt/butters` is an installed
tree, not a checkout, and the service deliberately has no read access to a
developer's private home directory; `ProtectHome=read-only` and
`ProtectSystem=strict` also mean it has no repository *write* authority.

`get_project_status` and the Codex skill builder therefore report
`repository_unavailable` unless `BUTTERS_PROJECT_INSPECTION_ROOT` (or
`remediation.project_inspection_root`) names a repository that is deliberately
readable by the `butters` service user. Unreadable, absent, and
foreign-ownership cases all surface as typed bounded failures, never as HTTP
500. Repository inspection is administrator-only when it is available at all.

## Codex skill jobs

The administrator submits 20–2,000 characters that must explicitly request a
read-only capability. Local validation rejects obvious control, disruption,
shell, write, credential, unrestricted-networking, deployment, and physical
phrasings. **This is a coarse pre-filter, not a security boundary**: it is
keyword-based and a determined phrasing evades it. The real controls are the
detached worktree, the path allow-list, the artifact and patch bounds, the test
gate, mandatory human review, and the fact that nothing is deployed.

A job records the clean base commit and uses a detached Git worktree.
`queued -> running` is a transactional compare-and-swap, so concurrent run
requests produce exactly one execution and a typed conflict; a refused attempt
returns the job to a retryable state and each attempt uses a fresh directory.

Before any patch is rendered, generated artifacts are inspected: non-regular,
executable, binary, oversized, and aggregate-oversized output is rejected so a
malicious file cannot force a large allocation. At approval time the persisted
diff is treated as untrusted input again — every target path is re-parsed and
re-checked against the canonical allow-list, and renames, copies, deletions,
symlink modes, executable modes, and binary patches are refused — before the
clean-base and `git apply --check` gates run. Nothing commits, pushes, deploys,
or restarts.

Codex receives `SKILL_DEVELOPMENT.md`, relevant examples, the untrusted request
as JSON data, and fixed security constraints. Its subprocess gets an explicit
minimal environment allow-list; `OPENAI_API_KEY`, MQTT/HA/database/admin and
other provider secrets are absent. Codex uses its independent existing login.
The in-daemon runner additionally refuses to start when its parent has a
recognized secret-bearing environment.

**Execution is disabled by default and must stay disabled.** The secret-free
parent environment is necessary but *not sufficient*, and the previous
documentation overstated it. Validation runs the generated pytest files, so
enabling execution means running model-authored code with the worker's full
ambient authority. Before `remediation.allow_codex_execution` may be turned on,
a dedicated worker must provide:

- a separate unprivileged UID with no `butters` group access to
  `/etc/butters` and no provider-secret group membership;
- a scrubbed `HOME`/`XDG_*`/Codex credential boundary;
- network isolation, including no access to the Butters web/admin socket;
- filesystem isolation with read-only mounts outside the worktree;
- explicit sandboxing of the test/execution step itself.

That worker is not built yet. Copying provider keys into Codex is forbidden.

## Verification and rollback

```bash
systemctl is-active butters-web.service
curl -fsS http://127.0.0.1:8090/healthz
curl -sS http://127.0.0.1:8090/readyz
journalctl -u butters-web.service --since '10 minutes ago' --no-pager
```

The service never owns physical ALSA; `butters-live`, WAV, wake-word, and CLI
flows remain separate. To stop exposure without deleting data:

```bash
sudo systemctl disable --now butters-web.service
```

Inspect `tailscale serve status` before changing proxy state. If Butters is the
only configured Serve handler, `sudo tailscale serve reset` removes it; do not
reset a node that serves unrelated applications.

To roll back application code:

```bash
sudo systemctl stop butters-web.service
sudo rm -rf /opt/butters && sudo mv /opt/butters.previous /opt/butters
sudo systemctl start butters-web.service && ./butters/scripts/verify-beta1
```

Do not delete `/var/lib/butters` if usage/budget history and presets are needed.

## Pi/iPhone acceptance checklist

- [ ] Private Tailscale HTTPS URL opens; no Funnel/public exposure exists.
- [ ] `BUTTERS_ALLOWED_ORIGINS` is set and `/readyz` reports ready.
- [ ] Normal page fits iPhone Safari and exposes no debug controls.
- [ ] iPhone Safari session startup succeeds (it must send `Sec-Fetch-Site`).
- [ ] Microphone permission succeeds.
- [ ] Hold/tap to speak works and debug mode shows changed partials.
- [ ] Final transcript enters the same assistant path as text.
- [ ] A box-three humidity query is deterministic and model-free.
- [ ] Local spoken response plays; Stop immediately halts browser playback.
- [ ] Network disconnect/reconnect obtains/reuses a bounded session safely.
- [ ] Repeated disconnects do not exhaust voice capacity.
- [ ] Unauthorized identity receives 403 for `/admin` and admin APIs.
- [ ] `/assets/admin.html` is not retrievable anonymously.
- [ ] A repository question from the normal page is refused, not answered.
- [ ] Forced configured cloud request reports model, effort, tokens, cost,
  tool calls, latency, and stopping reason without changing global defaults.
- [ ] Daily/monthly usage survives `systemctl restart butters-web`.
- [ ] The admin usage page returns promptly with a populated ledger.
- [ ] Local and configured cloud TTS previews work; presets contain no secrets.
- [ ] Skill metadata, permissions, examples, validation, toggle, and bounded
  test invocation are inspectable.
- [ ] Promoted host/stack/network observations either return data or report a
  typed unavailable result under the hardened unit; none raise.
- [ ] `remediation.allow_codex_execution` remains false.

## Tested versus manual

The repository test suite covers request/session/authorization, admission
control and administrator reserve, voice teardown release, deterministic and
diagnostic routing, overrides, cloud-disabled and budget behavior, persistent
privacy-preserving accounting at retention scale, audio conversion/limits,
WebSocket validation, production origin and cookie policy, skill audience
enforcement, promoted-skill degradation, Codex environment/request/Git/artifact/
patch/approval guards, routing corpora, diagnostic evaluation, and existing
printer behavior. Actual run results belong in the implementation report.

The checklist above requires manual validation on the real iPhone, microphone,
Tailscale HTTPS origin, local speaker/browser playback, configured paid
provider, and the hardened systemd sandbox. Unit/replay success is not a claim
those live paths were tested. In particular, promoted observations that depend
on `journalctl`, the tailscaled socket, or ICMP have not been exercised under
the real unit; they are designed to degrade to a typed unavailable result and
that degradation is what must be confirmed on the Pi.

Known Beta 1 limitations: push-to-talk uses the broadly supported legacy Web
Audio `ScriptProcessor` path for iPhone compatibility, and the browser's native
sample rate must be allow-listed (16/44.1/48 kHz) because there is no
client-side resampling fallback; cloud STT is a bounded provider adapter/status
surface but browser streaming defaults to the existing local engine; skill
enable/disable is process-local; no transcript long-term memory; Home Assistant
is health-only because no narrow state credential is available to Butters; no
automatic production deployment; and no control/action skills.
