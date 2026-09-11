# Conversational Assistant and Action Planner Architecture

Status: design specification, revalidated against `main` at `7560842`.

Every claim below is classified: **present** in current `main`, **different**
in current `main`, or **proposed** and not built. Section 2.0 is the
authoritative classification table; the prose that follows it obeys that table.
Nothing here describes branch-only functionality as if it shipped.

The one exception is explicitly labelled: the planner foundation described in
sections 15 and 16 exists on the unmerged branch
`integration/conversational-planner-foundation`, and is called out as such
wherever it appears.

## 1. Purpose and non-goals

Butters already has a deterministic Action API: a typed skill registry, a
default-deny policy validator, immutable frozen action plans, passkey
authentication bound to a plan digest, an enumerated privileged broker, an
interactive Windows Desktop Agent, and a sanitized audit trail. Manual
`Admin -> Tools` controls already execute through it.

This document specifies how a conversational voice/text assistant sits *above*
that system without becoming a second execution path.

The planner is an intent mapper. It selects a goal and names registered
capabilities. It never executes anything, never learns an infrastructure
identifier, and never produces authority.

Non-goals for this design:

- replacing the deterministic router, skill registry, policy validator,
  coordinator, broker, or Desktop Agent protocol;
- adding any new privileged mechanism, credential, or execution surface;
- making a cloud model a dependency of Admin, the broker, or manual control;
- changing ESP32 firmware, InfluxDB startup behavior, the Desktop Agent wire
  protocol, or deployed production configuration.

## 2. Current architecture discovered

### 2.0 Classification against `main` at `7560842`

| Component | Status in `main` | Note |
| --- | --- | --- |
| `BetaAssistantService._handle_text_locked`, per-session `turn_lock` | present | one ordered turn pipeline for text, browser STT, live voice |
| `IntentRouter` / `RoutedIntent` incl. `action_plan` | present | stage 1 |
| bounded compound read planner | present | stage 2 |
| `SkillSpec` / `SkillRegistry` metadata and strict parsers | present | including `validate_action_intent` and `validate_observation_intent` |
| `PolicyValidator`, `ActionAuthorization`, `AuthenticationContext` | present | default deny; four independent checks |
| `ActionCoordinator.freeze_plan` / `execute`, digest-bound `FRESH` | present | 1–4 steps, ≤8 KiB, observation only as final step |
| `ActionStateStore`: plans, jobs, sanitized audit, overrides | present | |
| `PasskeyManager` / `AuthStateStore` | present | |
| `ActionBroker` and every `BrokerOperation` value | present | including `desktop.shutdown`, `host.*`, environment |
| `DesktopWorkflow` / `DesktopState` / `start_remote_session` | present | facets: network, ssh, parsec, observed |
| `PLAN_OBSERVATION_SKILLS` / `wait_for_desktop_reachability` | present | |
| `LocalVoiceAuthorization`, `local_console_allowed` | present | |
| `llm/catalog.py` policy, `DOCUMENTED_EXCLUSIONS`, `derive_safe_tool_catalog` | present | still omits every `ACTION` |
| `LanguageModel`, `CloudReasoner`, `GeneralCloudReasoner` | present | three provider abstractions |
| `UsageLedger`, `TraceStage` / `TraceBuffer`, diagnostics sanitizer | present | |
| STT / TTS / wakeword / live controller edge | present | |
| Windows Desktop Agent (`butters-agent/`, `actions/agent.py`, `AgentHub`) | **absent** | not in this repository's `main` |
| `actions/compute.py` (`DesktopActions`), `actions/streaming.py` (`StreamingWorkflow`) | **absent** | |
| `skills/desktop_agent.py`: `desktop.app.launch`, `desktop.vm.stop`, `desktop.agent.status`, `desktop.streaming.*` | **absent** | so no interactive application launch exists |
| `execute_desktop_action` / `_freeze_or_execute` / `TOOLS_REGISTERED_ACTIONS` / `TOOLS_CONFIRM_ACTIONS` | **absent** | there is no Admin Tools manual desktop-action path |
| `/api/desktop/*` routes, `/agent/v1/session` WebSocket, `ElevationRequired` | **absent** | |
| shared manual + spoken entry point | **different** | actions are frozen only from the deterministic route inside `handle_text`; there is one path, not two converging ones |
| desktop capability availability | **different** | committed config sets `shutdown_enabled`, `parsec_*_enabled`, `lock_enabled`, `sleep_enabled`, `restart_enabled` all `false`; those skills register but report unavailable |
| interactive-session / Desktop Agent state facets | **proposed** | no observer exists in `main` to produce them |
| `butters.planner` package, `/api/planner`, `PlannerValidator` | **branch only** | on `integration/conversational-planner-foundation`, not merged |


### 2.1 Turn resolution already exists and is ordered

`BetaAssistantService._handle_text_locked` in
`butters/src/butters/web/service.py` is the single ordered turn pipeline for
text, browser STT results, and live voice transcripts. It holds a per-session
`turn_lock`, normalizes the transcript, emits structured trace stages, and
walks the resolution order documented in `butters/ARCHITECTURE.md`:

1. deterministic `IntentRouter.route` (`butters/src/butters/routing/router.py`);
2. bounded compound planner for multiple independent read-only clauses
   (`butters/src/butters/routing/compound.py`);
3. a configured reasoning provider, if one is enabled;
4. a truthful fixed fallback that states plainly the request cannot be answered.

Production runs with `llm.enabled`, `cloud.enabled`, and `allow_paid_calls` all
false, so stage 3 is inert today and open-ended language reaches stage 4.

### 2.2 The capability model is already typed and metadata-rich

`SkillSpec` (`butters/src/butters/skills/registry.py`) already carries every
field a planner needs to reason safely without being trusted:

- `name`, `version`, `description`, `category`;
- `action_class`: `READ_ONLY`, `ANALYTICAL`, `ACTION`;
- `audience`: `NORMAL` or `ADMINISTRATOR`;
- `input_schema` / `output_schema` (strict, enum-bound, closed);
- `authentication`: `NONE`, `ELEVATED`, `FRESH`, plus `local_console_allowed`;
- `explicit_intent_required`, `confirmation_required`, `side_effects`;
- `configured`, `available`, `unavailable_reason`;
- `timeout_seconds`, `max_result_bytes`, `permission_summary`.

`SkillRegistry.validate_action_intent`, `validate_observation_intent`, and
`validate_proposal` are the existing typed validation entry points, and
`PLAN_OBSERVATION_SKILLS` already names the one allow-listed non-mutating plan
observation (`wait_for_desktop_reachability`).

### 2.3 Authorization is already separated into four boundaries

`PolicyValidator.authorize` (`butters/src/butters/skills/policy.py`) enforces,
in order and independently:

1. action class admitted by policy (default deny);
2. an `ActionAuthorization` naming the exact skill — `allowed_skills`,
   `source`, `confirmed` — which a model cannot construct;
3. `explicit_intent_required` satisfied only by `direct_user_request` or
   `confirmed_user_request`, and `confirmation_required` satisfied only by a
   confirmed authorization;
4. `AuthenticationLevel` checked against an `AuthenticationContext` that is
   bound to session, identity, expiry, and — for `FRESH` — the exact plan
   digest.

### 2.4 Frozen plans, jobs, and audit already exist

`ActionCoordinator.freeze_plan` (`butters/src/butters/actions/coordinator.py`)
canonicalizes typed arguments into a `PendingPlan`
(`butters/src/butters/actions/store.py`) with a random nonce, SHA-256 digest
over steps plus session plus identity plus nonce, expiry, and one-use state
(`pending_auth` or `pending_confirmation`). Current hard bounds:

- one to four steps, canonical JSON at most 8192 bytes;
- a plan must contain at least one action — an observation-only plan is refused;
- an allow-listed observation may only be the final step and contributes
  `AuthenticationLevel.NONE`, so it can never lower what the plan requires;
- the plan requires `FRESH` if any step does, otherwise `ELEVATED`.

`ActionCoordinator.execute` refuses a `FRESH` plan unless the assertion is
`FRESH` *and* `authentication.action_digest` equals the plan digest, claims the
plan one-use, creates a job, and runs steps on a worker with a cancel event.
`ActionStateStore.audit` writes hashed identity/session references, canonical
sanitized arguments, outcome, authentication level, method, job id, elapsed
time, and a reason code into a bounded table.

### 2.5 There is exactly one action entry point today

Current `main` has no Admin Tools manual desktop-action path:
`execute_desktop_action`, `_freeze_or_execute`, `TOOLS_REGISTERED_ACTIONS`, and
`TOOLS_CONFIRM_ACTIONS` are branch-only. Every privileged action is frozen from
the deterministic route inside `handle_text`, which calls
`ActionCoordinator.freeze` / `freeze_plan` directly and returns the pending plan
on `ServiceResponse.pending_action`.

The invariant the planner must preserve therefore reads slightly differently
than it would on the Desktop Agent branch: there is one path to an action, not
two converging ones, and a planner must join that same path rather than open a
second. If a manual Tools path is added later, it has to converge here too.

### 2.6 Multi-step composition already exists deterministically

Two reviewed compositions exist today and are produced by deterministic router
rules, not by a model:

- `RoutedIntent.action_plan` for "wake my desktop and tell me when it is
  reachable" — `wake_desktop` then `wait_for_desktop_reachability`;
- `RoutedIntent.action_plan` for the explicit computer-plus-environment case;
- `DesktopWorkflow.start_remote_session`
  (`butters/src/butters/integrations/desktop.py`), which owns sequencing,
  network and SSH readiness, separate polling deadlines, cancellation,
  structured stages, and error handling. (`StreamingWorkflow` is branch-only
  and absent from `main`.)

This is the pattern the planner must feed, not replace.

### 2.7 State is already observed as independent facets

`DesktopState` exposes `network_reachable`, `ssh_ready`, `parsec_ready`
(nullable) and `observed` — four independent facets, with `parsec_ready`
deliberately nullable so "not observed" is distinct from "not running". That is
the pattern this design extends.

No interactive-session or agent-liveness observer exists in `main`, because the
Desktop Agent is absent. Those facets in section 7 are therefore **proposed**,
not present, and nothing in `main` can supply them today.

### 2.8 Provider abstraction already exists three times

- `LanguageModel.propose_tools` (`butters/src/butters/llm/model.py`) returns a
  `ToolProposal` with `ProposalKind` of `TOOL`, `CLARIFICATION`, or
  `UNSUPPORTED`. It has no execution API.
- `CloudReasoner.analyze` (`butters/src/butters/cloud/model.py`) returns typed
  `ToolRequest` or `CloudConclusion`. It has no execution API.
- `GeneralCloudReasoner` (`butters/src/butters/cloud/general.py`) is the
  abstract general-chat provider with an `available()` predicate;
  `OpenAIGeneralReasoner` is one implementation.

`butters/src/butters/llm/catalog.py` already holds the model-visible capability
policy: `MODEL_SAFE_ACTION_CLASSES` admits only `READ_ONLY` and `ANALYTICAL`,
`model_eligible` categorically rejects `ACTION`, anything requiring
authentication, anything declaring side effects, anything requiring explicit
intent or confirmation, and the administrator audience;
`DOCUMENTED_EXCLUSIONS` plus a parity test prevent silent drift.

### 2.9 Voice is already a replaceable edge

`StreamingSTTEngine` owns the recognizer lifecycle, `LiveVoiceController` is a
per-frame state machine that owns no device, `BrowserAudioStream` normalizes
browser PCM, `LocalTTSProvider` / `OpenAITTSProvider` implement synthesis
behind a preset, and `WaveFileOutput` is a separate explicit sink. No STT or
TTS component imports the skill registry, coordinator, or broker.

`LocalVoiceAuthorization` (`butters/src/butters/live/authorization.py`) already
implements the physical-wake confirmation domain: `note_physical_wake()`,
a fixed affirmative/negative vocabulary, one pending plan per local voice
session, and cancellation on a new wake.

### 2.10 What is genuinely missing

A first foundation for items 2, 4, and 5 below now exists on the unmerged
branch `integration/conversational-planner-foundation` as the `butters.planner`
package (`model.py`, `provider.py`, `validator.py`) plus a `/api/planner`
endpoint. It is disabled by two independent gates and ships no provider. The
remaining items are still open. Nothing below exists in `main` itself:

1. a planner that may propose a *multi-step* goal from open language — today
   `action_plan` is populated only by hard-coded router rules, and
   `LanguageModel.propose_tools` returns exactly one tool call;
2. a proposal-only action catalog — `derive_safe_tool_catalog` categorically
   omits every `ACTION`, which is correct for an executing model but leaves a
   planner unable to even name "wake the desktop";
3. an assembled planner context of independently observed state facets;
4. a planner output schema and its deterministic validator;
5. a provider-neutral conversational planner interface with a null default;
6. utterance-level idempotency (the Desktop Agent has `idempotency_key` and the
   broker has replay protection, but a repeated sentence is not yet deduped).

## 3. Proposed architecture

### 3.1 Component diagram

```text
  microphone / browser mic / typed text
      |
      v
+-------------------------------------------------------------+
| EDGE (untrusted input, no privilege)                        |
|  AudioSource -> LiveVoiceController / BrowserAudioStream    |
|  -> StreamingSTTEngine -> normalize_transcript              |
+-------------------------------------------------------------+
      | utterance text + source + session
      v
+-------------------------------------------------------------+
| CONVERSATION LAYER (deterministic, existing)                |
|  BetaAssistantService.handle_text  (per-session turn_lock)  |
|   1 IntentRouter            -> answer or typed action route |
|   2 compound read planner   -> answer                       |
|   3 ConversationOrchestrator -> PLANNER (new, optional)     |
|   4 truthful fixed fallback -> answer                       |
+-------------------------------------------------------------+
      | PlannerRequest (text, state facets, catalog, auth state, context)
      v
+-------------------------------------------------------------+
| PLANNER (untrusted proposer; provider-neutral)              |
|  ConversationPlanner.propose()                              |
|   NullPlanner | LocalLlamaPlanner | OpenAIResponsesPlanner  |
|  returns ProposedPlan: goal, action id, params, prereqs,    |
|  confidence, clarification need, confirmation expectation   |
+-------------------------------------------------------------+
      | ProposedPlan (data only, no authority)
      v
+-------------------------------------------------------------+
| PLAN VALIDATOR + COMPOSER (deterministic, new, small)       |
|  PlanValidator: schema, allow-list, audience, availability  |
|  PlanComposer: reviewed prerequisite templates only         |
|   -> ((skill, args), ...) ordered steps, <= 4               |
+-------------------------------------------------------------+
      v
+=============================================================+
| TRUST BOUNDARY: everything below is authoritative           |
+=============================================================+
|  SkillRegistry.validate_action_intent                       |
|  ActionCoordinator.freeze_plan -> PendingPlan (nonce/digest)|
|  confirmation (browser dialog | local voice affirmative)     |
|  PasskeyManager assertion -> AuthenticationContext          |
|  ActionCoordinator.execute -> PolicyValidator -> skill      |
|  ActionStateStore: jobs + sanitized audit                   |
+-------------------------------------------------------------+
      v
+-------------------------------------------------------------+
| DETERMINISTIC EFFECTORS (existing, unchanged)               |
|  ActionBroker (enumerated ops, SO_PEERCRED, root gates)     |
|  DesktopWorkflow (WOL, readiness, remote session)           |
|  Desktop Agent + StreamingWorkflow (proposed; not in main)  |
|  Home Assistant / MQTT / WOL / SSH adapters                 |
+-------------------------------------------------------------+
```

### 3.2 Trust boundaries

| Boundary | Authenticates | Carries | Never carries |
| --- | --- | --- | --- |
| Browser domain | Tailscale identity allow-list, session cookie, allow-listed HTTPS `Origin`, CSRF token, passkey elevation | session, identity, plan ids, confirmations | machine credentials |
| Machine-agent domain (**proposed**; absent from `main`) | SHA-256 agent token plus signed frames on a LAN listener | registered action invocations, heartbeats | browser sessions, passkey elevation |
| Broker domain | `SO_PEERCRED` peer UID, protocol version, replay window, plus a second root-owned per-operation gate | one enumerated operation and a request id | argv, shell text, host, MAC, path, service, entity, topic, payload |
| Planner domain | nothing; the planner is never an authenticated principal | an utterance, sanitized state facets, a capability catalog, a returned proposal | credentials, addresses, MACs, SSH users or keys, tokens, entity ids, tool results marked administrator |

Two rules follow and must be stated in code review terms:

- **Browser authentication and machine-agent authentication remain separate
  security domains.** An agent token never authorizes a browser route, and a
  passkey elevation never authorizes an agent frame. The existing agent hub
  comment already asserts this; the planner adds no path between them.
- **The planner is a data producer, not a principal.** Every planner output
  re-enters typed parsing, the allow-list, the audience check, the policy
  validator, the freeze step, and the authentication ceremony. A syntactically
  perfect proposal has exactly the authority of a user typing the same words.

### 3.3 Request lifecycle

```text
 1 utterance      Voice: wake -> STT -> normalized final transcript
                  Text:  POST /api/chat (session + CSRF + Origin)
 2 conversation   handle_text acquires the session turn_lock,
                  claims the interaction generation, opens a trace
 3 deterministic  IntentRouter; a matched read-only route answers here
                  and never reaches the planner
 4 action route   a matched ACTION route freezes immediately through the
                  existing path; the planner is not consulted
 5 planner        only an unmatched, planner-eligible turn reaches
                  ConversationOrchestrator, and only if a provider is
                  configured and enabled
 6 proposal       ProposedPlan returned as data; traced as a proposal,
                  never as a decision
 7 validation     PlanValidator + PlanComposer produce ordered steps or a
                  typed rejection
 8 freeze         ActionCoordinator.freeze_plan -> PendingPlan; the
                  response describes the frozen plan in fixed local text
 9 confirmation   browser dialog on the plan id, or a deterministic
                  affirmative inside the same physical voice session
10 authentication ELEVATED reuses live elevation; FRESH requires a passkey
                  assertion bound to this plan digest
11 execution      ActionCoordinator.execute -> PolicyValidator -> skill ->
                  broker / agent / adapter, as a job with cancellation
12 observation    job stages and independent state reads; the response
                  reports what was observed, not what was requested
13 audit          utterance, proposal, frozen plan, execution, and observed
                  outcome are five distinct records
```

Steps 3, 4, 7 through 13 are existing behavior. Steps 5, 6, and the composer
half of 7 are new.

### 3.4 Conversation request model

```python
@dataclass(frozen=True, slots=True)
class ConversationRequest:
    utterance: str              # normalized transcript, 1..4000 chars
    source: str                 # "text" | "browser_voice" | "local_voice"
    session_id: str             # browser session, or the local voice session
    identity: str               # peer identity key; never an email in logs
    administrator: bool         # audience, from the existing session flag
    physical_session: bool      # a live wake-bound local voice interaction
    interaction_generation: int | None
    request_id: str             # uuid4, carried into plan and audit
    received_at: float
```

This mirrors what `handle_text` already receives; it exists so the planner
layer has one typed input instead of a growing keyword argument list.

### 3.5 Planner input

`PlannerRequest` is assembled by deterministic code and is the *only* thing a
provider sees.

```python
@dataclass(frozen=True, slots=True)
class PlannerRequest:
    utterance: str
    conversation: tuple[PlannerTurn, ...]     # <= 4 turns, <= 2000 chars each
    state: StateSnapshot                      # section 7
    catalog: tuple[PlannerCapability, ...]    # <= 48 entries
    authorization: AuthorizationSnapshot
    budget: PlannerBudget
```

```python
@dataclass(frozen=True, slots=True)
class PlannerCapability:
    action_id: str                 # the registered skill name, verbatim
    summary: str                   # SkillSpec.description
    parameter_schema: dict         # SkillSpec.input_schema, unchanged
    safety_class: str              # section 5, derived
    confirmation_expected: bool    # SkillSpec.confirmation_required
    authentication_expected: str   # "none" | "elevated" | "fresh"
    side_effects: str              # SkillSpec.side_effects
    available: bool                # SkillSpec.available and configured
    unavailable_reason: str | None
    prerequisites: tuple[str, ...] # enumerated prerequisite names only
```

```python
@dataclass(frozen=True, slots=True)
class AuthorizationSnapshot:
    administrator: bool
    elevation_active: bool          # an ELEVATED action can run without a new
                                    # ceremony; the planner may say so
    fresh_required_actions: tuple[str, ...]
    physical_session: bool
```

Rules for planner input:

- the catalog is built by a **new** `planning/catalog.py`, deliberately
  separate from `llm/catalog.py`. `llm/catalog.py` answers "what may a model
  *call*" and keeps excluding every `ACTION`. The planner catalog answers "what
  may a model *name*", and it is proposal-only: entries carry no executable
  binding at all. The parity-test discipline of `DOCUMENTED_EXCLUSIONS` is
  reused, with its own exclusion list and its own parity test.
- administrator-audience capabilities appear only when
  `AuthorizationSnapshot.administrator` is true, so the audience boundary is
  enforced before the provider call, exactly as the registry, orchestrator, and
  cloud tool filter already do.
- the snapshot is sanitized by the existing diagnostics sanitizer
  (`butters/src/butters/diagnostics/sanitizer.py`). No MAC, IP, SSH user, key
  path, Home Assistant entity id, topic, token, or broker socket path is ever
  serialized into a planner request. The desktop is `"desktop"`; the outlet is
  `"monitors"`.
- prior conversational context is the existing bounded session history. Raw
  request strings are never concatenated into a new utterance, matching the
  existing clarification rule.

### 3.6 Planner output schema

The provider returns structured intent. It never returns code, a command, a
URL, an address, or a step ordering for infrastructure.

```python
class PlannerOutcome(str, Enum):
    PLAN = "plan"                  # a goal the planner believes is intended
    CLARIFICATION = "clarification"
    ANSWERABLE_READ_ONLY = "read_only"   # defer to a read-only capability
    UNSUPPORTED = "unsupported"

@dataclass(frozen=True, slots=True)
class ProposedStep:
    action_id: str                 # must be a catalog action_id
    parameters: dict[str, object]  # must satisfy that action's input_schema

@dataclass(frozen=True, slots=True)
class ProposedPlan:
    outcome: PlannerOutcome
    goal_summary: str                       # <= 200 chars, user-facing intent
    steps: tuple[ProposedStep, ...]         # <= 4; usually exactly one
    prerequisites: tuple[str, ...]          # enumerated names, section 4
    confidence: str                         # "high" | "moderate" | "low"
    ambiguity: tuple[str, ...]              # candidate readings, <= 4
    clarification_question: str | None      # only when CLARIFICATION
    clarification_necessary: bool           # see below
    expects_confirmation: bool              # planner's belief, never authority
    rationale: str                          # <= 400 chars, for the audit trail
```

Field semantics that matter:

- `steps` name registered capabilities only. A step is a *selection*, not an
  instruction; `parameters` are re-parsed by the registry's strict parser and a
  missing, extra, or ill-typed key is a rejection, not a repair.
- `prerequisites` are enumerated names from a fixed vocabulary (section 4). The
  planner may say "this needs the machine awake and the interactive agent
  present". It may not say how to achieve that.
- `clarification_necessary` exists because the common failure of a
  conversational layer is asking when it already knows. It is honored only when
  `outcome` is `CLARIFICATION` *and* the deterministic validator agrees that a
  required parameter is genuinely unresolved or that `ambiguity` names two or
  more distinct registered capabilities. A planner that asks about something
  the registry can already resolve is overridden and the turn proceeds.
- `expects_confirmation` is advisory. Whether confirmation is required is
  decided by `SkillSpec.confirmation_required`, `TOOLS_CONFIRM_ACTIONS`, and
  the coordinator's plan state. A planner saying `false` for a shutdown changes
  nothing.
- `confidence` is categorical, as in `Confidence` for diagnostics. No numeric
  score is invented, and low confidence never lowers an authentication
  requirement — it only affects whether the plan is proposed at all or the turn
  degrades to clarification.

Validation, in `planning/validator.py`, rejects with a typed reason code:
unknown `action_id`; an action absent from the catalog actually sent; an
administrator action for a non-administrator turn; `available` or `configured`
false; a disabled skill; more than four steps; an observation that is not the
final step; a plan with no action; parameters failing `strict_arguments`; a
prerequisite outside the vocabulary; a duplicate action within one plan.

### 3.7 Execution orchestration and composition

The planner selects a goal. A deterministic `PlanComposer` turns a goal plus
declared prerequisites into ordered steps, using **reviewed templates only**.
This is the answer to "the LLM should not invent polling commands or
infrastructure details".

Worked example: "Open Parsec on my desktop."

```text
planner output
  outcome: PLAN
  goal_summary: "open Parsec on the desktop"
  steps: [ { action_id: "desktop.app.launch", parameters: {app: "parsec"} } ]
  prerequisites: ["machine_powered", "interactive_agent"]
  confidence: high
  clarification_necessary: false
```

The composer then consults the independently observed `StateSnapshot` and
selects one reviewed template:

This example is **proposed**. In current `main` none of it is reachable:
`desktop.app.launch` does not exist, and `ensure_parsec_running` /
`restart_parsec` are registered but disabled in committed configuration. The
table describes the target once an interactive-session capability exists.

| Observed state | Composed steps |
| --- | --- |
| network reachable, interactive agent present | `desktop.app.launch{app=parsec}` (**proposed capability**) |
| network reachable, agent stale or disconnected | refuse with `interactive_agent_absent`; report the named agent state, do not wake or retry blindly |
| not reachable | a single reviewed workflow action that owns WOL, readiness, and the launch — not a planner-assembled step list |
| reachable, Parsec already running (`parsec_ready` true) | answer that it is already running; propose `restart_parsec` only if the user asked for a restart, and only where it is enabled |

Both branches stay inside the existing four-step plan bound and the existing
rule that an observation may only be the final step. Where a composition would
need more than that — wake, observe, then act — the reviewed workflow object is
the right unit: `DesktopWorkflow.start_remote_session` already owns
sequencing, readiness polling, separate deadlines, cancellation, and structured
stages behind *one* registered action.

The composition rule, stated once: **a multi-stage physical sequence is a
registered workflow action, not a planner-assembled step list.** The planner's
contribution is naming the goal and declaring which prerequisites it believes
are unmet; the composer decides whether that maps to one workflow action, a
reviewed two-step plan, or a refusal.

Templates live in code, are enumerated, and are covered by tests. Adding one is
the same reviewed act as adding a skill.

## 4. Prerequisite vocabulary

A closed set, versioned with the planner catalog. Each name has a
deterministic observer and a deterministic resolution policy.

| Prerequisite | Observed by | Resolution policy |
| --- | --- | --- |
| `machine_powered` | `DesktopState.network_reachable` plus job history | `wake_desktop`, then observe; never assume WOL succeeded |
| `machine_network` | `DesktopState.network_reachable` | observe only; no remediation |
| `machine_ssh` | `DesktopState.ssh_ready` | observe only; ping is never treated as readiness |
| `interactive_agent` | **proposed**: no observer in `main` | never remediated automatically; an absent agent is reported |
| `parsec_service` | `DesktopState.parsec_ready` | `desktop.parsec_ensure` where enabled; idempotent by design |
| `elevation` | `AuthStateStore.elevation` | the existing browser ceremony |
| `fresh_authentication` | plan requirement | the existing passkey ceremony bound to the plan digest |
| `device_reachable` | IoT adapter freshness | observe only; stale state fails closed |

A planner declaring an unknown prerequisite is rejected.

## 5. Safety classes

No new authority enum is introduced. The five classes are a **derived label**
used for explanation, planner catalog annotation, confirmation language, and
audit grouping. They never grant anything.

One property of the existing code must be stated first, because it changes how
the derivation has to work. Every action registered through the shared
`action()` helper in `skills/actions_v2.py` sets both
`explicit_intent_required=True` and `confirmation_required=True`, and
`ActionCoordinator._run_plan` always constructs its `ActionAuthorization` with
`confirmed=True`, setting `source` to `confirmed_user_request` when the plan
state is `pending_confirmation` and `direct_user_request` otherwise. So
`SkillSpec.confirmation_required` is a floor that every action shares; it does
not by itself separate a wake from a shutdown.

What actually discriminates is:

- `action_class` — `ACTION` versus `READ_ONLY`/`ANALYTICAL`;
- `authentication` — `ELEVATED` versus `FRESH`;
- `audience` — `NORMAL` versus `ADMINISTRATOR`;
- the plan state, `pending_confirmation` versus `pending_auth`, which is chosen
  by a reviewed membership set (`TOOLS_CONFIRM_ACTIONS` today) and is what makes
  a user explicitly confirm the frozen plan;
- whether the effect is recoverable, which registry metadata does not encode.

The derivation therefore uses those fields plus one explicit reviewed
membership set per disruptive class. That is deliberate: pretending the label
can be inferred from existing metadata alone would be the kind of quiet
mis-classification this design exists to prevent.

| Class | Derivation | Current members | Execution requirements |
| --- | --- | --- | --- |
| `OBSERVATION` | `action_class != ACTION` | all read-only and analytical skills, including `get_desktop_status`, `get_parsec_status`, `desktop.agent.status`, `wait_for_desktop_reachability` | none beyond the audience check; the default-deny policy already admits only these |
| `REVERSIBLE` | `ACTION`, `ELEVATED`, not in the confirm set | `wake_desktop`, `monitors_on`, `monitors_off`, `wake_nas` | frozen plan plus live elevation; no separate confirmation step |
| `STATE_CHANGING` | `ACTION`, `ELEVATED`, in the confirm set, or carrying a timed override | environment `heater` / `dehumidifier` / `ventilation` set operations | frozen plan, explicit confirmation of that plan, elevation, and the persisted-override release path |
| `DISRUPTIVE` | `ACTION`, `FRESH`, recoverable without data loss, named in the reviewed disruptive set | `ensure_parsec_running`, `restart_parsec`, `lock_desktop`, `sleep_desktop` — all currently disabled in committed configuration | frozen plan, explicit confirmation, a passkey assertion bound to the plan digest |
| `SECURITY_SENSITIVE` | `ACTION`, `FRESH`, powers off or reboots a host, or is gated by a default-off root broker enable | `shutdown_desktop`, `restart_desktop`, `restart_butters_service`, `host.reboot`, `host.shutdown` | frozen plan, explicit confirmation, a digest-bound passkey assertion, **and** the root broker's own per-operation gate |

Consequences that the planner cannot negotiate:

- the label is computed from the registry and the reviewed sets, so a planner
  cannot assert a lower class. A new capability with missing metadata is
  refused registration outright, and a new capability absent from both
  disruptive sets classifies no lower than `STATE_CHANGING`, so omission fails
  toward caution rather than away from it;
- a `SECURITY_SENSITIVE` or `DISRUPTIVE` action is never reachable from the
  local voice path without a passkey device, matching the existing rule that
  `FRESH` actions always direct the user to a passkey. `LocalVoiceAuthorization`
  admits only steps that declare `local_console_allowed` and refuses every
  `FRESH` step outright;
- the desktop shutdown gate structure is preserved exactly: the capability
  switch in `assistant.toml`, the root broker's own default-off gate, the
  frozen plan, the `pending_confirmation` state, and the `FRESH` assertion bound
  to the digest. The planner adds nothing to that chain and removes nothing
  from it;
- most of these actions ship disabled or unavailable in current `main`:
  committed configuration sets `shutdown_enabled`, `parsec_status_enabled`,
  `parsec_ensure_enabled`, `parsec_restart_enabled`, `lock_enabled`,
  `sleep_enabled`, and `restart_enabled` to `false`, and the host power
  operations and environment actuators are off as well. Only `wake_desktop`,
  the two monitor operations, and the read-only observations are live. The
  planner catalog carries `available` and `unavailable_reason` straight from the
  registry, so an unavailable capability is reported as unavailable rather than
  proposed and then failed.

## 6. Confirmation language and lifecycle

Confirmation state is owned by deterministic code. The model may only explain.

```text
plan frozen (state = pending_confirmation)
  -> deterministic code renders the confirmation prompt from the plan itself:
     the composed step list, the derived safety class, the named side effects,
     and what will not happen
  -> the planner may supply goal_summary as a human-readable preamble; it is
     rendered as a quoted restatement of intent, never as the authoritative
     description of effects
  -> confirmation arrives as one of exactly two deterministic forms:
       browser: an explicit POST naming the plan id, with session and CSRF
       local voice: a fixed affirmative inside the same physical session, via
                    LocalVoiceAuthorization, without a new wake phrase
  -> anything else does not confirm: stale text, another session, browser text
     answering a voice plan, a model-produced string, a repeated utterance
  -> a new wake cancels the pending local confirmation (existing behavior)
  -> the plan expires on its own deadline and is one-use
```

The planner never sees a plan id, never receives the confirmation event, and is
not consulted again between confirmation and execution. If the planner's
`goal_summary` and the composed steps disagree, the composed steps are
authoritative and the mismatch is a rejection, not a reconciliation.

## 7. State handling

`StateSnapshot` keeps facets independent. There is no `online` field, and the
planner cannot construct one, because no boolean in the snapshot spans layers.

```python
@dataclass(frozen=True, slots=True)
class StateFacet:
    name: str
    value: str            # a named state; a bare bool would lie here
    confidence: str       # "observed" | "inferred" | "unknown" | "stale"
    observed_at: float
    age_seconds: float | None

@dataclass(frozen=True, slots=True)
class StateSnapshot:
    facets: tuple[StateFacet, ...]
    assembled_at: float
    incomplete: tuple[str, ...]   # facets that could not be observed
```

Facets, each from its existing independent observer:

| Facet | Values | Source |
| --- | --- | --- |
| `desktop.power` | `on`, `off`, `unknown`, `wake_requested` | WOL job history plus reachability; never asserted from a sent packet |
| `desktop.network` | `reachable`, `unreachable`, `unknown` | `DesktopState.network_reachable` |
| `desktop.ssh` | `ready`, `not_ready`, `unknown` | `DesktopState.ssh_ready` |
| `desktop.interactive_session` | `present`, `absent`, `unknown` | **proposed**: no observer exists in `main` |
| `desktop.agent` | `not_configured`, `disconnected`, `awaiting_heartbeat`, `heartbeat_stale`, `heartbeat_aging`, `connected` | **proposed**: the Desktop Agent is absent from `main` |
| `desktop.parsec` | `running`, `not_running`, `unknown` | `DesktopState.parsec_ready`, which is nullable for a reason |
| `iot.<device>` | `on`, `off`, `unavailable`, `stale` | environment adapter plus freshness |
| `sensor.<entity>` | `fresh`, `stale`, `missing` | existing dashboard adapter freshness policy |

Rules:

- `unknown` is never rendered to the planner as `off`, and `stale` is never
  rendered as a current value. The existing diagnostics discipline that
  unobservable state is not proof of failure applies unchanged.
- a facet the planner needs but that is `unknown` or `stale` makes the relevant
  prerequisite unresolved, which is a legitimate reason to observe before
  acting or to report rather than act.
- the snapshot is read-only and assembled fresh per planner call, with the
  existing short-lived adapter caches. Assembling it never mutates anything and
  never wakes anything.

## 8. Failure semantics

Every row below is reported as what was observed, distinct from what was
requested. None of them is retried by the planner; retry policy is
deterministic where it exists at all.

| Situation | Deterministic behavior | Reported as |
| --- | --- | --- |
| WOL sent, machine not yet reachable | the frozen plan's final allow-listed observation polls under fixed local deadlines | `wake_requested`, reachability `not_yet_observed` with the deadline; not "the desktop is on" |
| Reachable but Desktop Agent absent | refuse the interactive step; no automatic remediation | the named agent state (`disconnected`, `heartbeat_stale`, ...), never "offline" |
| Parsec already running | no action; the idempotent `parsec_ensure` is still safe if explicitly requested | `already_running` |
| MQTT/IoT device unavailable | fail closed; missing or stale required safety data blocks the action | `device_unavailable` or `state_stale` with age |
| Action succeeded, observation not yet confirming | job `completed`, state facet `inferred`/`unknown` | two separate statements: the action was accepted, the state is not yet observed |
| Stale sensor state | the existing freshness policy; no interpolation | `stale` with age, and the request is answered about staleness |
| Provider unavailable, timeout, malformed output, quota exhausted | no plan is produced; the turn degrades to the existing stage 4 truthful fallback | "I can't resolve that with the local capabilities currently enabled" |
| Validator rejection | typed reason code, traced and audited; no freeze | a fixed local message per reason code, never the provider's text |
| Confirmation expires, or a new wake arrives | the plan is one-use and expires; nothing runs | `plan_expired` or `superseded` |
| Broker unavailable or operation gate off | existing fail-closed behavior | `unavailable_reason` from the registry, surfaced before the button or the sentence |

## 9. Idempotency

Repeated natural language must not cause duplicate side effects. Four layers,
three of which already exist:

1. **Utterance dedupe (new).** A per-session bounded window keyed by
   `(session_id, canonical_utterance, composed_steps_digest)`. Inside the
   window, a repeat returns the *existing* pending plan or the existing job
   instead of freezing a second plan. The window is short and is not a cache of
   answers, only of action identity.
2. **One-use frozen plans (existing).** A `PendingPlan` is claimed once;
   `store.claim` plus the allowed-state check means a replayed confirmation
   cannot run twice.
3. **Digest-bound `FRESH` assertions (existing).** An assertion cannot be
   replayed against a different plan, and cannot create general elevation.
4. **Effector idempotency (existing).** The broker validates a request id
   against a replay window; `desktop.parsec_ensure` is idempotent by design;
   monitor operations poll to a settled state rather than toggling. (The
   Desktop Agent's per-request `idempotency_key` is branch-only.)

Layer 1 is **not implemented** in the planner foundation on
`integration/conversational-planner-foundation`. Layers 2 to 4 are, because
they are the existing machinery the planner is required to go through. With
that branch's catalog — desktop status, wake, shutdown — a repeated utterance
either re-reads status, freezes a second wake plan that the operator's existing
elevation may run twice, or freezes a second shutdown plan that still requires
its own confirmation and its own digest-bound passkey assertion. A duplicate
wake is idempotent in effect. Utterance-level dedupe is therefore a follow-up
rather than a merge blocker at this catalog size, and becomes a blocker as soon
as a non-idempotent `REVERSIBLE` action joins the catalog.

Naturally idempotent goals ("wake my desktop" when it is already awake) are
answered from observed state without freezing a plan. Naturally
non-idempotent, disruptive goals always require a fresh confirmation, so a
repeat is a new deliberate act rather than a silent duplicate.

## 10. Provider abstraction

```python
class ConversationPlanner(ABC):
    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def propose(self, request: PlannerRequest) -> PlannerResult: ...

    def close(self) -> None: ...
```

`PlannerResult` wraps `ProposedPlan` with provider metadata only: model name,
elapsed seconds, token counts, and a bounded raw-output field used for traces
and never for execution. This deliberately mirrors `LanguageModelResult` and
`CloudTurn` so the existing usage ledger records the same non-content shape.

Implementations:

- `NullPlanner` — the default and the production default. `available` is
  `False`; `propose` is never called. This is what "no `OPENAI_API_KEY`" means
  in practice: stage 5 does not exist and the turn reaches stage 4.
- `LocalLlamaPlanner` — reuses `llm/llama_server.py` and the restricted parsing
  in `llm/parsing.py`. Subject to the same measured resource gates recorded in
  `butters/benchmarks/llm.md`; currently no candidate passes on this Pi.
- `OpenAIResponsesPlanner` — reuses `cloud/openai_responses.py` request
  construction, the strict flat function-definition style, `store=false`,
  `cloud/usage.py` budget permit/record, and `cloud/routing.py` escalation
  policy. Requires `cloud.enabled`, `allow_paid_calls`, a reviewed price, and
  `OPENAI_API_KEY`, exactly as today.

Provider-neutrality requirements:

- no provider object crosses the `ConversationPlanner` boundary, matching the
  existing `CloudReasoner` rule;
- the planner interface has no execution method, no adapter handle, no
  credential, and no registry reference;
- a planner is constructed by the service and may be absent entirely. Absence
  is a supported, tested configuration, not a degraded one;
- the planner's own configuration lives in a new `[planner]` section of
  `assistant.toml` with `enabled = false` committed, following the established
  pattern of `[llm]` and `[cloud]`. Enabling it never enables a provider, and
  enabling a provider never enables an action.

## 11. Voice separation

```text
microphone / browser mic
  -> AudioSource (16 kHz mono S16_LE)            existing contract
  -> WakeWordDetector                            existing, replaceable
  -> LiveVoiceController state machine           existing, owns no device
  -> StreamingSTTEngine                          existing lifecycle owner
  -> normalize_transcript                        existing
  ===== text boundary: everything above is audio-only =====
  -> ConversationRequest
  -> handle_text (router, planner, validator, coordinator)
  ===== text boundary: everything below is audio-only =====
  -> ResponseFormatter fixed local text          existing
  -> LocalTTSProvider / OpenAITTSProvider        existing
  -> AudioOutput sink                            existing, separate
```

Interface rules:

- STT and TTS components import no registry, coordinator, broker, agent, or
  auth module, and they receive no plan id, identity, or credential. Today this
  is already true; the design keeps it true by putting the planner strictly
  inside the text boundary.
- STT output is untrusted text. It gains authority only by traversing the same
  pipeline a typed sentence traverses. A transcript cannot confirm a plan except
  through `LocalVoiceAuthorization`, which requires a physical wake in the same
  session and a fixed affirmative token.
- TTS receives the final response string only. A response is never composed by
  the TTS layer, and a synthesis failure degrades to a tone or text, never to a
  skipped confirmation.
- a future satellite microphone implements the same `AudioSource` contract and
  the same text boundary. Raw audio stays off MQTT; MQTT carries only low-rate
  semantic events.

## 12. Auditing

Five record kinds, deliberately distinguishable. The first three are new
trace/audit shapes; the last two exist.

| Record | Where | Contains | Excludes |
| --- | --- | --- | --- |
| Utterance | `TraceStage.REQUEST` and the session message log | normalized text, source, session, generation | audio, raw PCM, credentials |
| Planner proposal | `TraceStage.MODEL`, reason code `planner_proposal` | provider, model, outcome, goal summary, proposed action ids, parameters as proposed, confidence, ambiguity, bounded rationale, elapsed, tokens | provider chain-of-thought, credentials, raw provider payload beyond the bounded field |
| Validation outcome | `TraceStage.POLICY` | accepted or the typed rejection reason code, composed steps, template name | provider text as an explanation |
| Confirmed plan | `ActionStateStore` plan row plus `audit(method=...)` | plan id, canonical steps, digest, hashed session and identity, source (`planner` vs `manual_ui` vs `local_voice`), authentication level, confirmation form | nonce beyond its storage, passkey material, challenge |
| Execution and observed outcome | job rows plus `audit(...)` | skill, canonical sanitized arguments, outcome, elapsed, reason code, job stages, observed state after | command output, secrets, entity ids, addresses |

Requirements:

- `PendingPlan.source` is the discriminator between a manual Tools click and a
  planner-originated plan, and it is already stored and audited. A planner plan
  is `source="planner"`; an executed plan's audit `method` still records how
  authentication was satisfied. An operator can therefore answer "did a model
  cause this?" from the audit table alone.
- the proposal record must exist even when the plan is rejected or never
  confirmed. A proposal that went nowhere is exactly the record worth keeping.
- no secret, token, challenge, bootstrap token, key path, MAC, IP, SSH user, or
  raw credential enters any record, matching the existing audit and usage
  rules. The existing sanitizer is reused rather than reimplemented.
- usage accounting stays non-content: provider, model, tokens, cost, latency,
  error, as `cloud/usage.py` already does.

## 13. Local and manual fallback

The following must remain true and should each have a test:

- with `[planner].enabled = false` (the committed default), the deterministic
  router, every read-only skill, the diagnostic path, `Admin -> Tools`, the
  wake control, the shutdown control with both its gates, the broker, and the
  Desktop Agent all behave exactly as they do today;
- with no `OPENAI_API_KEY`, no internet, a failing provider, or an exhausted
  quota, a planner-eligible turn degrades to the existing truthful fallback,
  and nothing about manual control changes;
- with STT or TTS unavailable, text chat and the Admin panel remain fully
  functional;
- no privileged action is reachable *only* through the planner. Every action
  the planner may name is independently reachable from Admin or from a
  deterministic route. The planner is strictly additive convenience;
- planner failure is never a Butters failure state: `/readyz` does not depend
  on a planner, and the home monitoring stack remains entirely independent, as
  `butters/ARCHITECTURE.md` already requires.

## 14. Implementation phases

**Phase 0 — typed skeleton, no provider, no execution.** New `butters.planner`
package: `model.py`, `catalog.py`, `validator.py`, `provider.py` with
`NullPlanner` only. A `[planner]` config section defaulting to disabled. Tests
for catalog derivation, the safety-class function, catalog parity with its
documented exclusions, and every validator rejection path. Nothing is wired
into the turn pipeline.

**Phase 1 — observation and preview.** `StateSnapshot` assembly from the
existing observers, and an administrator-only preview endpoint that accepts an
utterance plus a hand-written `ProposedPlan` and returns the validated
composition or the typed rejection. This exercises the whole new layer with no
model and no execution.

**Phase 2 — composer and reuse of reviewed workflows.** `PlanComposer` with the
enumerated prerequisite vocabulary and the initial desktop templates, mapped
onto `DesktopWorkflow` and any reviewed workflow action that follows. Still no
provider, still no freeze from the planner path.

**Phase 3 — orchestrator wiring, still provider-free.** Insert
`ConversationOrchestrator` as stage 5 of the existing resolution order, behind
the disabled config flag, with the freeze path enabled only for
administrator-audience turns and only for `REVERSIBLE` actions initially. The
entire confirmation and authentication chain is the existing one.

**Phase 4 — first provider.** `OpenAIResponsesPlanner` or `LocalLlamaPlanner`
behind explicit configuration, budgets, and the existing usage ledger, with a
fixed ground-truth planner corpus and a model-independent scorer, following the
precedent in `butters/benchmarks/llm-corpus.json`.

**Phase 5 — voice-path planner.** Extend to the physical voice session, keeping
`FRESH` actions on the passkey path and the local affirmative vocabulary
unchanged.

Each phase is independently revertible and leaves production behavior unchanged
while its flag is off.

## 15. First implementation slice, as built

The first slice exists on `integration/conversational-planner-foundation`
(not merged). It implements this design's Phase 0 with one deliberate naming
difference: the package is `butters.planner`, not `butters.planning`. That
name is now authoritative and the rest of this document should be read with it.

What the branch contains:

1. `butters/src/butters/planner/model.py` — `PlannerRequest`,
   `PlannerCatalogAction`, `PlannerStep`, `PlannerPlan`, `PlannerError`.
2. `butters/src/butters/planner/provider.py` — the `PlannerProvider` protocol,
   `DisabledPlannerProvider` (the production default), and a
   `DeterministicPlannerProvider` used only by tests. No provider performs I/O.
3. `butters/src/butters/planner/validator.py` — `PlannerValidator`, which
   builds the proposal catalog from `SkillRegistry` and validates provider
   output against it.
4. `SkillRegistry.canonical_arguments` — a public typed projection so the
   planner freezes exactly the values the registry accepts, instead of
   duplicating any parser.
5. `[planner]` in `butters/config/assistant.toml` with `enabled = false` and
   `provider = "disabled"`; `PlannerSettings.validated()` refuses any provider
   value other than `disabled`.
6. `POST /api/planner` behind the existing session, `Origin`, CSRF, and rate
   limiting, returning a structured `unavailable` state by default.
7. `butters/tests/test_conversational_planner_foundation.py`.

How it maps onto this design:

- the catalog is proposal-only and separate from `llm/catalog.py`, which still
  omits every `ACTION` from `derive_safe_tool_catalog`;
- authentication and confirmation are recomputed from `SkillSpec` and a
  reviewed confirmation set; a provider's own booleans carry no authority;
- read-only steps execute through `SkillRegistry`; every `ACTION` goes through
  `ActionCoordinator.freeze_plan`, the confirmation state, and the passkey
  ceremony;
- multi-step plans are refused unless their exact ordered action IDs appear in
  a reviewed composition template, and that set ships empty — so a validated
  plan freezes exactly one action, and section 3.7's composition remains
  deferred to a workflow action rather than planner assembly;
- two independent gates keep it off: `[planner].enabled` and provider
  availability.

What it deliberately does not do, and what the next slices owe:

- no `StateSnapshot`: the slice supplies no observed facets at all rather than
  a collapsed value. Section 7 remains the target;
- no safety-class derivation module: section 5's labels are not yet computed in
  code, though the reviewed confirmation set it depends on is;
- no utterance-level idempotency (section 9, layer 1);
- no prerequisite vocabulary or `PlanComposer` (sections 4 and 3.7).

## 16. Existing code reused unchanged

No change is required to any of the following. The planner layer is additive.

| Component | Path | Role in this design |
| --- | --- | --- |
| `SkillSpec` / `SkillRegistry` | `skills/registry.py` | authoritative capability catalog and strict parsing |
| `PolicyValidator` | `skills/policy.py` | default-deny, intent, confirmation, authentication |
| `ActionAuthorization` / `AuthenticationContext` | `skills/model.py` | authority the planner cannot construct |
| `ActionCoordinator` | `actions/coordinator.py` | freeze, digest binding, execute, jobs, cancellation |
| `ActionStateStore` / `PendingPlan` | `actions/store.py` | one-use plans, jobs, sanitized audit, overrides |
| `PasskeyManager` / `AuthStateStore` | `auth/manager.py`, `auth/store.py` | WebAuthn ceremonies, elevation |
| `ActionBroker` + `BrokerOperation` | `actions/broker.py` | enumerated privileged operations, root gates |
| `DesktopWorkflow` / `DesktopState` | `integrations/desktop.py` | reviewed multi-stage desktop sequencing and facets |
| `IntentRouter` / `RoutedIntent` | `routing/router.py`, `routing/model.py` | stage 1, and the existing `action_plan` shape |
| compound read planner | `routing/compound.py` | stage 2, unchanged |
| action freeze inside `handle_text` | `web/service.py` | the single existing path to a privileged action |
| `LocalVoiceAuthorization` | `live/authorization.py` | physical-session confirmation domain |
| `LiveVoiceController` / `StreamingSTTEngine` | `live/controller.py`, `stt/` | audio edge, unchanged |
| `LocalTTSProvider` / `OpenAITTSProvider` | `web/speech.py` | response audio, unchanged |
| `ResponseFormatter` | `responses/formatter.py` | fixed local response text |
| diagnostics sanitizer | `diagnostics/sanitizer.py` | sanitizing planner input and records |
| `UsageLedger` | `cloud/usage.py` | non-content provider accounting and budgets |
| `EscalationPolicy` | `cloud/routing.py` | tier selection for a paid planner |
| `openai_responses` request/parse | `cloud/openai_responses.py` | strict function-call transport for one planner |
| `llama_server` + restricted parsing | `llm/llama_server.py`, `llm/parsing.py` | transport for a local planner |
| `TraceBuffer` / `TraceStage` | `web/trace.py` | proposal, policy, and execution tracing |
| model-visibility parity discipline | `llm/catalog.py` | the pattern the planner catalog copies, not the catalog itself |

Two components are deliberately *not* reused as-is:

- `derive_safe_tool_catalog` stays exactly as it is. The planner gets a
  separate proposal-only catalog, so the existing rule that a model may never
  *call* an `ACTION` remains literally true in code.
- `LanguageModel.propose_tools` stays as the single-tool semantic fallback for
  read-only routing. The planner is a distinct interface because its output is
  a plan proposal, not a tool call.

## 17. Review checklist for any future planner change

- Did this change add an execution path that does not pass
  `ActionCoordinator.freeze_plan` and `PolicyValidator.authorize`?
- Can any planner output reach a shell, an SSH command, a Windows executable,
  an MQTT publish, an arbitrary host, or a free-text argument?
- Does any infrastructure identifier appear in a planner request or response?
- Can a planner output satisfy a confirmation or an authentication requirement?
- Does the change let a planner plan bypass `TOOLS_CONFIRM_ACTIONS`, a
  `FRESH` requirement, or a root broker gate?
- Do Admin, the broker, and the manual controls still work with the planner
  disabled and no provider configured?
- Are the five audit record kinds still distinguishable?
- Does a new capability appear in the planner catalog or in
  `PLANNER_EXCLUSIONS` with a reason, so the parity test passes?
