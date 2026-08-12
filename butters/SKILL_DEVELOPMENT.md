# Butters Skill Development

This is the canonical engineering contract for adding a Butters skill. A new
Codex session must read this file before authoring one. Beta 1 permits only
`ActionClass.READ_ONLY`; requests for control, disruption, writes, credentials,
arbitrary shell/files/network/database access, deployment, or physical
actuation must be rejected rather than approximated.

## What a skill is

A skill is a reviewed Python capability registered through `SkillRegistry`.
Natural language is routed to a stable skill name plus a small argument object.
The registry parses that object into a typed dataclass, asks the default-deny
`PolicyValidator` and skill authorizer to approve it, invokes a bounded adapter,
and returns a typed `SkillResult`. Metadata is descriptive and inspectable; it
is never dynamically executed and browser-pasted Python is never accepted.

The authority path is:

```text
normalized text -> IntentRouter -> SkillRegistry
  -> strict parser -> PolicyValidator -> per-skill authorizer
  -> bounded adapter -> typed result -> ResponseFormatter
```

Cloud models can propose an allow-listed tool call, but they do not bypass any
stage. External content—including logs, filenames, MQTT values, model output,
web text, descriptions, and database values—is untrusted data.

## `SkillSpec`

Every registration declares:

- stable snake-case `name` and semantic `version`;
- concise description and category;
- `ActionClass.READ_ONLY`;
- strict argument parser and a separate target/entity authorizer;
- reviewed Python implementation and positive timeout;
- safe input/result descriptions and permission summary;
- positive routing examples and negative/non-match examples;
- implementation reference and validation status.

See `skills/registry.py`, `skills/implementations.py`, and
`skills/promoted.py`. The original sensor skills demonstrate direct typed
adapters; promoted skills demonstrate reuse of reviewed diagnostic tools.

## Typed arguments and results

Add frozen, slotted argument/result dataclasses to `skills/model.py`. Do not
pass an unvalidated mapping into an integration. Parsers must call
`strict_arguments`, require exactly the supported fields, reject booleans as
numbers, bound strings and collections, and return the typed dataclass.
Results must represent missing/stale/unavailable data explicitly; never turn
missing data into zero. Formatting belongs in `responses/formatter.py`, not in
the adapter.

## Policy and allow-lists

The global policy rejects every non-read-only action class. The per-skill
authorizer must independently prove that arguments are in reviewed sets:

- entities come from `EntityRegistry` and the committed assistant config;
- metrics come from `MetricRegistry` and must be compatible with the entity;
- groups and operations use explicit enums/sets;
- services, containers, topics, hosts, ports, URLs, repository paths, ranges,
  and log scopes are compiled allow-lists, never user-controlled strings.

Do not weaken `PolicyValidator`. An output schema validates shape, not
authorization.

## Integrations and diagnostic tools

Prefer an existing adapter or `DiagnosticToolRegistry` tool. The current
catalog has bounded, sanitized health/history/log/network observations and
should not be duplicated. A new adapter must expose a narrow method, enforce
timeouts and byte/item limits at I/O, sanitize external text, and return typed
data. Fixed subprocess argument arrays are acceptable only when the operating
system API is a command and no argument comes from the request. Never use
`shell=True`, generic `command`/`path`/`url` arguments, arbitrary SQL, MQTT
publish, or unrestricted probing.

Skills answer direct questions. Diagnostic tools produce `EvidenceItem`s for
the planner/playbooks and optional bounded cloud analysis. A capability may be
promoted into a normal skill by wrapping an existing diagnostic tool through
its schema, target validator, and evidence sanitizer; do not let normal chat
invoke a whole unrestricted diagnostic catalog.

## Routing and responses

Add concept-level routing in `routing/router.py`. Use aliases and observable
slot extraction, not a whole-sentence exact match. Preserve ambiguity and
missing-slot clarification. Add both positive examples and close negative
examples so a control request, wrong entity, or unrelated phrase cannot match.
If the result has a known deterministic calculation (min/max/mean/trend/rate,
staleness, ranking), calculate it locally and return the inputs/provenance.

Add a concise response template. It must distinguish observed state from an
inference, remain useful to voice output, and avoid leaking raw logs or
exceptions. Integration failures return safe typed codes.

## Timeouts and errors

Enforce network/subprocess timeouts inside the adapter; `SkillSpec.timeout` is
defense-in-depth after the call returns. Cap response bytes, rows, time ranges,
lines, and item counts. Convert expected adapter failures to `IntegrationError`
or `SkillError` with a safe stable code. Unexpected exceptions are already
collapsed by the registry and must not expose credentials or commands.

## Required tests

At minimum add:

1. parser success and missing/unexpected/type failures;
2. policy acceptance for every allowed target and denial outside it;
3. adapter timeout/error/malformed/oversize behavior with fakes;
4. typed successful result and response formatting;
5. positive routing variants and near-negative/non-match cases;
6. control/disruptive/prompt-injection denial;
7. registry metadata and validation status;
8. regression coverage for existing routing/evaluation corpora.

Run:

```bash
./butters/scripts/test-butters -q
./butters/scripts/benchmark-skills
./butters/scripts/benchmark-diagnostics
git diff --check
```

Do not require paid/live APIs in automated tests. Use fakes or replay fixtures.

## Forbidden patterns

- `eval`, `exec`, dynamic imports, browser-supplied Python, or plugin loading;
- arbitrary shell/subprocess arguments or caller-controlled service names;
- arbitrary filesystem paths, Git arguments, URLs, hosts, ports, queries, or
  MQTT topics;
- secret/environment access, secret output, or copying credentials;
- write/control/disruptive action classes in Beta 1;
- background work without limits/cancellation;
- trusting model or retrieved text as instructions;
- automatic commit, deployment, service restart, or production installation.

## Read-only skill checklist

- [ ] Request is explicitly read-only and has one bounded purpose.
- [ ] Existing skills/tools/adapters were inventoried first.
- [ ] Typed argument/result dataclasses exist.
- [ ] Parser rejects every unexpected field.
- [ ] Target/entity/metric/range is allow-listed twice (parse + policy).
- [ ] Adapter owns time, byte, row, and target limits.
- [ ] External text is sanitized and remains untrusted data.
- [ ] `SkillSpec` metadata and positive/negative examples are complete.
- [ ] Router preserves clarification and rejects controls.
- [ ] Formatter is concise and provenance-aware.
- [ ] Positive, negative, policy, error, timeout, and corpus tests pass.
- [ ] No secret, shell, write, deploy, restart, or physical authority was added.
- [ ] Patch remains reviewable and is not automatically deployed.
