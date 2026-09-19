"""Reviewed, per-model pricing with the billing dimension each model actually uses.

Butters previously held one pricing shape - input/cached/output tokens per
million - and a single free-floating
`providers.cloud_tts_price_per_million_characters_usd`. Both were wrong for
speech. `tts-1` and `tts-1-hd` are billed per input *character*;
`gpt-4o-mini-tts` is billed per text input *token* plus per audio output
*token*. Forcing all three through one character rate would have required
inventing a character equivalent for a model OpenAI does not price that way.

So a model declares its own billing dimensions, and the ledger asks the model
how to cost a usage record rather than assuming.

Just as important is what Butters can actually observe. `POST /v1/audio/speech`
returns audio bytes and no usage object, so for `gpt-4o-mini-tts` neither the
text input tokens nor the audio output tokens are reported back. Butters
therefore cannot state that model's cost precisely, and it says so
(`CostBasis.ESTIMATED_UPPER_BOUND`) rather than presenting a fabricated figure.
Audio tokens are never derived from encoded audio size: OpenAI defines no such
conversion, and inventing one would be a guess wearing the costume of a
measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Verified against the official OpenAI pricing documentation on this date.
# Updating a rate means updating this constant in the same change, so a stale
# table cannot masquerade as a current one.
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICING_DATE = "2026-09-19"


class CostBasis(StrEnum):
    """How much trust a recorded cost figure deserves.

    Kept explicit because these are genuinely different claims, and collapsing
    them would let an upper bound be read as a measurement.
    """

    # Every billable dimension came from the provider's own usage report.
    PROVIDER_REPORTED = "provider_reported"
    # Every billable dimension is exactly known from the request Butters sent
    # (for example characters submitted to a character-priced model).
    INPUT_MEASURED = "input_measured"
    # At least one billable dimension is not observable; the figure is a
    # deliberate over-estimate used to keep budgets fail-closed.
    ESTIMATED_UPPER_BOUND = "estimated_upper_bound"
    # No trustworthy figure could be produced.
    UNAVAILABLE = "unavailable"
    # Rows written before the basis was recorded.
    UNRECORDED = "unrecorded"


@dataclass(frozen=True, slots=True)
class TokenPricing:
    """A text model billed on input, cached input, and output tokens."""

    input_per_million_usd: float
    cached_input_per_million_usd: float
    output_per_million_usd: float
    # OpenAI bills a cache write at 1.25x the uncached input rate.
    cache_write_multiplier: float = 1.25

    def cost(
        self,
        *,
        input_tokens: int,
        cached_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
    ) -> float:
        uncached = max(0, input_tokens - cached_tokens - cache_write_tokens)
        return (
            uncached * self.input_per_million_usd
            + cached_tokens * self.cached_input_per_million_usd
            + cache_write_tokens
            * self.input_per_million_usd
            * self.cache_write_multiplier
            + output_tokens * self.output_per_million_usd
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class CharacterPricing:
    """A speech model billed per million input characters (tts-1 family).

    Butters sends the text, so the billable quantity is known exactly before
    and after the request. Cost for these models is measured, not estimated.
    """

    input_per_million_characters_usd: float

    def cost(self, *, characters: int) -> float:
        return max(0, characters) * self.input_per_million_characters_usd / 1_000_000


@dataclass(frozen=True, slots=True)
class SpeechTokenPricing:
    """A speech model billed on text input tokens and audio output tokens.

    `/v1/audio/speech` returns audio, not usage, so neither dimension comes
    back from the provider. `upper_bound` exists to keep budget admission
    fail-closed; it is a ceiling for refusing requests, never a billing
    figure, and every cost it produces is labelled
    `CostBasis.ESTIMATED_UPPER_BOUND`.
    """

    text_input_per_million_usd: float
    audio_output_per_million_usd: float
    # Reviewed worst-case shape factors, used only to bound a request:
    #   * one token per two characters is pessimistic for English text;
    #   * audio output tokens per input character is the dominant term, set
    #     well above observed ratios so the ceiling cannot under-reserve.
    # Neither is presented to an operator as a measurement.
    worst_case_input_tokens_per_character: float = 0.5
    worst_case_audio_tokens_per_character: float = 8.0

    def cost_from_reported_usage(
        self, *, text_input_tokens: int, audio_output_tokens: int
    ) -> float:
        return (
            max(0, text_input_tokens) * self.text_input_per_million_usd
            + max(0, audio_output_tokens) * self.audio_output_per_million_usd
        ) / 1_000_000

    def upper_bound(self, *, characters: int) -> float:
        characters = max(0, characters)
        return self.cost_from_reported_usage(
            text_input_tokens=int(characters * self.worst_case_input_tokens_per_character) + 1,
            audio_output_tokens=int(characters * self.worst_case_audio_tokens_per_character) + 1,
        )


SpeechPricing = CharacterPricing | SpeechTokenPricing

# Reviewed chat pricing. `CloudSettings.validated()` pins the model IDs, so a
# model cannot appear here without a deliberate configuration change, and a
# model absent from here is denied before any HTTP call.
CHAT_PRICING: dict[str, TokenPricing] = {
    "gpt-5.6-luna": TokenPricing(0.20, 0.02, 1.20),
    "gpt-5.6-terra": TokenPricing(2.00, 0.20, 12.00),
    "gpt-5.6-sol": TokenPricing(4.00, 0.40, 20.00),
}

# Reviewed speech pricing, one entry per supported model, each carrying the
# dimension OpenAI actually bills. Server-authoritative: no Admin form writes
# here, and a model absent from this mapping cannot be used for paid speech.
SPEECH_PRICING: dict[str, SpeechPricing] = {
    "tts-1": CharacterPricing(15.00),
    "tts-1-hd": CharacterPricing(30.00),
    "gpt-4o-mini-tts": SpeechTokenPricing(0.60, 12.00),
}


@dataclass(frozen=True, slots=True)
class SpeechCost:
    """A speech cost together with how much it should be trusted."""

    amount_usd: float
    basis: CostBasis
    detail: str

    @property
    def reconciliation_required(self) -> bool:
        return self.basis is CostBasis.ESTIMATED_UPPER_BOUND

    def as_dict(self) -> dict[str, object]:
        return {
            "amount_usd": self.amount_usd,
            "basis": str(self.basis),
            "detail": self.detail,
            "reconciliation_required": self.reconciliation_required,
        }


def speech_pricing(model: str) -> SpeechPricing | None:
    return SPEECH_PRICING.get(model)


def speech_cost(
    model: str,
    *,
    characters: int,
    reported_text_input_tokens: int | None = None,
    reported_audio_output_tokens: int | None = None,
) -> SpeechCost:
    """Cost one speech request, stating plainly how the figure was obtained."""

    pricing = SPEECH_PRICING.get(model)
    if pricing is None:
        return SpeechCost(
            float("inf"),
            CostBasis.UNAVAILABLE,
            f"{model} has no reviewed speech pricing",
        )
    if isinstance(pricing, CharacterPricing):
        return SpeechCost(
            pricing.cost(characters=characters),
            CostBasis.INPUT_MEASURED,
            f"{characters} submitted characters at "
            f"${pricing.input_per_million_characters_usd:.2f}/1M characters",
        )
    if reported_text_input_tokens is not None and reported_audio_output_tokens is not None:
        # Reserved for a future speech response that reports usage. Nothing in
        # the current client populates these, so this branch is unreachable in
        # production today - deliberately, rather than by being omitted.
        return SpeechCost(
            pricing.cost_from_reported_usage(
                text_input_tokens=reported_text_input_tokens,
                audio_output_tokens=reported_audio_output_tokens,
            ),
            CostBasis.PROVIDER_REPORTED,
            f"{reported_text_input_tokens} text input and "
            f"{reported_audio_output_tokens} audio output tokens reported by the provider",
        )
    return SpeechCost(
        pricing.upper_bound(characters=characters),
        CostBasis.ESTIMATED_UPPER_BOUND,
        "the speech endpoint reports no usage, so this is a conservative "
        "ceiling from the submitted text, not a measured charge",
    )
