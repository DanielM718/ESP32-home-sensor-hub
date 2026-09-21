"""Adaptive cloud reasoning: tier selection, summaries, and what must not move.

Butters Chat used to send one fixed model and effort - in practice Terra at
`high` - for essentially every question the deterministic router could not
answer. These tests hold the replacement to four promises:

1. A deterministic question is still answered locally, with no model at all.
   Adaptive routing may never take a request the router already matched.
2. The tier is chosen locally, from the same complexity classification the
   trace already records, with no provider call of any kind.
3. Cost admission prices the model that will actually be called.
4. Speech says the answer and nothing else. Not the reasoning summary, not
   the model, not the tier.

The reasoning-summary fixtures follow the documented OpenAI Responses shape:
a `reasoning` output item carrying a `summary` array of `summary_text` parts.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from butters.ai.capabilities import CapabilityError, build_registry
from butters.ai.model import validate_chat
from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.cloud.adaptive import (
    DEFAULT_MAX_AUTOMATIC_TIER,
    DEFAULT_ROUTING_MODE,
    AdaptiveCloudRouter,
    CloudTier,
    classify_complexity,
)
from butters.cloud.general import (
    MAX_REASONING_SUMMARY_CHARS,
    GeneralCloudTurn,
    OpenAIGeneralReasoner,
)
from butters.cloud.model import CloudReasonerError, CloudTokenUsage, EscalationLevel
from butters.integrations.model import (
    SensorRecord,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.stt.normalization import DomainVocabulary
from butters.web.service import BetaAssistantService, RouteOverride
from frontend_assets import STYLESHEETS, declarations

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
APP_JS = (STATIC / "assets/app.js").read_text(encoding="utf-8")
ADMIN_JS = (STATIC / "assets/admin.js").read_text(encoding="utf-8")
ADMIN_HTML = (STATIC / "admin.html").read_text(encoding="utf-8")


# =========================== harness ======================================


class Sensors:
    def snapshot(self) -> SensorSnapshot:
        return SensorSnapshot(
            "2026-08-12T12:00:00Z",
            tuple(
                SensorRecord(
                    "environment",
                    str(index),
                    "2026-08-12T11:59:55Z",
                    5,
                    "online",
                    {"humidity": 20.0 + index, "temperature": 20.0 + index},
                )
                for index in (1, 2, 3)
            ),
        )


class Health:
    def snapshot(self) -> ServerHealthSnapshot:
        return ServerHealthSnapshot(
            100, 0.1, 0.1, 0.1, 1_000_000, 0, 1_000_000, 2_000_000, 45.0, "0x0", ()
        )


class General:
    """A cloud reasoner that records every request and never leaves the host."""

    available = True

    def __init__(
        self, *, text: str = "The likely cause is X.", summary: str | None = None
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._text = text
        self._summary = summary

    def reason(self, **kwargs: object) -> GeneralCloudTurn:
        self.calls.append(kwargs)
        return GeneralCloudTurn(
            str(kwargs["model"]),
            str(kwargs["effort"]),
            0.01,
            response_id="response_safe_id",
            response_text=self._text,
            usage=CloudTokenUsage(input_tokens=100, output_tokens=20),
            reasoning_summary=self._summary,
        )


ADAPTIVE_PROFILE = {
    "provider": "openai",
    "model": "gpt-5.6-terra",
    "reasoning_effort": "high",
    "max_output_tokens": 1200,
    "routing_mode": "adaptive",
    "max_automatic_tier": "sol",
}


def _service(
    tmp_path: Path,
    *,
    cloud: bool = True,
    adaptive: bool = False,
    ceiling: str = "sol",
    summaries: bool = False,
    allow_automatic_maximum: bool = False,
    budget: float = 0.5,
    reasoner: General | None = None,
):
    base = load_assistant_settings()
    settings = replace(
        base,
        cloud=replace(
            base.cloud,
            enabled=cloud,
            allow_paid_calls=cloud,
            allow_automatic_maximum=allow_automatic_maximum,
            max_estimated_cost_per_request_usd=budget,
        ),
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            max_sessions_per_peer=60,
        ),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(
        settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health()
    )
    provider = reasoner if reasoner is not None else General()
    service = BetaAssistantService(
        settings,
        vocabulary,
        assistant=assistant,
        general_reasoner=provider,
        state_dir=tmp_path,
    )
    if adaptive:
        service.ai.apply_chat(
            {
                **ADAPTIVE_PROFILE,
                "max_automatic_tier": ceiling,
                "reasoning_summary_enabled": summaries,
            }
        )
    return service, provider


def _ask(service, text: str):
    """One turn on a fresh conversation.

    A pending clarification is conversation state, so reusing one session
    across unrelated corpus prompts would let the previous turn decide the
    next one's route.
    """

    return service.handle_text(service.sessions.create(), text)


def _decide(text: str, *, allow_automatic_maximum: bool = False, ceiling: str | None = None):
    """Classify and tier one prompt with no service and no network."""

    base = load_assistant_settings().cloud
    router = AdaptiveCloudRouter(
        replace(
            base,
            enabled=True,
            allow_paid_calls=True,
            allow_automatic_maximum=allow_automatic_maximum,
        )
    )
    features = classify_complexity(
        text.casefold(),
        route_matched=False,
        aggregate=False,
        missing_arguments=(),
        diagnostic_recognized=False,
        admin_override=None,
    )
    return router.select(features, max_automatic_tier=ceiling, fallback_effort="high")


# ==================== 1. the adaptive routing corpus ======================
#
# The prompts the feature was specified against, tiered by the real
# classifier and the real policy. Selection is a pure function of the text,
# so this needs no service, no credential and no provider.

CORPUS_D = (
    "Why can relative humidity increase when temperature falls even if the "
    "amount of water vapor stays the same?"
)
CORPUS_E = (
    "Explain how temperature differences between a filament box and the room "
    "could affect the humidity readings, and how I could distinguish that from "
    "actual moisture entering the box."
)
CORPUS_F = (
    "I see a humidity rise after the room cools. Compare temperature-driven RH "
    "change, actual moisture ingress, and sensor error and explain what "
    "evidence would distinguish them."
)
CORPUS_G = (
    "Remote Jellyfin playback is buffering even though nominal upload bandwidth "
    "appears sufficient. Analyze competing explanations involving bitrate, "
    "transcoding, Tailscale transport, other remote traffic, and latency, and "
    "identify measurements that distinguish the hypotheses."
)
CORPUS_H = (
    "Investigate this exhaustively and consider every supported competing cause "
    "of the humidity drift."
)


@pytest.mark.parametrize(
    ("label", "prompt", "tier", "model", "effort"),
    [
        ("D simple general knowledge", CORPUS_D, "light", "gpt-5.6-luna", "medium"),
        ("E moderate synthesis", CORPUS_E, "balanced", "gpt-5.6-terra", "medium"),
        ("F causal analysis", CORPUS_F, "analysis", "gpt-5.6-terra", "high"),
        ("G deep technical analysis", CORPUS_G, "deep", "gpt-5.6-sol", "high"),
    ],
)
def test_the_corpus_selects_the_intended_tier(
    label: str, prompt: str, tier: str, model: str, effort: str
) -> None:
    decision = _decide(prompt)

    assert decision.tier_name == tier, (label, decision.as_dict())
    assert decision.model == model
    assert decision.effort == effort
    assert decision.reason_codes or tier == "light"


def test_deep_uses_the_lower_sufficient_effort() -> None:
    """Sol at `high` unless the evidence genuinely piles up."""

    assert _decide(CORPUS_G).effort == "high"


def test_explicit_exhaustive_reaches_maximum_only_when_policy_allows_it() -> None:
    denied = _decide(CORPUS_H, allow_automatic_maximum=False)
    allowed = _decide(CORPUS_H, allow_automatic_maximum=True)

    # The reviewed automatic-maximum approval is a separate gate and this
    # feature does not widen it. Without it the request still gets the
    # deepest permitted tier, and says why it got no further.
    assert denied.tier is CloudTier.DEEP
    assert denied.effort != "max"
    assert "automatic_maximum_not_permitted" in denied.reason_codes

    assert allowed.tier is CloudTier.MAXIMUM
    assert allowed.model == "gpt-5.6-sol"
    assert allowed.effort == "max"


def test_maximum_is_never_reached_without_asking_for_it() -> None:
    """No ordinary prompt, however complex, buys Sol/max by itself."""

    for prompt in (CORPUS_D, CORPUS_E, CORPUS_F, CORPUS_G):
        decision = _decide(prompt, allow_automatic_maximum=True)
        assert decision.tier is not CloudTier.MAXIMUM, prompt
        assert decision.effort != "max"


# ------------------------- the length trap --------------------------------


def test_length_alone_never_determines_the_tier() -> None:
    """"Explain this in detail" is not Sol merely because it will be long."""

    assert _decide("Explain this in detail").tier is CloudTier.LIGHT

    padded = (
        "Explain this in detail and please be really thorough about it because "
        "I would like a very long complete answer that covers the whole topic "
        "from beginning to end without leaving anything at all out of the "
        "description you produce for me today"
    )
    decision = _decide(padded)
    assert decision.tier is CloudTier.LIGHT
    # Length contributes at most one point, and the LIGHT band is two wide.
    assert decision.estimated_complexity <= 1


def test_an_ordinary_comparison_stays_on_terra() -> None:
    """Sol is for contradictory evidence and log interpretation, not for any
    prompt that happens to say "compare" and "explanations"."""

    decision = _decide(
        "Compare convection, conduction, and radiation as explanations for an "
        "enclosure warming overnight, and explain what evidence would "
        "distinguish them."
    )
    assert decision.tier is CloudTier.ANALYSIS
    assert decision.model == "gpt-5.6-terra"


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "These logs show an unfamiliar error after the service restarts. "
            "Compare a config regression, a dependency change, and a permissions "
            "problem, and explain what evidence would distinguish them."
        ),
        (
            "The evidence is contradictory: the dashboard says healthy but the "
            "readings disagree. Compare a stale cache, a clock skew, and a broken "
            "exporter and explain which is most supported."
        ),
    ],
)
def test_the_deep_markers_promote_a_genuine_investigation(prompt: str) -> None:
    decision = _decide(prompt)

    assert decision.tier is CloudTier.DEEP
    assert decision.model == "gpt-5.6-sol"


# ---------------------------- the ceiling ---------------------------------


@pytest.mark.parametrize(
    ("ceiling", "tier", "model"),
    [
        ("luna", "light", "gpt-5.6-luna"),
        ("terra", "analysis", "gpt-5.6-terra"),
        ("sol", "deep", "gpt-5.6-sol"),
    ],
)
def test_the_configured_ceiling_caps_automatic_selection(
    ceiling: str, tier: str, model: str
) -> None:
    decision = _decide(CORPUS_G, ceiling=ceiling)

    assert decision.tier_name == tier
    assert decision.model == model
    assert decision.ceiling_applied is (ceiling != "sol")
    if ceiling != "sol":
        assert "ceiling_applied" in decision.reason_codes


def test_the_ceiling_also_binds_an_explicitly_exhaustive_request() -> None:
    decision = _decide(CORPUS_H, allow_automatic_maximum=True, ceiling="terra")

    assert decision.tier is CloudTier.ANALYSIS
    assert decision.model == "gpt-5.6-terra"
    assert decision.effort != "max"


# --------------------- decisions are explainable --------------------------


def test_a_decision_explains_itself_in_butters_own_terms() -> None:
    decision = _decide(CORPUS_F)
    payload = decision.as_dict()

    assert payload["tier"] == "analysis"
    assert payload["model"] == "gpt-5.6-terra"
    assert payload["effort"] == "high"
    assert payload["mode"] == "adaptive"
    assert isinstance(payload["estimated_complexity"], int)
    # Deterministic routing reasons, not the model's reasoning.
    assert "comparison_or_aggregation" in payload["reason_codes"]
    assert "hypothesis_discrimination" in payload["reason_codes"]


def test_the_router_reads_the_classification_the_trace_records() -> None:
    """There is one classifier, not two: the keys the router scores are the
    keys the `complexity` trace stage already emits."""

    features = classify_complexity(
        CORPUS_F.casefold(),
        route_matched=False,
        aggregate=False,
        missing_arguments=(),
        diagnostic_recognized=False,
        admin_override=None,
    )
    # The pre-existing keys are still present and unchanged in meaning.
    for legacy in (
        "deterministic_route_matched",
        "complete_required_slots",
        "missing_required_arguments",
        "single_operation",
        "historical_data_required",
        "comparison_or_aggregation",
        "diagnostic_domain_recognized",
        "open_ended_causal_request",
        "external_general_knowledge_required",
        "admin_override",
    ):
        assert legacy in features, legacy
    assert features["comparison_or_aggregation"] is True
    assert features["hypothesis_discrimination"] is True


def _adaptive_module_imports() -> set[str]:
    """Top-level module names the routing module imports.

    Read from the parse tree rather than by grepping the text, so prose in a
    docstring cannot pass or fail this audit.
    """

    import ast

    tree = ast.parse(
        (Path(__file__).parents[1] / "src/butters/cloud/adaptive.py").read_text(
            encoding="utf-8"
        )
    )
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_selection_makes_no_provider_call() -> None:
    """The router cannot reach the network: it imports nothing that could."""

    router = AdaptiveCloudRouter(load_assistant_settings().cloud)
    imported = _adaptive_module_imports()

    assert not hasattr(router, "reason")
    assert imported <= {"__future__", "re", "dataclasses", "enum", "butters"}
    for forbidden in ("urllib", "http", "socket", "requests", "ssl", "subprocess"):
        assert forbidden not in imported, forbidden


# ============ 2. deterministic routing is never stolen ====================

DETERMINISTIC = [
    "What is the humidity in filament box 1?",
    "What are the temperature and humidity in filament box 1?",
    "Which filament box currently has the highest humidity?",
    "What is the NAS status?",
    "What is the status of the NAS and Jellyfin?",
    "What is the Jellyfin status?",
    "What is the sensor status for filament box 1?",
    "What is the printer status?",
    "What is the desktop status?",
    "What is the Butters service status?",
    "What is the action broker status?",
    "What is the storage status?",
    "What is the server health?",
    "Turn on the dehumidifier",
]


@pytest.mark.parametrize("prompt", DETERMINISTIC)
def test_a_deterministic_question_never_reaches_a_model(
    tmp_path: Path, prompt: str
) -> None:
    """Adaptive routing must not take a request the router already matched,
    however complicated the sentence sounds."""

    service, reasoner = _service(tmp_path, adaptive=True)

    response = _ask(service, prompt)

    assert reasoner.calls == [], prompt
    assert response.route != "general_cloud", prompt
    assert response.cloud_used is False
    assert response.model is None
    assert response.routing_tier is None


@pytest.mark.parametrize(
    ("prompt", "route"),
    [
        ("What is the humidity in filament box 1?", "deterministic"),
        ("What are the temperature and humidity in filament box 1?", "deterministic"),
        ("Which filament box currently has the highest humidity?", "deterministic"),
    ],
)
def test_corpus_a_to_c_stay_local_and_model_free(
    tmp_path: Path, prompt: str, route: str
) -> None:
    service, reasoner = _service(tmp_path, adaptive=True)

    response = _ask(service, prompt)

    assert response.route == route
    assert response.cloud_used is False
    assert reasoner.calls == []


def test_deterministic_routing_is_identical_in_both_modes(tmp_path: Path) -> None:
    fixed, fixed_reasoner = _service(tmp_path / "fixed", adaptive=False)
    adaptive, adaptive_reasoner = _service(tmp_path / "adaptive", adaptive=True)

    for prompt in DETERMINISTIC:
        one = _ask(fixed, prompt)
        two = _ask(adaptive, prompt)
        assert one.route == two.route, prompt
        assert one.skill == two.skill, prompt
        assert one.response_text == two.response_text, prompt
    assert fixed_reasoner.calls == []
    assert adaptive_reasoner.calls == []


# ================= 3. adaptive selection end to end =======================

LIGHT_PROMPT = "Why does warm air hold more water vapour than cold air?"
BALANCED_PROMPT = (
    "Explain how thermal drift could change an instrument measurement, and how "
    "I would distinguish it from a genuine change in the sample."
)
ANALYSIS_PROMPT = (
    "Compare convection, conduction, and radiation as explanations for an "
    "enclosure warming overnight, and explain what evidence would distinguish "
    "them."
)
DEEP_PROMPT = (
    "These logs show an unfamiliar error after the service restarts. Compare a "
    "config regression, a dependency change, and a permissions problem, and "
    "explain what evidence would distinguish them."
)


@pytest.mark.parametrize(
    ("prompt", "tier", "model", "effort"),
    [
        (LIGHT_PROMPT, "light", "gpt-5.6-luna", "medium"),
        (BALANCED_PROMPT, "balanced", "gpt-5.6-terra", "medium"),
        (ANALYSIS_PROMPT, "analysis", "gpt-5.6-terra", "high"),
        (DEEP_PROMPT, "deep", "gpt-5.6-sol", "high"),
    ],
)
def test_adaptive_mode_sends_the_selected_model_and_effort(
    tmp_path: Path, prompt: str, tier: str, model: str, effort: str
) -> None:
    service, reasoner = _service(tmp_path, adaptive=True)

    response = _ask(service, prompt)

    assert response.route == "general_cloud"
    assert response.cloud_used is True
    assert response.routing_mode == "adaptive"
    assert response.routing_tier == tier
    assert response.model == model
    assert response.reasoning_effort == effort
    # What was reported is what was actually sent.
    assert len(reasoner.calls) == 1
    assert reasoner.calls[0]["model"] == model
    assert reasoner.calls[0]["effort"] == effort


def test_adaptive_mode_does_not_climb_a_paid_ladder(tmp_path: Path) -> None:
    """One call at the chosen tier. Luna is not bought first as a warm-up."""

    service, reasoner = _service(tmp_path, adaptive=True)

    response = _ask(service, DEEP_PROMPT)

    assert len(reasoner.calls) == 1
    assert reasoner.calls[0]["model"] == "gpt-5.6-sol"
    assert response.tier_escalated is False


def test_ordinary_chat_reports_no_escalation_it_did_not_perform(
    tmp_path: Path,
) -> None:
    """Normal Chat has no trustworthy structured "this answer is insufficient"
    signal, so it performs no second cloud call and never claims one.

    The diagnostic subsystem keeps its own evidence-aware iterative
    escalation, which is driven by a typed `Confidence` from a strict
    function-call schema. Nothing equivalent exists for free-form Chat text,
    and prose is not evidence of insufficiency.
    """

    service, reasoner = _service(tmp_path, adaptive=True)

    for prompt in (LIGHT_PROMPT, ANALYSIS_PROMPT, DEEP_PROMPT):
        response = service.handle_text(service.sessions.create(), prompt)
        assert response.tier_escalated is False, prompt
    assert all(call["previous_response_id"] is None for call in reasoner.calls)


def test_the_trace_records_the_routing_decision(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, adaptive=True)

    response = _ask(service, DEEP_PROMPT)

    trace = service.traces.get(response.trace_id)
    assert trace is not None
    started = next(
        item
        for item in trace.events
        if item.stage == "model" and item.status == "started"
    )
    routing = started.fields["routing"]
    assert routing["tier"] == "deep"
    assert routing["model"] == "gpt-5.6-sol"
    assert routing["mode"] == "adaptive"
    assert routing["reason_codes"]
    # The complexity stage carries the features the decision was made from.
    assert any(item.stage == "complexity" for item in trace.events)


# ================= 4. fixed mode and safe migration =======================


def test_a_profile_saved_before_this_feature_is_fixed(tmp_path: Path) -> None:
    """The migration promise: adopting this release changes nothing until an
    administrator chooses Adaptive."""

    registry = build_registry(load_assistant_settings())
    legacy = validate_chat(
        registry,
        {
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "max_output_tokens": 1200,
        },
    )

    assert legacy.routing_mode is None
    assert legacy.effective_routing_mode == DEFAULT_ROUTING_MODE == "fixed"
    assert legacy.adaptive is False
    assert legacy.reasoning_summary_enabled is None


def test_a_fresh_installation_is_also_fixed(tmp_path: Path) -> None:
    """A brand-new state directory must not start adaptive either: Butters
    cannot distinguish "new install" from "install that never opened the Chat
    form", so the safe answer is the same for both."""

    service, _ = _service(tmp_path, adaptive=False)

    assert service.ai.effective.chat.adaptive is False
    assert service.ai.effective.chat.effective_routing_mode == "fixed"


@pytest.mark.parametrize("prompt", [LIGHT_PROMPT, ANALYSIS_PROMPT, DEEP_PROMPT])
def test_fixed_mode_preserves_the_exact_previous_behaviour(
    tmp_path: Path, prompt: str
) -> None:
    service, reasoner = _service(tmp_path, adaptive=False)

    response = _ask(service, prompt)

    assert response.route == "general_cloud"
    assert response.routing_mode == "fixed"
    assert response.model == "gpt-5.6-terra"
    assert response.reasoning_effort == "high"
    assert reasoner.calls[0]["model"] == "gpt-5.6-terra"
    assert reasoner.calls[0]["effort"] == "high"


def test_switching_to_adaptive_and_back_is_reversible(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, adaptive=False)

    assert _ask(service, LIGHT_PROMPT).model == "gpt-5.6-terra"
    service.ai.apply_chat(ADAPTIVE_PROFILE)
    assert _ask(service, LIGHT_PROMPT).model == "gpt-5.6-luna"
    service.ai.apply_chat({**ADAPTIVE_PROFILE, "routing_mode": "fixed"})
    assert _ask(service, LIGHT_PROMPT).model == "gpt-5.6-terra"


def test_an_administrator_override_is_never_re_tiered(tmp_path: Path) -> None:
    """An operator who forced a model asked for exactly that model."""

    service, reasoner = _service(tmp_path, adaptive=True)

    response = service.handle_text(
        service.sessions.create(),
        LIGHT_PROMPT,
        override=RouteOverride.FORCE_CLOUD_MODEL,
        forced_model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        administrator=True,
    )

    assert response.model == "gpt-5.6-sol"
    assert response.reasoning_effort == "xhigh"
    assert response.routing_mode == "fixed"
    assert reasoner.calls[0]["model"] == "gpt-5.6-sol"
    assert reasoner.calls[0]["effort"] == "xhigh"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("routing_mode", "turbo", "invalid_routing_mode"),
        ("max_automatic_tier", "astra", "invalid_max_automatic_tier"),
        ("routing_mode", 7, "invalid_request"),
        ("reasoning_summary_enabled", "yes", "invalid_request"),
    ],
)
def test_routing_settings_are_allow_listed(field: str, value: object, code: str) -> None:
    registry = build_registry(load_assistant_settings())

    with pytest.raises(CapabilityError) as refused:
        validate_chat(
            registry,
            {"provider": "openai", "model": "gpt-5.6-terra", field: value},
        )
    assert refused.value.code == code


def test_no_model_outside_the_reviewed_allowlist_can_be_selected() -> None:
    """Luna, Terra, Sol. The router cannot name anything else."""

    settings = load_assistant_settings().cloud
    allowed = {settings.luna_model, settings.terra_model, settings.sol_model}

    for prompt in (CORPUS_D, CORPUS_E, CORPUS_F, CORPUS_G, CORPUS_H):
        for maximum in (False, True):
            for ceiling in ("luna", "terra", "sol", None):
                decision = _decide(
                    prompt, allow_automatic_maximum=maximum, ceiling=ceiling
                )
                assert decision.model in allowed, decision.as_dict()
    assert allowed == {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}


# ================= 5. cost admission by the selected model ================


def test_cost_admission_prices_the_model_that_will_be_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = _service(tmp_path, adaptive=True)
    priced: list[str] = []
    original = service.ledger.conservative_request_estimate

    def recording(model: str, evidence_bytes: int, max_output_tokens: int) -> float:
        priced.append(model)
        return original(model, evidence_bytes, max_output_tokens)

    monkeypatch.setattr(service.ledger, "conservative_request_estimate", recording)

    for prompt, model in (
        (LIGHT_PROMPT, "gpt-5.6-luna"),
        (ANALYSIS_PROMPT, "gpt-5.6-terra"),
        (DEEP_PROMPT, "gpt-5.6-sol"),
    ):
        priced.clear()
        response = service.handle_text(service.sessions.create(), prompt)
        # Not the configured model, and not a default: the selected one.
        assert priced == [model], (prompt, priced)
        assert response.model == model


def test_each_model_is_estimated_at_its_own_reviewed_price(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, adaptive=True)

    luna = service.ledger.conservative_request_estimate("gpt-5.6-luna", 4000, 1200)
    terra = service.ledger.conservative_request_estimate("gpt-5.6-terra", 4000, 1200)
    sol = service.ledger.conservative_request_estimate("gpt-5.6-sol", 4000, 1200)

    assert 0 < luna < terra < sol


def test_a_budget_denial_happens_before_the_provider_is_called(
    tmp_path: Path,
) -> None:
    service, reasoner = _service(tmp_path, adaptive=True, budget=0.0)

    response = _ask(service, DEEP_PROMPT)

    assert reasoner.calls == []
    assert response.route == "local_fallback"
    assert response.stopping_reason == "budget_denied"
    assert response.cloud_used is False


def test_adaptive_selection_cannot_outrun_the_per_request_ceiling(
    tmp_path: Path,
) -> None:
    """A ceiling that admits Luna but refuses Sol refuses Sol, rather than
    quietly downgrading the request to something affordable."""

    service, _ = _service(tmp_path, adaptive=True)
    luna = service.ledger.conservative_request_estimate("gpt-5.6-luna", 2000, 1200)
    sol = service.ledger.conservative_request_estimate("gpt-5.6-sol", 2000, 1200)
    assert luna < sol

    between, reasoner = _service(
        tmp_path / "between", adaptive=True, budget=(luna + sol) / 2
    )
    allowed = between.handle_text(between.sessions.create(), LIGHT_PROMPT)
    refused = between.handle_text(between.sessions.create(), DEEP_PROMPT)

    assert allowed.model == "gpt-5.6-luna"
    assert refused.stopping_reason == "budget_denied"
    assert [call["model"] for call in reasoner.calls] == ["gpt-5.6-luna"]


def test_the_ledger_records_the_tier_that_was_used(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, adaptive=True)

    _ask(service, LIGHT_PROMPT)
    _ask(service, DEEP_PROMPT)

    levels = {
        (row.model, row.escalation_level)
        for row in service.ledger.records
        if row.request_category == "general"
    }
    assert ("gpt-5.6-luna", int(EscalationLevel.LIGHT)) in levels
    assert ("gpt-5.6-sol", int(EscalationLevel.DEEP)) in levels


# ============= 6. the Responses reasoning-summary request =================


def _reasoner():
    settings = replace(
        load_assistant_settings().cloud, enabled=True, allow_paid_calls=True
    )
    return OpenAIGeneralReasoner(settings, api_key="fake-key")


def test_a_summary_is_requested_only_when_it_is_enabled() -> None:
    reasoner = _reasoner()
    common = {
        "text": "hello",
        "context": (),
        "tools": (),
        "model": "gpt-5.6-terra",
        "effort": "high",
        "max_output_tokens": 400,
        "previous_response_id": None,
        "tool_output": None,
        "parameters": None,
    }

    off = reasoner.build_request(**common)
    on = reasoner.build_request(**common, reasoning_summary="auto")

    # Unset stays out of the body entirely rather than being sent as a default.
    assert off["reasoning"] == {"effort": "high"}
    assert on["reasoning"] == {"effort": "high", "summary": "auto"}


def test_requesting_a_summary_changes_nothing_else_about_the_request() -> None:
    reasoner = _reasoner()
    common = {
        "text": "hello",
        "context": (),
        "tools": (),
        "model": "gpt-5.6-terra",
        "effort": "high",
        "max_output_tokens": 400,
        "previous_response_id": None,
        "tool_output": None,
        "parameters": None,
    }

    off = reasoner.build_request(**common)
    on = reasoner.build_request(**common, reasoning_summary="auto")

    assert {key: value for key, value in on.items() if key != "reasoning"} == {
        key: value for key, value in off.items() if key != "reasoning"
    }
    # Output ceiling, tool policy and storage posture are untouched.
    assert on["max_output_tokens"] == 400
    assert on["parallel_tool_calls"] is False
    assert on["store"] is False


def test_butters_never_asks_a_model_for_its_chain_of_thought() -> None:
    from butters.cloud.general import GENERAL_SYSTEM_INSTRUCTIONS

    lowered = GENERAL_SYSTEM_INSTRUCTIONS.casefold()
    assert "do not expose hidden reasoning or chain-of-thought" in lowered
    for forbidden in ("print your reasoning", "show your thinking", "step by step"):
        assert forbidden not in lowered, forbidden


def test_the_service_requests_summaries_only_when_the_operator_enabled_them(
    tmp_path: Path,
) -> None:
    off, off_reasoner = _service(tmp_path / "off", adaptive=True, summaries=False)
    on, on_reasoner = _service(tmp_path / "on", adaptive=True, summaries=True)

    _ask(off, LIGHT_PROMPT)
    _ask(on, LIGHT_PROMPT)

    assert off_reasoner.calls[0]["reasoning_summary"] is None
    assert on_reasoner.calls[0]["reasoning_summary"] == "auto"


def test_the_summary_preference_does_not_change_reasoning_effort(
    tmp_path: Path,
) -> None:
    off, _off = _service(tmp_path / "off", adaptive=True, summaries=False)
    on, _on = _service(tmp_path / "on", adaptive=True, summaries=True)

    for prompt in (LIGHT_PROMPT, ANALYSIS_PROMPT, DEEP_PROMPT):
        first = off.handle_text(off.sessions.create(), prompt)
        second = on.handle_text(on.sessions.create(), prompt)
        assert first.model == second.model, prompt
        assert first.reasoning_effort == second.reasoning_effort, prompt
        assert first.routing_tier == second.routing_tier, prompt


# ================ 7. parsing the documented output shape ==================


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


def _parse(output: list[object], usage: dict[str, object] | None = None):
    return OpenAIGeneralReasoner.parse_response(
        {"id": "resp_1", "output": output, "usage": usage or {}},
        model="gpt-5.6-terra",
        effort="high",
        elapsed_seconds=0.1,
    )


def _reasoning(*texts: str) -> dict[str, object]:
    return {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text} for text in texts],
    }


def _message(text: str) -> dict[str, object]:
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def test_a_final_answer_with_a_reasoning_summary() -> None:
    turn = _parse([_reasoning("I compared A, B, and C."), _message("The likely cause is X.")])

    assert turn.response_text == "The likely cause is X."
    assert turn.reasoning_summary == "I compared A, B, and C."


def test_an_answer_without_a_summary_still_works() -> None:
    turn = _parse([_message("Plain answer.")])

    assert turn.response_text == "Plain answer."
    assert turn.reasoning_summary is None


def test_reasoning_tokens_with_no_summary_produce_no_summary() -> None:
    turn = _parse(
        [{"type": "reasoning", "summary": []}, _message("Answer.")],
        {"output_tokens_details": {"reasoning_tokens": 512}},
    )

    assert turn.response_text == "Answer."
    assert turn.reasoning_summary is None
    assert turn.usage.reasoning_tokens == 512


def test_multiple_summary_parts_are_joined_once_each() -> None:
    turn = _parse(
        [_reasoning("First consideration.", "First consideration.", "Second one."), _message("A.")]
    )

    assert turn.reasoning_summary == "First consideration.\n\nSecond one."
    assert turn.reasoning_summary.count("First consideration.") == 1


def test_a_malformed_summary_part_does_not_destroy_the_answer() -> None:
    turn = _parse(
        [
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "Usable part."},
                    {"type": "summary_text", "text": 12345},
                    {"type": "not_a_summary", "text": "ignored"},
                    "a bare string",
                    None,
                ],
            },
            _message("The answer survives."),
        ]
    )

    assert turn.response_text == "The answer survives."
    assert turn.reasoning_summary == "Usable part."


def test_a_summary_that_is_not_an_array_is_ignored(tmp_path: Path) -> None:
    turn = _parse([{"type": "reasoning", "summary": "oops"}, _message("Still fine.")])

    assert turn.response_text == "Still fine."
    assert turn.reasoning_summary is None


def test_an_excessive_summary_is_bounded() -> None:
    turn = _parse([_reasoning("y" * 40_000), _message("Short answer.")])

    assert turn.response_text == "Short answer."
    assert len(turn.reasoning_summary) <= MAX_REASONING_SUMMARY_CHARS + 1


def test_a_tool_request_may_carry_a_summary() -> None:
    turn = _parse(
        [
            _reasoning("I need one reading first."),
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_sensor_value",
                "arguments": json.dumps({"entity": "filament_box_1"}),
            },
        ]
    )

    assert turn.tool_request is not None
    assert turn.tool_request.name == "get_sensor_value"
    assert turn.reasoning_summary == "I need one reading first."


def test_a_summary_is_never_mistaken_for_the_answer() -> None:
    """A response whose only text is a reasoning summary has no answer."""

    with pytest.raises(CloudReasonerError) as failure:
        _parse([_reasoning("Only reasoning here, no answer.")])

    assert failure.value.code == "malformed_response"


def test_a_summary_never_enters_a_tool_argument() -> None:
    turn = _parse(
        [
            _reasoning("Sensitive-looking reasoning text."),
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "get_sensor_value",
                "arguments": json.dumps({"entity": "filament_box_1"}),
            },
        ]
    )

    encoded = json.dumps(turn.tool_request.arguments)
    assert "Sensitive-looking" not in encoded
    assert turn.tool_request.arguments == {"entity": "filament_box_1"}


def test_summaries_from_several_provider_rounds_are_combined_once(
    tmp_path: Path,
) -> None:
    """Multi-round tool use yields one reasoning item per round. The reader
    gets one combined block, not a repeated fragment per round."""

    class MultiRound:
        available = True

        def __init__(self) -> None:
            self.rounds = 0

        def reason(self, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                return GeneralCloudTurn(
                    str(kwargs["model"]),
                    str(kwargs["effort"]),
                    0.01,
                    response_id="r1",
                    reasoning_summary="Shared preamble.",
                    stopping_reason="tool_call",
                    tool_request=__import__(
                        "butters.cloud.model", fromlist=["ToolRequest"]
                    ).ToolRequest("call_1", "get_sensor_value", {"entity": "filament_box_1"}),
                )
            return GeneralCloudTurn(
                str(kwargs["model"]),
                str(kwargs["effort"]),
                0.01,
                response_id="r2",
                response_text="Final answer.",
                reasoning_summary="Shared preamble.\n\nAnd the conclusion.",
            )

    service, _ = _service(
        tmp_path, adaptive=True, summaries=True, reasoner=MultiRound()
    )

    response = _ask(service, "Why is filament box 1 behaving oddly today, roughly?")

    if response.route == "general_cloud":
        assert response.response_text == "Final answer."
        assert response.reasoning_summary is not None
        assert response.reasoning_summary.count("Shared preamble.") == 1
        assert "And the conclusion." in response.reasoning_summary


# ============== 8. what the service response may claim ====================


def test_a_cloud_answer_reports_truthful_metadata(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path,
        adaptive=True,
        summaries=True,
        reasoner=General(text="The likely cause is X.", summary="I compared A, B, and C."),
    )

    response = _ask(service, ANALYSIS_PROMPT)
    payload = response.as_dict()

    assert payload["cloud_used"] is True
    assert payload["routing_mode"] == "adaptive"
    assert payload["routing_tier"] == "analysis"
    assert payload["model"] == "gpt-5.6-terra"
    assert payload["reasoning_effort"] == "high"
    assert payload["tier_escalated"] is False
    assert payload["routing_reason_codes"]
    assert payload["reasoning_summary"] == "I compared A, B, and C."
    # The answer is the answer. Nothing else was folded into it.
    assert payload["response_text"] == "The likely cause is X."


def test_a_local_answer_never_claims_a_model(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, adaptive=True, summaries=True)

    payload = _ask(service, "What is the humidity in filament box 1?").as_dict()

    assert payload["cloud_used"] is False
    assert payload["model"] is None
    assert payload["reasoning_effort"] is None
    assert payload["routing_tier"] is None
    assert payload["reasoning_summary"] is None
    assert payload["tier_escalated"] is False


def test_a_failed_cloud_request_does_not_claim_cloud_use(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, adaptive=True, budget=0.0)

    payload = _ask(service, DEEP_PROMPT).as_dict()

    assert payload["cloud_used"] is False
    assert payload["reasoning_summary"] is None


def test_the_summary_is_a_separate_field_not_appended_to_the_answer(
    tmp_path: Path,
) -> None:
    service, _ = _service(
        tmp_path,
        adaptive=True,
        summaries=True,
        reasoner=General(text="The likely cause is X.", summary="I compared A, B, and C."),
    )

    response = _ask(service, ANALYSIS_PROMPT)

    assert response.response_text == "The likely cause is X."
    assert "compared" not in response.response_text
    assert response.reasoning_summary == "I compared A, B, and C."


# =========== 9. the invariant that matters most: speech ===================

ANSWER = "The likely cause is X."
SUMMARY = "I compared A, B, and C, and C best explains the overnight change."


def test_the_stored_assistant_message_is_the_answer_alone(tmp_path: Path) -> None:
    """Speech synthesises the stored assistant message for the trace, so this
    string *is* the TTS input. It must equal the answer exactly."""

    service, _ = _service(
        tmp_path, adaptive=True, summaries=True, reasoner=General(text=ANSWER, summary=SUMMARY)
    )
    session = service.sessions.create()

    response = service.handle_text(session, ANALYSIS_PROMPT)

    spoken = next(
        item.text
        for item in reversed(session.messages)
        if item.role == "assistant" and item.trace_id == response.trace_id
    )
    assert spoken == ANSWER
    assert "compared" not in spoken
    assert SUMMARY not in spoken


def test_tts_speaks_the_answer_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression the brief asks for, held at the synthesis boundary."""

    from butters.web.speech import SpeechResult

    service, _ = _service(
        tmp_path, adaptive=True, summaries=True, reasoner=General(text=ANSWER, summary=SUMMARY)
    )
    session = service.sessions.create()
    response = service.handle_text(session, ANALYSIS_PROMPT)
    assert response.reasoning_summary == SUMMARY

    synthesized: list[str] = []

    def recording(text, preset, **_kwargs):
        synthesized.append(text)
        return SpeechResult(b"RIFF", "local", "local-piper", "kathleen", 0.01, 0.5)

    monkeypatch.setattr(service, "synthesize_preview", recording)
    service.synthesize_trace_response(session, response.trace_id)

    assert synthesized == [ANSWER]
    spoken = synthesized[0]
    for forbidden in (
        SUMMARY,
        "compared",
        "gpt-5.6",
        "terra",
        "Terra",
        "high",
        "analysis",
        "Analysis",
        "cloud",
        "Cloud",
        "escalat",
        "usd",
        "$",
    ):
        assert forbidden not in spoken, forbidden


def _spoken_text(tmp_path: Path, prompt: str) -> list[str]:
    """Everything synthesis was handed for one turn."""

    from butters.web.speech import SpeechResult

    service, _ = _service(
        tmp_path,
        adaptive=True,
        summaries=True,
        reasoner=General(text=ANSWER, summary=SUMMARY),
    )
    session = service.sessions.create()
    response = service.handle_text(session, prompt)
    if response.route != "general_cloud":
        return []
    spoken: list[str] = []

    def recording(text, preset, **_kwargs):
        spoken.append(text)
        return SpeechResult(b"RIFF", "local", "local-piper", "kathleen", 0.01, 0.5)

    service.synthesize_preview = recording  # type: ignore[method-assign]
    service.synthesize_trace_response(session, response.trace_id)
    return spoken


@pytest.mark.parametrize(
    "prompt", [LIGHT_PROMPT, BALANCED_PROMPT, ANALYSIS_PROMPT, DEEP_PROMPT]
)
def test_speech_carries_no_routing_metadata_at_any_tier(
    tmp_path: Path, prompt: str
) -> None:
    spoken = _spoken_text(tmp_path / re.sub(r"\W+", "_", prompt)[:24], prompt)

    assert spoken in ([], [ANSWER]), prompt


def test_nothing_appends_the_summary_to_the_response_text() -> None:
    """A structural guard: the service must never concatenate the summary
    into the answer, whatever a future edit is tempted to do."""

    source = (
        Path(__file__).parents[1] / "src/butters/web/service.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "response_text + turn.reasoning_summary",
        "response_text += ",
        "reasoning_summary + response_text",
    ):
        assert forbidden not in source, forbidden


# ============ 10. selection changes intelligence, not authority ===========


def test_a_complex_request_gains_no_extra_tools(tmp_path: Path) -> None:
    """A prompt becoming "deep" must not widen the provider tool boundary."""

    service, reasoner = _service(tmp_path, adaptive=True)

    _ask(service, LIGHT_PROMPT)
    _ask(service, DEEP_PROMPT)

    for call in reasoner.calls:
        for tool in call["tools"]:
            assert service.assistant.skills.get(str(tool["name"])) is not None


def test_a_deep_tier_cannot_run_a_tool_that_was_not_offered(tmp_path: Path) -> None:
    from butters.cloud.model import ToolRequest

    class Overreaching:
        available = True

        def __init__(self) -> None:
            self.calls = 0

        def reason(self, **kwargs):
            self.calls += 1
            return GeneralCloudTurn(
                str(kwargs["model"]),
                str(kwargs["effort"]),
                0.01,
                response_id="r",
                tool_request=ToolRequest("call_1", "run_shell_command", {"cmd": "id"}),
                stopping_reason="tool_call",
            )

    service, _ = _service(tmp_path, adaptive=True, reasoner=Overreaching())

    response = _ask(service, DEEP_PROMPT)

    assert response.route == "local_fallback"
    assert response.stopping_reason in {"tool_not_offered", "cloud_no_conclusion"}


def test_administrator_only_requests_are_still_refused_before_any_tier(
    tmp_path: Path,
) -> None:
    """Complexity is not authority: the administrator check runs first, and a
    refused request is never escalated to a paid model to try anyway."""

    service, reasoner = _service(tmp_path, adaptive=True)
    # Treat the skill this prompt matches as administrator-only. The guard,
    # not the particular skill, is what this test is about.
    service.assistant.skills.requires_administrator = (  # type: ignore[method-assign]
        lambda name: name == "get_sensor_value"
    )

    response = _ask(service, "What is the humidity in filament box 1?")

    assert reasoner.calls == []
    assert response.route == "unsupported"
    assert "administrator_required" in response.reason_codes
    assert response.cloud_used is False


def test_selection_never_touches_the_authorization_surface() -> None:
    """The routing module names models and text features. Nothing else.

    Identifiers are read from the parse tree, so the module may *describe*
    authorization in a comment while being unable to reference it in code.
    """

    import ast

    tree = ast.parse(
        (Path(__file__).parents[1] / "src/butters/cloud/adaptive.py").read_text(
            encoding="utf-8"
        )
    )
    identifiers = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    for forbidden in (
        "administrator",
        "passkey",
        "authorize",
        "authorized",
        "elevate",
        "ssh",
        "mqtt",
        "execute",
        "run",
    ):
        assert forbidden not in {name.casefold() for name in identifiers}, forbidden


# ======================== 11. the Chat surface ============================


def test_a_local_answer_renders_no_cloud_badge() -> None:
    """`cloud_used` gates the whole line, so a deterministic reply says
    nothing rather than announcing that no model ran."""

    assert 'if (!meta || meta.cloud_used !== true) return null;' in APP_JS


def test_the_metadata_line_reports_the_real_model_effort_and_tier() -> None:
    block = APP_JS[APP_JS.index("function cloudMetadata"):]
    block = block[: block.index("\n}\n")]

    assert "meta.model" in block
    assert "meta.reasoning_effort" in block
    assert "meta.routing_tier" in block
    assert "message-meta" in block


def test_escalating_is_claimed_only_when_a_tier_actually_changed() -> None:
    block = APP_JS[APP_JS.index("function cloudMetadata"):]
    block = block[: block.index("\n}\n")]

    assert 'meta.tier_escalated === true ? "Escalating reasoning" : "Using cloud reasoning"' in block


def test_the_reasoning_summary_is_separate_and_collapsed_by_default() -> None:
    block = APP_JS[APP_JS.index("function reasoningSummary"):]
    block = block[: block.index("\n}\n")]

    # A <details> with no `open` attribute is collapsed.
    assert 'createElement("details")' in block
    assert "open" not in block.replace("createElement", "")
    assert '"Reasoning summary"' in block
    # Named for what it is, and never called raw thinking.
    for forbidden in ("Raw thinking", "Chain of thought", "Internal thoughts"):
        assert forbidden not in APP_JS, forbidden


def test_a_missing_summary_produces_no_empty_panel() -> None:
    block = APP_JS[APP_JS.index("function reasoningSummary"):]
    block = block[: block.index("\n}\n")]

    assert "if (!text) return null;" in block


def test_the_summary_is_never_placed_in_the_answer_body() -> None:
    block = APP_JS[APP_JS.index("function addMessage"):]
    block = block[: block.index("\n}\n")]

    # The answer body carries `text` and nothing else; the metadata and the
    # summary are appended to the article as siblings of it.
    assert "renderAssistantMarkdown(text)" in block
    assert "paragraph.textContent = text;" in block  # the user branch
    assert "reasoning_summary" not in block


def test_the_surface_renders_text_rather_than_markup() -> None:
    """Provider text reaches the DOM as text, so a summary cannot inject
    markup or be mistaken for an interface element."""

    for function in ("cloudMetadata", "reasoningSummary"):
        block = APP_JS[APP_JS.index(f"function {function}"):]
        block = block[: block.index("\n}\n")]
        assert "innerHTML" not in block, function
        assert "textContent" in block, function


def test_the_metadata_is_visually_secondary_to_the_answer() -> None:
    meta = declarations(".message-meta", STYLESHEETS["chat.css"])

    assert meta["font-size"] == "var(--type-meta-size)"
    assert meta["color"] == "var(--text-muted)"


def test_the_summary_uses_the_design_system_and_survives_a_narrow_screen() -> None:
    # The summary is rendered through the safe Markdown path, so its styled
    # element is the rendered body rather than a bare paragraph.
    body = declarations(".reasoning-summary > .message-body", STYLESHEETS["chat.css"])
    trigger = declarations(".reasoning-summary > summary", STYLESHEETS["chat.css"])

    assert body["font-size"] == "var(--type-meta-size)"
    assert body["overflow-wrap"] == "anywhere"
    assert declarations(".message-meta", STYLESHEETS["chat.css"])["overflow-wrap"] == "anywhere"
    # Reachable with a thumb at iPhone width.
    assert trigger["min-height"] == "var(--tap-min)"


def test_the_hidden_invariant_is_still_in_force() -> None:
    assert declarations("[hidden]", STYLESHEETS["base.css"])["display"] == "none !important"


# ======================== 12. the Admin surface ===========================


def test_admin_offers_the_routing_mode_and_the_ceiling() -> None:
    assert 'id="chat-routing-mode"' in ADMIN_HTML
    assert 'id="chat-max-tier"' in ADMIN_HTML
    assert 'id="chat-summary"' in ADMIN_HTML
    assert 'id="chat-routing-note"' in ADMIN_HTML


def test_the_fixed_controls_are_kept_for_debugging() -> None:
    """Fixed mode remains available and its controls are not removed."""

    for control in ("chat-model", "chat-effort", "chat-provider"):
        assert f'id="{control}"' in ADMIN_HTML, control
    assert '"fixed"' in ADMIN_JS


def test_adaptive_mode_says_the_fixed_controls_are_not_the_selector() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function applyRoutingMode"):]
    block = block[: block.index("\n}\n")]

    assert "are not the per-request selector" in block
    assert "selected per request from local complexity" in block


def test_the_ceiling_is_shown_only_where_it_applies() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function applyRoutingMode"):]
    block = block[: block.index("\n}\n")]

    assert 'show(document.querySelector("#chat-max-tier-field"), adaptive)' in block


def test_the_effective_headline_reports_the_routing_mode() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function effectiveChatSummary"):]
    block = block[: block.index("\n}\n")]

    # Reporting the saved model as "effective" would be untrue in Adaptive
    # mode, because no single model is what a request uses.
    assert "adaptive routing" in block
    assert "ceiling" in block
    assert "fixed routing" in block


def test_a_profile_with_no_saved_mode_displays_as_fixed() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function renderChatRouting"):]
    block = block[: block.index("\n}\n")]

    assert 'saved && saved.routing_mode ? saved.routing_mode : "fixed"' in block


def test_admin_submits_the_routing_settings() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function chatBody"):]
    block = block[: block.index("\n}\n")]

    assert "routing_mode:" in block
    assert "max_automatic_tier:" in block
    assert "reasoning_summary_enabled:" in block


def test_capability_validation_is_not_removed() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function chatBody"):]
    block = block[: block.index("\n}\n")]

    assert "supports.reasoning_effort" in block
    assert "supports.verbosity" in block
    assert "const supports = model.supports;" in block


def test_the_catalog_publishes_the_routing_vocabulary() -> None:
    registry = build_registry(load_assistant_settings())
    catalog = registry.as_dict()

    assert catalog["routing_modes"] == ["adaptive", "fixed"]
    assert catalog["automatic_tiers"] == ["luna", "terra", "sol"]
    for provider in catalog["chat_providers"]:
        for model in provider["chat_models"]:
            assert "reasoning_summary" in model["supports"]


def test_the_defaults_are_the_conservative_ones() -> None:
    assert DEFAULT_ROUTING_MODE == "fixed"
    assert DEFAULT_MAX_AUTOMATIC_TIER == "sol"


# ========= 13. coexistence with provider billing reconciliation ===========
#
# Two accounting systems now exist and they answer different questions. The
# local ledger is an admission ceiling: it decides, before a request runs,
# whether Butters may spend. Provider reconciliation is the bill: it reports,
# afterwards, what OpenAI actually charged. Adaptive routing depends on the
# first and must stay entirely independent of the second.


def test_admission_is_decided_by_the_local_ledger_alone() -> None:
    """Every spend gate is the local ledger. None consults the provider."""

    source = (
        Path(__file__).parents[1] / "src/butters/web/service.py"
    ).read_text(encoding="utf-8")

    gates = re.findall(r"if not ([\w.]+)\.permits\(", source)
    assert gates, "expected the cloud paths to gate on a ledger"
    assert set(gates) == {"self.ledger"}


def test_adaptive_routing_never_touches_the_usage_admin_credential() -> None:
    """The Admin billing key can create keys and move spend limits. Nothing
    on the inference path may reach it."""

    import ast

    routing = (
        Path(__file__).parents[1] / "src/butters/cloud/adaptive.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(routing)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    for forbidden in (
        "butters.cloud.usage_admin_credential",
        "butters.cloud.provider_accounting",
    ):
        assert forbidden not in imported, forbidden
    for forbidden in ("usage_admin", "UsageAdmin", "ProviderAccounting", "Costs"):
        assert forbidden not in routing, forbidden


def test_provider_reporting_is_read_only() -> None:
    """Reconciliation observes. It issues no request that could change
    anything at the provider, and it is not in a position to gate a call."""

    accounting = Path(__file__).parents[1] / "src/butters/cloud/provider_accounting.py"
    if not accounting.exists():  # pragma: no cover - before the billing branch
        pytest.skip("provider accounting is not present on this branch")
    body = accounting.read_text(encoding="utf-8")

    methods = set(re.findall(r'method="(\w+)"', body))
    assert methods <= {"GET"}, methods
    assert ".permits(" not in body


def test_the_two_ledgers_stay_separate_under_adaptive_routing(
    tmp_path: Path,
) -> None:
    """An adaptive turn writes a local estimate and nothing else. It does not
    produce, consume, or wait on a provider billing snapshot."""

    service, _ = _service(tmp_path, adaptive=True)

    response = _ask(service, DEEP_PROMPT)

    assert response.model == "gpt-5.6-sol"
    rows = [row for row in service.ledger.records if row.request_category == "general"]
    assert rows, "the adaptive turn should have written a local estimate"
    # The local row prices the selected model with Butters' own reviewed
    # rates; it never claims to be a provider-reported charge.
    assert all(row.model == "gpt-5.6-sol" for row in rows)
    assert all(row.provider == "openai" for row in rows)
    assert all(row.estimated_cost_usd > 0 for row in rows)
