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
every privileged endpoint. Missing or unauthorized identity fails closed.

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

Browser audio is incremental PCM S16LE at the browser's allow-listed native
sample rate (16/44.1/48 kHz), one or two channels. The backend explicitly
downmixes/resamples to the existing 16 kHz mono 20 ms `AudioFrame` boundary.
Frames, utterance duration, buffer, idle/session time, concurrency, and queues
are hard-bounded. Disconnect/cancel closes the recognizer and releases slots.
Audio is not persisted.

## State, privacy, and budgets

- application: `/opt/butters`;
- non-secret mutable state: `/var/lib/butters` mode 0700;
- usage ledger: `/var/lib/butters/usage.sqlite3`;
- voice presets: `/var/lib/butters/state.sqlite3`;
- Codex job metadata/worktrees: `/var/lib/butters/{skill-jobs.sqlite3,codex-jobs}`;
- secrets: `/etc/butters/butters.env` root:butters mode 0640;
- non-secret deployment overrides: `/etc/butters/butters.conf`.

Sessions and detailed traces are memory-only, bounded, and expire. Full
transcripts, prompts, responses, evidence, audio, and keys are absent from the
usage DB. Persistent rows contain IDs, route/provider/model categories, actual
provider token counts where supplied, costs, latencies, and safe error codes.
Unknown pricing fails closed. Request, daily, monthly, output, retry, tool,
cloud-round, escalation, and wall-time ceilings remain enforced after restart.
The daemon serializes the paid permit/call/account sequence across text, STT,
and TTS so concurrent requests cannot race a stale balance. An uncertain
provider failure is charged at the conservative preflight estimate.
Detailed request/provider rows retain at most 50,000 entries by default;
content-free daily spend rollups retain 400 days so pruning cannot reset an
active daily/monthly budget.

## Install and configure

Prepare the isolated environment/models first as described in README, then:

```bash
sudo ./butters/scripts/install-beta1 --enable --start
sudoedit /etc/butters/butters.env
sudo chown root:butters /etc/butters/butters.env
sudo chmod 0640 /etc/butters/butters.env
sudo systemctl restart butters-web.service
./butters/scripts/verify-beta1
```

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

The resulting URL is the HTTPS host shown by `tailscale serve status`. Do not
run `tailscale funnel`. Tailnet ACLs should restrict access; application admin
authorization remains mandatory even inside the tailnet.

## Codex skill jobs

The administrator submits 20–2,000 characters that must explicitly request a
read-only capability. Local validation denies control, disruption, shell,
writes, credentials, unrestricted networking, deployment, and physical
actions. A job records the clean base commit and uses a detached Git worktree.
Codex receives `SKILL_DEVELOPMENT.md`, relevant examples, the untrusted request
as JSON data, and fixed security constraints. Its subprocess gets an explicit
minimal environment allow-list; `OPENAI_API_KEY`, MQTT/HA/database/admin and
other provider secrets are absent. Codex uses its independent existing login.
The in-daemon skill runner additionally refuses to start when its parent has a
recognized secret-bearing environment, preventing same-user `/proc` recovery
of variables omitted from the child environment. A credential-bearing web
deployment therefore needs a separate secret-free Codex worker boundary before
programmatic execution can be enabled.

Execution is disabled by default. Enabling it is a separate reviewed
deployment change because the web daemon's production hardening deliberately
does not grant repository writes or Codex-auth access. Even when enabled, path
and patch-size bounds plus the full Butters suite and `git diff --check` gate a
patch. Completion produces `patch_ready`; only an explicit administrator
approval applies it to a still-clean matching base. Nothing commits, pushes,
deploys, or restarts. A distinct authenticated Codex worker user/service is a
future hardening step; copying provider keys into Codex is forbidden.

## Verification and rollback

```bash
systemctl is-active butters-web.service
curl -fsS http://127.0.0.1:8090/healthz
curl -fsS http://127.0.0.1:8090/readyz
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

To roll back application code, copy the prior reviewed `/opt/butters` tree (or
rerun the installer from the prior Git revision), then restart and verify. Do
not delete `/var/lib/butters` if usage/budget history and presets are needed.

## Pi/iPhone acceptance checklist

- [ ] Private Tailscale HTTPS URL opens; no Funnel/public exposure exists.
- [ ] Normal page fits iPhone Safari and exposes no debug controls.
- [ ] Microphone permission succeeds.
- [ ] Hold/tap to speak works and debug mode shows changed partials.
- [ ] Final transcript enters the same assistant path as text.
- [ ] A box-three humidity query is deterministic and model-free.
- [ ] Local spoken response plays; Stop immediately halts browser playback.
- [ ] Network disconnect/reconnect obtains/reuses a bounded session safely.
- [ ] Unauthorized identity receives 403 for `/admin` and admin APIs.
- [ ] Forced configured cloud request reports model, effort, tokens, cost,
  tool calls, latency, and stopping reason without changing global defaults.
- [ ] Daily/monthly usage survives `systemctl restart butters-web`.
- [ ] Local and configured cloud TTS previews work; presets contain no secrets.
- [ ] Skill metadata, permissions, examples, validation, toggle, and bounded
  test invocation are inspectable.
- [ ] A sample read-only Codex request yields a bounded tested diff, requires
  explicit approval, and does not deploy/restart.

## Tested versus manual

The repository test suite covers request/session/authorization, deterministic
and diagnostic routing, overrides, cloud-disabled and budget behavior,
persistent privacy-preserving accounting, audio conversion/limits, WebSocket
validation, secret redaction/status, Codex environment/request/Git/patch
guards, routing corpora, diagnostic evaluation, and existing printer behavior.
Actual run results belong in the implementation report/commit notes.

The checklist above requires manual validation on the real iPhone, microphone,
Tailscale HTTPS origin, local speaker/browser playback, configured paid
provider, and Codex login. Unit/replay success is not a claim those live paths
were tested.

Known Beta 1 limitations: push-to-talk uses the broadly supported legacy Web
Audio `ScriptProcessor` path for iPhone compatibility; cloud STT is a bounded
provider adapter/status surface but browser streaming defaults to the existing
local engine; skill enable/disable is process-local; no transcript long-term
memory; Home Assistant is health-only because no narrow state credential is
available to Butters; no automatic production deployment; and no
control/action skills.
