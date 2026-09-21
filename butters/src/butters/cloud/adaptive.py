"""Deterministic, local, inspectable cloud model/effort selection for Chat.

Ordinary Butters Chat used to send one fixed pair - whatever Admin had saved,
in practice ``gpt-5.6-terra`` at ``high`` - for essentially every question that
the deterministic router could not answer. A trivial "why does X happen"
therefore cost the same reasoning tier as a genuine multi-hypothesis
investigation.

This module chooses the tier instead, from features the request already
produces locally. Three rules shape everything below:

1. **No model decides which model to call.** Selection is pure text and route
   inspection. There is no classifier request, no hidden provider round trip,
   and no network call of any kind on this path.
2. **Every decision is explainable.** A decision carries the reason codes that
   produced it, so "why did this prompt use Terra/high?" is answered from the
   trace rather than from intuition.
3. **Selection changes intelligence, never authority.** A tier is a model name
   and an effort string. It grants no tool, no action, no privilege, and it is
   computed after the administrator and policy checks the service already
   performs.

The ladder is deliberately *not* an escalation ladder. Paying for Luna before
Terra on a request that was obviously moderate wastes money, so the initial
tier is chosen directly from complexity and one call is made at it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from butters.assistant_config import CloudSettings
from butters.cloud.model import EscalationLevel

# Longest prompt that still counts as "short" for the weak length signal. A
# prompt can never reach a paid tier on length alone: the bonus is one point
# and the LIGHT band is two points wide.
LONG_PROMPT_WORDS = 35


class CloudTier(IntEnum):
    """The six reviewed tiers ordinary Chat may select between.

    This is a *Chat* ladder and is intentionally separate from
    :class:`~butters.cloud.model.EscalationLevel`, which is the diagnostic
    subsystem's evidence-aware ladder and has different rungs.
    :meth:`escalation_level` projects one onto the other so the usage ledger
    keeps recording the single integer it already records.
    """

    LOCAL = 0
    LIGHT = 1
    BALANCED = 2
    ANALYSIS = 3
    DEEP = 4
    MAXIMUM = 5

    @property
    def escalation_level(self) -> EscalationLevel:
        """The ledger's existing level whose model class matches this tier."""

        return _LEDGER_LEVELS[self]


_LEDGER_LEVELS: dict[CloudTier, EscalationLevel] = {
    CloudTier.LOCAL: EscalationLevel.LOCAL,
    CloudTier.LIGHT: EscalationLevel.LIGHT,
    # BALANCED and ANALYSIS are both Terra; the ledger's ANALYSIS rung is the
    # Terra rung, so both project onto it rather than inventing a sixth value
    # in a column other rows already use.
    CloudTier.BALANCED: EscalationLevel.ANALYSIS,
    CloudTier.ANALYSIS: EscalationLevel.ANALYSIS,
    CloudTier.DEEP: EscalationLevel.DEEP,
    CloudTier.MAXIMUM: EscalationLevel.MAXIMUM,
}

ROUTING_MODES: tuple[str, ...] = ("adaptive", "fixed")
# Ordered weakest to strongest. The administrator's ceiling names one of these.
TIER_MODELS: tuple[str, ...] = ("luna", "terra", "sol")

# Unset routing mode means the configuration predates this feature, and a
# configuration that predates the feature must keep behaving exactly as it did.
DEFAULT_ROUTING_MODE = "fixed"
# Unset ceiling means "the whole reviewed ladder". Adaptive mode is opt-in, the
# MAXIMUM rung has its own separate gate, and every request is still bounded by
# the per-request, daily, and monthly cost ceilings, so the default ceiling
# does not need to be the thing that keeps spend down.
DEFAULT_MAX_AUTOMATIC_TIER = "sol"


def _words(*terms: str) -> re.Pattern[str]:
    """Word-boundary alternation, so "cause" does not fire inside "because"
    by accident and multi-word terms still match as written."""

    return re.compile(r"\b(?:" + "|".join(re.escape(item) for item in terms) + r")\b")


_CAUSAL = _words(
    "why", "cause", "causes", "caused", "causing", "root cause",
    "explanation", "explanations", "due to", "leads to", "results in",
)
_DISCRIMINATION = _words(
    "distinguish", "differentiate", "rule out", "competing", "hypothesis",
    "hypotheses", "alternative", "alternatives", "versus", "vs",
    "most supported", "which explanation",
)
_EVIDENCE = _words(
    "evidence", "reading", "readings", "measurement", "measurements",
    "sample", "samples", "telemetry", "observation", "observations",
)
# Deliberately narrow. A bare "error" is ordinary English - "sensor error" is a
# hypothesis, not a log - and treating it as log interpretation would push
# ordinary comparison prompts two points up the ladder.
_LOGS = _words(
    "logs", "log file", "logfile", "traceback", "stack trace", "exception",
    "stderr", "error message", "error log", "core dump", "unfamiliar error",
)
_UNRESOLVED = _words(
    "contradictory", "conflicting", "inconsistent", "unresolved",
    "does not add up", "doesn't add up", "no clear cause", "still unknown",
)
_EXHAUSTIVE = _words(
    "exhaustive", "exhaustively", "every possible", "every supported",
    "all possible", "leave no stone", "deep dive", "as thoroughly as possible",
)
_CONJUNCTIONS = (" and ", " also ", " then ")


def classify_complexity(
    text: str,
    *,
    route_matched: bool,
    aggregate: bool,
    missing_arguments: tuple[str, ...] | list[str],
    diagnostic_recognized: bool,
    admin_override: str | None,
) -> dict[str, object]:
    """The single local complexity classification for one request.

    The first block of keys is the classification Butters already emitted on
    the ``complexity`` trace stage and carried on ``RouteDecision.features``;
    it is reproduced byte-for-byte so nothing that reads a trace changes
    meaning. The second block is what adaptive tier selection adds.
    """

    lowered = text.casefold()
    # Occurrences, not distinct conjunction words: "a and b and c" asks for
    # more than "a and b" does, and the legacy `single_operation` key below
    # keeps its original presence-only meaning either way.
    conjunctions = sum(lowered.count(word) for word in _CONJUNCTIONS)
    words = len(lowered.split())
    return {
        # ----- the pre-existing classification, unchanged -----------------
        "deterministic_route_matched": route_matched,
        "complete_required_slots": route_matched and not missing_arguments,
        "missing_required_arguments": list(missing_arguments),
        "single_operation": len(
            [word for word in _CONJUNCTIONS if word in lowered]
        )
        == 0,
        "historical_data_required": any(
            word in lowered for word in ("history", "trend", "yesterday", "baseline")
        ),
        "comparison_or_aggregation": aggregate
        or any(
            word in lowered
            for word in ("compare", "most", "highest", "average", "mean")
        ),
        "diagnostic_domain_recognized": diagnostic_recognized,
        "open_ended_causal_request": any(
            word in lowered
            for word in ("why", "might", "causing", "caused", "affected")
        ),
        "external_general_knowledge_required": not diagnostic_recognized
        and not route_matched
        and words > 4,
        "admin_override": admin_override,
        # ----- added for adaptive tier selection --------------------------
        "causal_language": bool(_CAUSAL.search(lowered)),
        "hypothesis_discrimination": bool(_DISCRIMINATION.search(lowered)),
        "evidence_interpretation_required": bool(_EVIDENCE.search(lowered)),
        "logs_need_interpretation": bool(_LOGS.search(lowered)),
        "unresolved_or_contradictory": bool(_UNRESOLVED.search(lowered)),
        "explicit_exhaustive_request": bool(_EXHAUSTIVE.search(lowered)),
        "requested_operation_count": conjunctions + 1,
        "competing_candidate_count": _competing_candidates(lowered),
        "prompt_word_count": words,
    }


def _competing_candidates(lowered: str) -> int:
    """How many alternatives an enumerated list appears to offer.

    Only a list that is actually joined - "a, b, and c" - counts. A comma used
    for ordinary punctuation is not an enumeration, and a prompt with no joined
    list scores zero rather than one.
    """

    if ", and " not in lowered and ", or " not in lowered:
        return 0
    return lowered.count(",") + 1


# Each entry is (feature key, points, reason code). Points are small integers
# so a decision can be recomputed by hand from the reason codes alone.
_SCORE_RULES: tuple[tuple[str, int, str], ...] = (
    ("causal_language", 1, "causal_language"),
    ("comparison_or_aggregation", 1, "comparison_or_aggregation"),
    ("historical_data_required", 1, "historical_data_required"),
    ("evidence_interpretation_required", 1, "evidence_interpretation_required"),
    ("hypothesis_discrimination", 1, "hypothesis_discrimination"),
    ("diagnostic_domain_recognized", 1, "diagnostic_domain_recognized"),
    ("logs_need_interpretation", 2, "logs_need_interpretation"),
    ("unresolved_or_contradictory", 2, "unresolved_or_contradictory"),
)

# Score bands. Inclusive lower bound, ordered strongest first.
_BANDS: tuple[tuple[int, CloudTier], ...] = (
    (7, CloudTier.DEEP),
    (4, CloudTier.ANALYSIS),
    (2, CloudTier.BALANCED),
    (0, CloudTier.LIGHT),
)

# What actually distinguishes a DEEP request from a merely analytical one.
# "Compare three explanations and say what evidence separates them" is an
# analysis; contradictory evidence, log interpretation, and a genuinely wide
# hypothesis space are the cases the brief reserves Sol for. Without this,
# adding one causal word to an ordinary comparison would buy a Sol call.
_DEEP_MARKERS: tuple[str, ...] = (
    "logs_need_interpretation",
    "unresolved_or_contradictory",
)
_DEEP_MARKER_MINIMUM_SCORE = 5


@dataclass(frozen=True, slots=True)
class CloudRoutingDecision:
    """One request's model and effort, and the reasoning that produced them."""

    tier: CloudTier
    model: str
    effort: str
    reason_codes: tuple[str, ...]
    estimated_complexity: int
    mode: str
    # True only when the ceiling actually moved the request down a rung, so the
    # UI and the trace can tell "chose Terra" from "wanted Sol, allowed Terra".
    ceiling_applied: bool = False

    @property
    def tier_name(self) -> str:
        return self.tier.name.casefold()

    def as_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier_name,
            "model": self.model,
            "effort": self.effort,
            "reason_codes": list(self.reason_codes),
            "estimated_complexity": self.estimated_complexity,
            "mode": self.mode,
            "ceiling_applied": self.ceiling_applied,
        }


class AdaptiveCloudRouter:
    """Maps local complexity onto the reviewed Luna/Terra/Sol ladder."""

    def __init__(self, settings: CloudSettings) -> None:
        self.settings = settings

    # ----- public API -----------------------------------------------------

    def fixed(self, model: str, effort: str, *, reason: str = "fixed_mode") -> CloudRoutingDecision:
        """The administrator's exact saved or forced pair, unchanged.

        Fixed mode and the Admin route overrides both come through here, so a
        request that was explicitly configured is never silently re-tiered.
        """

        return CloudRoutingDecision(
            self._tier_for_model(model),
            model,
            effort,
            (reason,),
            0,
            "fixed",
        )

    def select(
        self,
        features: dict[str, object],
        *,
        max_automatic_tier: str | None,
        fallback_effort: str,
    ) -> CloudRoutingDecision:
        """Choose a tier for one ordinary Chat request."""

        score, reasons = self._score(features)
        tier = self._band(score)
        if tier is CloudTier.ANALYSIS and score >= _DEEP_MARKER_MINIMUM_SCORE:
            marker = any(features.get(key) for key in _DEEP_MARKERS) or (
                int(features.get("competing_candidate_count") or 0) >= 5
            )
            if marker:
                tier = CloudTier.DEEP
                reasons.append("deep_evidence_marker")
        exhaustive = bool(features.get("explicit_exhaustive_request"))
        if exhaustive:
            reasons.append("explicit_exhaustive_request")
            if self.settings.allow_automatic_maximum:
                tier = CloudTier.MAXIMUM
            else:
                # The reviewed automatic-maximum policy is a separate approval
                # and this feature does not widen it. The request still gets
                # the deepest tier the policy does allow, and says why.
                tier = max(tier, CloudTier.DEEP)
                reasons.append("automatic_maximum_not_permitted")
        ceiling = self._ceiling(max_automatic_tier)
        capped = min(tier, ceiling)
        if capped != tier:
            reasons.append("ceiling_applied")
        model, effort = self._configuration(capped, score, fallback_effort)
        return CloudRoutingDecision(
            capped,
            model,
            effort,
            tuple(reasons),
            score,
            "adaptive",
            ceiling_applied=capped != tier,
        )

    # ----- internals ------------------------------------------------------

    def _score(self, features: dict[str, object]) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        for key, points, code in _SCORE_RULES:
            if features.get(key):
                score += points
                reasons.append(code)
        if int(features.get("requested_operation_count") or 1) >= 3:
            score += 1
            reasons.append("multiple_operations")
        candidates = int(features.get("competing_candidate_count") or 0)
        if candidates >= 5:
            score += 3
            reasons.append("many_competing_candidates")
        elif candidates >= 3:
            score += 1
            reasons.append("competing_candidates_enumerated")
        # Length is the weakest signal Butters has and is capped at one point
        # so that a long but simple request - "explain this in detail" padded
        # out - can never climb past the LIGHT band on length alone.
        if int(features.get("prompt_word_count") or 0) > LONG_PROMPT_WORDS:
            score += 1
            reasons.append("long_prompt_weak_signal")
        return score, reasons

    @staticmethod
    def _band(score: int) -> CloudTier:
        for threshold, tier in _BANDS:
            if score >= threshold:
                return tier
        return CloudTier.LIGHT

    def _ceiling(self, max_automatic_tier: str | None) -> CloudTier:
        name = (max_automatic_tier or DEFAULT_MAX_AUTOMATIC_TIER).casefold()
        if name == "luna":
            return CloudTier.LIGHT
        if name == "terra":
            return CloudTier.ANALYSIS
        return CloudTier.MAXIMUM

    def _configuration(
        self, tier: CloudTier, score: int, fallback_effort: str
    ) -> tuple[str, str]:
        if tier is CloudTier.LIGHT:
            return self.settings.luna_model, "medium"
        if tier is CloudTier.BALANCED:
            return self.settings.terra_model, "medium"
        if tier is CloudTier.ANALYSIS:
            return self.settings.terra_model, "high"
        if tier is CloudTier.DEEP:
            # "Use the lower sufficient effort": xhigh is reserved for the
            # requests whose evidence genuinely piles up, not for every Sol.
            return self.settings.sol_model, "xhigh" if score >= 8 else "high"
        if tier is CloudTier.MAXIMUM:
            return self.settings.sol_model, "max"
        return self.settings.luna_model, fallback_effort

    def _tier_for_model(self, model: str) -> CloudTier:
        if model == self.settings.luna_model:
            return CloudTier.LIGHT
        if model == self.settings.sol_model:
            return CloudTier.DEEP
        return CloudTier.ANALYSIS
