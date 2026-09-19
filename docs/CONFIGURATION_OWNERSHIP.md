# Configuration ownership

Who owns which setting, and what a deployment is allowed to change.

## The defect this fixes

`install-beta1` replaces `/opt/butters` wholesale, including
`butters/config/`, with `rsync --delete`. Production approvals were stored in
`/opt/butters/config/assistant.toml`, so a deployment of *identical
application code* rewrote them to whatever the repository shipped.

Deploying `baf2203` did exactly that. Three approvals reverted:

```
[nas_agent_ingress]     enabled    true → false
[actions.nas_shutdown]  enabled    true → false
[actions.nas_shutdown]  configured true → false
```

The NAS Agent stopped authenticating (`WebSocket /nas-agent/v1/session 403`)
and Admin reported the NAS ingress as `not_configured`. Nothing warned; the
deployment reported success.

The root cause is ownership, not the three booleans. Any approval in that file
had the same exposure, in both directions: a deployment could silently revoke
one, and — because gates such as `desktop.shutdown_enabled` and
`actions.nas.enabled` ship `true` — it could silently **grant** one too.

## Layers

```
/opt/butters/config/assistant.toml   shipped defaults      replaced every deploy
            +
/etc/butters/assistant.local.toml    local approvals       never touched by deploy
            +
/etc/butters/butters.conf            machine values        never touched by deploy
            ↓
                        effective configuration
```

Precedence, lowest first: shipped TOML, then the production-local overlay
deep-merged over it, then the existing environment overlay
(`BUTTERS_ALLOWED_ORIGINS`, `BUTTERS_ADMIN_IDENTITIES`, `BUTTERS_STATE_DIR`,
`BUTTERS_REPOSITORY_ROOT`, `BUTTERS_CODEX_JOBS_DIR`,
`BUTTERS_PROJECT_INSPECTION_ROOT`). Merging happens on the raw tables before
any dataclass is built, so every existing validator sees one effective
configuration rather than two layers to reconcile.

The overlay is active only when `BUTTERS_LOCAL_CONFIG` names it. Both units
ship that line, so the path is version-controlled rather than hand-set, and
the installer refuses to publish a unit that lacks it. A developer checkout
and the test suite never inherit a machine's approvals.

## Ownership classes

| Class | Owner | Lives in | Examples |
| --- | --- | --- | --- |
| 1. Shipped application default | repository | `/opt/butters/config/assistant.toml` | timeouts, capacities, model IDs, pricing, prompt limits |
| 2. Environment / machine | machine | `/etc/butters/butters.conf` | `BUTTERS_ALLOWED_ORIGINS`, `BUTTERS_ADMIN_IDENTITIES`, state and repository paths |
| 3. Production-local approval | machine | `/etc/butters/assistant.local.toml` | the 41 gates in `GATE_KEYS` |
| 4. Secret / credential | machine | `/etc/butters/butters.env`, `/etc/butters/credentials/`, `/var/lib/butters/credentials/` | `OPENAI_API_KEY`, agent command keys |
| 5. Runtime / user preference | service | `/var/lib/butters/state.sqlite3` | Admin AI/TTS settings, voice presets |

Machine-specific *values* that are not approvals — the NAS LAN address, the
Tailscale hostnames, the two Jellyfin URLs — remain class 1 and stay in the
repository. They are reviewed, they are not secrets, and there is exactly one
NAS. Moving them would add churn without adding safety.

### The 41 production-local approvals

`butters.config_overlay.GATE_KEYS` is the authoritative list:

* ingress — `agent_ingress.enabled`, `nas_agent_ingress.enabled`
* desktop — `desktop.enabled` and the ten `desktop.*_enabled` capability gates
* host power — `actions.host_restart_butters_enabled`, `host_reboot_enabled`,
  `host_shutdown_enabled`
* devices — `enabled`, `configured`, `local_console_allowed` for `nas`,
  `nas_shutdown`, `heater`, `dehumidifier`, `ventilation`
* paid/cloud — `cloud.enabled`, `cloud.allow_paid_calls`,
  `providers.allow_paid_stt`, `providers.allow_paid_tts`
* other capabilities — `broker.enabled`, `portal.enabled`, `planner.enabled`,
  `llm.enabled`, `diagnostics.enabled`, `remediation.allow_codex_execution`

Only booleans qualify. An approval is a yes or a no; admitting free-form
values would recreate the arbitrary-configuration editor this design refuses.
`enabled`, `configured`, `local_console_allowed`, credential presence, and
runtime connection stay five separate facts — the overlay carries the first
three independently and says nothing about the last two.

### What the overlay may never touch

`authentication.enabled`, `web.development_mode`,
`web.trusted_tailscale_proxy`, `web.admin_identities`, and
`web.allowed_origins` are refused with a specific message. The overlay can
grant or revoke a *capability*; moving the authentication and identity
boundary stays a reviewed repository change.

## Fail-closed behaviour

| Condition | Result |
| --- | --- |
| overlay absent | shipped defaults; not an error |
| overlay unreadable | `LocalConfigError`, service stops |
| malformed TOML | `LocalConfigError`, service stops |
| key outside `GATE_KEYS` | `LocalConfigError`, naming the key |
| key in the authentication boundary | `LocalConfigError`, naming the boundary |
| non-boolean value | `LocalConfigError` |
| not owned by uid 0 | `LocalConfigError` |
| group-writable or world-readable | `LocalConfigError` |

Stopping — rather than ignoring the overlay and continuing on shipped
defaults — is the fail-closed choice precisely because several gates ship
enabled. Silently falling back could grant a capability an administrator had
revoked.

`REQUIRED_OWNER_UID` is a module constant so the test suite can exercise merge
behaviour as an ordinary user. The production default is asserted separately
against the unmodified constant.

## Deployment

`install-beta1` gained two steps, both **before** the swap, because the swap
is two atomic renames and cannot be meaningfully undone afterwards.

1. **migrate** (once per machine): any gate whose value in the deployed
   `/opt/butters/config/assistant.toml` differs from the incoming shipped
   default is written to `/etc/butters/assistant.local.toml`. The running
   configuration is the only input. Approval is never inferred from a service
   being reachable — a connected agent is evidence about the network, not
   about what an administrator allowed. An existing overlay is never
   overwritten; a machine with no deployed tree is a fresh install and gets no
   overlay at all.
2. **verify** (every time): compute the gates in force now and the gates the
   staged tree would produce, and refuse the deployment if any differ.
   `--allow-gate-changes` states a deliberate change on the command line.

The installer also refuses to publish a `butters-web.service` that does not
set `BUTTERS_LOCAL_CONFIG`.

Deliberately *not* done: preserving the previous `assistant.toml` wholesale.
That would pin shipped defaults forever and block legitimate configuration
evolution. Ownership is per key.

```
sudo ./butters/scripts/install-beta1 --start
  ...
  stage → build venv → verify deps → compile
  → migrate local approvals (once)
  → verify no gate moves           ← the baf2203 deployment stops here
  → check the unit names the overlay
  → seal → atomic swap → restart
```

## Files and modes

| Path | Owner | Mode | Replaced by deploy |
| --- | --- | --- | --- |
| `/opt/butters/config/assistant.toml` | `root:butters` | 0640 | yes |
| `/etc/butters/assistant.local.toml` | `root:butters` | 0640 | **no** |
| `/etc/butters/butters.conf` | `root:butters` | 0640 | no |
| `/etc/butters/butters.env` | `root:butters` | 0640 | no |
| `/var/lib/butters/**` | `butters:butters` | 0700 | no |

Systemd hardening is unchanged: `ProtectSystem=strict`,
`ProtectHome=read-only`, `NoNewPrivileges=true`, empty
`CapabilityBoundingSet`, and `/var/lib/butters` as the only writable path.
`/etc/butters` is readable but not writable by the service, which is what makes
the overlay a boundary the service cannot move.

## Pricing ownership

Model rates are **class 1**: reviewed repository configuration in
`butters/src/butters/pricing.py`, alongside `PRICING_SOURCE` and
`PRICING_DATE`, which are updated in the same change as the rates. They are
server-authoritative — no Admin form field can express a price, and a test
asserts that `ChatSettings` and `SpeechSettings` carry no price, rate, or cost
field.

Each model declares the dimension OpenAI actually bills:

| Model | Billing dimension |
| --- | --- |
| `gpt-5.6-luna` / `terra` / `sol` | input, cached input, output tokens (`TokenPricing`) |
| `tts-1`, `tts-1-hd` | input characters (`CharacterPricing`) |
| `gpt-4o-mini-tts` | text input tokens + audio output tokens (`SpeechTokenPricing`) |

A model with no entry is denied before any HTTP call, in both the chat and
speech paths.

### What a recorded cost claims

`provider_usage.cost_basis` says how much a figure should be trusted:
`provider_reported`, `input_measured`, `estimated_upper_bound`, `unavailable`,
or `unrecorded` for rows written before the column existed. Every field is
named `estimated_cost_usd`; the ledger is an estimate until reconciled against
OpenAI billing, and nothing in it claims to be a settled charge.

Recorded rows keep their request-time figure. They are a snapshot for audit,
not a recomputation, so a later rate change never rewrites history.

`POST /v1/audio/speech` returns audio bytes and no usage object, so for
`gpt-4o-mini-tts` neither billable dimension is observable. Its cost is
therefore an openly labelled ceiling (`estimated_upper_bound`,
`reconciliation_required: true`), never a measurement, and audio tokens are
never inferred from the encoded audio's size or duration — OpenAI defines no
such conversion. Budget admission uses that same ceiling, so a request whose
worst case exceeds the budget is refused rather than admitted on the grounds
that its true cost is unknowable.
