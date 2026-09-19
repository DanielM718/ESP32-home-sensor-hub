# Admin AI and TTS configuration

Butters Admin is the authoritative control plane for the OpenAI credential,
the Butters Chat model and its request parameters, and text-to-speech.

This document records the design and the boundaries. It is reference for a
reviewer; the code is the enforcement.

## Why this exists

The previous Admin surface exposed model and voice as free text, showed a
speaking-style box regardless of whether the selected model consumed one, and
offered no way to see or manage the OpenAI credential at all. Three concrete
problems followed:

1. A capitalization mistake in a model or voice identifier was accepted by the
   form and only failed later, at the provider.
2. Nothing in the UI distinguished a stored setting from a running one, so
   Admin could display a voice that synthesis was not using.
3. The credential lived only in `/etc/butters/butters.env`, which the
   `butters-web` unit cannot write, so rotating it required shell access.

## Capability registry

`butters.ai.capabilities` is the single description of what exists.

```
Provider
 ├── chat models
 │    ├── reasoning efforts
 │    ├── sampling controls        (declared per model)
 │    ├── output controls
 │    └── tool / storage / cache controls
 └── speech models
      ├── voices
      ├── instructions support
      ├── speed support and range
      └── output formats
```

The chat catalog is **derived**, not listed: it is built from
`cloud.pricing`, the same mapping that authorizes a paid call. A model cannot
appear in the Admin dropdown unless Butters already holds reviewed pricing for
it, and a pricing change cannot leave the dropdown behind. This follows the
precedent set when the model-visible tool catalog stopped being a
hand-maintained list.

The browser consumes `GET /api/admin/ai/catalog`. `admin.js` contains no model
identifier, no voice name, and no numeric bound of its own, and a test asserts
that.

### Current catalog

| Provider | Chat models | Speech models |
| --- | --- | --- |
| `openai` | `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol` | `gpt-4o-mini-tts`, `tts-1`, `tts-1-hd` |
| `local` | *(none — the local LLM is a router, not a chat model)* | `local-piper` |

`gpt-4o-mini-tts` is the only speech model that consumes a speaking style, and
the only one carrying the expanded voice set (including Cedar and Marin).
`local-piper` declares exactly one voice, because the bundled Piper model
directory has exactly one speaker: offering a list there would be a lie.

No reviewed chat model accepts `temperature` or `top_p` — the GPT-5.6 family
rejects sampling controls — so those fields never render. The capability flags
exist so a future non-reasoning model turns them on by declaring support, not
by someone editing a template.

## Parameter semantics

* **Unset means unset.** A control the administrator has not set is stored as
  `null` and omitted from the provider request. It is never replaced by an
  invented default.
* **Unsupported means refused.** A parameter the selected model does not
  support is rejected by the server with a specific error code, not silently
  dropped. A silent drop would let Admin display a value the provider never
  receives.
* **Identifiers are canonical.** `Cedar` is not `cedar`. The browser sends a
  registry identifier or the request fails.

Normal chat controls: model, reasoning effort, maximum output tokens, response
verbosity (and temperature where a model supports it).
Advanced chat controls, collapsed by default: `top_p`, truncation, maximum
tool calls, parallel tool calls, response storage, prompt-cache hint.

Normal TTS controls: provider, model, voice, speed (slider bounded by the
model's own range), speaking style where supported, and Preview.
Advanced TTS controls: audio format.

## Credential storage

One credential class (`openai_api_key`), one destination, no generic secret
API.

```
/var/lib/butters/credentials/openai-api-key          0600
/var/lib/butters/credentials/openai-api-key.meta.json 0600
(directory)                                          0700
```

`/var/lib/butters` is the only path the `butters-web` unit can write
(`ProtectSystem=strict`, `ReadWritePaths=/var/lib/butters`). Writes are atomic:
a same-directory temporary file opened at mode 0600, then `os.replace`.

Resolution order is **stored file, then `OPENAI_API_KEY` from the unit
environment**. The existing `/etc/butters/butters.env` path is untouched:
Butters never reads it into the store, never rewrites it, and removal here
deletes only Butters' own copy. The reported `source` says which one is in
force.

### Replacement

```
candidate
   ↓
validate (GET /v1/models, then GET /v1/models/{configured model})
   ↓
valid? ── no ──▶ discard candidate; existing credential unchanged
   │
  yes
   ↓
atomic write ──▶ rebuild providers ──▶ activated?
                                        │
                              no ──▶ restore previous bytes and metadata,
                                     reinstall previous providers,
                                     report the activation failure
                                        │
                                       yes ──▶ report effective state
```

Validation is bounded and side-effect free. `GET /v1/models` is the
least-expensive authenticated read and creates no upstream object. Model
availability is asked separately, because *the credential authenticates* and
*this project can use that model* are different facts and are reported as two
fields.

### Removal is local only

"Remove from Butters" deletes the local credential. The response carries
`upstream_revoked: false` and the notice:

> This credential may still exist in your OpenAI account. Revoke it in the
> OpenAI Platform if it is no longer needed.

Revoking upstream would require an organization-level OpenAI Admin API key.
That is a broader credential than Butters needs for inference and speech, and
storing one here would mean a compromise of Butters could delete keys for
unrelated projects. It is deliberately out of scope. For deployment, use a
dedicated OpenAI **project** key scoped to Butters, with the minimum
permissions its actual use requires (model inference and audio speech).

### Authorization

Reading credential state needs an administrator identity. Mutating it needs,
in addition:

* an explicit confirmation (`confirm: true`);
* a FRESH WebAuthn assertion for the purpose `openai_credential`, bound to the
  subject `set` or `remove`, single-use and short-lived.

A grant collected for removal cannot authorize a replacement, and a replayed
grant is refused. Origin and CSRF checks are the existing ones on every Admin
mutation.

### Secret handling

The candidate arrives only in the JSON body of an authenticated, same-origin,
CSRF-checked POST. It is never a path or query parameter, never returned, and
is cleared from the browser field on success and on failure. It is not written
to `localStorage`, `sessionStorage`, IndexedDB, or a cookie, and is not held in
a JavaScript module variable.

Server side it is never logged, never placed in an audit entry, a job payload,
or an exception message. An OpenAI error body that quotes the rejected key back
is read and discarded rather than surfaced. Admin state carries a truncated
SHA-256 fingerprint, which identifies *which* credential is installed without
disclosing any part of it. A sentinel-value test suite asserts absence from
responses, logs, audit records, jobs, and exceptions.

## Saved, credential, effective

Three facts, reported separately and never collapsed:

| Fact | Meaning |
| --- | --- |
| `saved` | the durable row for the selected provider |
| `credential` | whether a credential exists and whether it authenticates |
| `effective` | what the live provider objects are using right now |

`in_sync` says whether they agree; `activation_error` says why not. A database
write alone is never reported as a configuration change. Every Butters Chat
turn reads `effective`, so Admin cannot display a voice that synthesis is not
using.

Each provider owns its own stored profile, so switching provider neither keeps
an incompatible model nor destroys a working configuration: switching back
restores exactly the last valid setup, speaking style included.

## Speech wiring

Before:

```
Admin "save as default preset" → voice_presets table
Butters Chat → POST /api/speech → voice_presets.default(static TOML)
```

The default came from a named-preset row that had to be explicitly saved, the
cloud model was pinned to `providers.cloud_tts_model` in `assistant.toml`, and
the style was sent to every model whether or not it consumed one.

After:

```
Admin TTS form
  → POST /api/admin/ai/tts   (capability-validated)
  → AISettingsStore (per-provider profile)
  → AIRuntimeController.effective.speech
  → preset_from_speech_settings()
  → synthesize_preview()  ← the one synthesis entry point
  → OpenAITTSProvider / LocalTTSProvider
```

Butters Chat (`POST /api/speech`) and Preview (`POST /api/admin/voice/preview`)
both enter at `synthesize_preview`. There is no second synthesis path, so a
preview cannot honour a setting that a spoken chat answer would ignore. Preview
is administrator-only, rate limited, and bounded to a short phrase.

`test_admin_tts_runtime.py` proves the wiring end to end: it sets the voice in
Admin, asks Butters Chat a question, requests the spoken answer, and asserts
the voice in the synthesis request that left the process — for two distinct
voices.

## Endpoints

| Method | Path | Authorization |
| --- | --- | --- |
| GET | `/api/admin/ai/catalog` | administrator |
| GET | `/api/admin/ai/settings` | administrator |
| POST | `/api/admin/ai/chat` | administrator mutation |
| POST | `/api/admin/ai/tts` | administrator mutation |
| GET | `/api/admin/integrations/openai` | administrator |
| POST | `/api/admin/integrations/openai/test` | administrator mutation |
| POST | `/api/admin/integrations/openai/key` | administrator mutation + confirm + FRESH |
| DELETE | `/api/admin/integrations/openai/key` | administrator mutation + confirm + FRESH |

## Boundaries preserved

No generic shell execution, no arbitrary configuration mutation, no
administrator-supplied secret file path, and no environment mutation through
Admin. Every endpoint has a closed request schema and refuses unknown fields.
Tools remains machine operations; configuration lives under Integrations and
TTS.
