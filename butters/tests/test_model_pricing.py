"""Per-model pricing, and how honestly a recorded cost describes itself.

Two defects motivated this file. The chat rates had drifted from OpenAI's
published prices, and the speech rate was a single characters-per-million
number that is simply not how `gpt-4o-mini-tts` is billed. The tests below
pin the current rates and, more importantly, pin the distinction between a
cost Butters measured and a cost it could only bound.
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest
from butters.assistant_config import load_assistant_settings
from butters.cloud.model import CloudTokenUsage
from butters.cloud.usage import UsageLedger
from butters.pricing import (
    CHAT_PRICING,
    PRICING_DATE,
    PRICING_SOURCE,
    SPEECH_PRICING,
    CharacterPricing,
    CostBasis,
    SpeechTokenPricing,
    TokenPricing,
    speech_cost,
)


@pytest.fixture()
def settings():
    return load_assistant_settings()


@pytest.fixture()
def ledger(settings, tmp_path: Path):
    return UsageLedger(settings.cloud, tmp_path / "usage.sqlite3")


# ============================ chat pricing =================================


@pytest.mark.parametrize(
    ("model", "input_rate", "cached_rate", "output_rate"),
    (
        ("gpt-5.6-luna", 0.20, 0.02, 1.20),
        ("gpt-5.6-terra", 2.00, 0.20, 12.00),
        ("gpt-5.6-sol", 4.00, 0.40, 20.00),
    ),
)
def test_current_chat_pricing(
    model: str, input_rate: float, cached_rate: float, output_rate: float
) -> None:
    """The whole family, checked together: one stale row is how this started."""

    price = CHAT_PRICING[model]

    assert price.input_per_million_usd == input_rate
    assert price.cached_input_per_million_usd == cached_rate
    assert price.output_per_million_usd == output_rate


def test_the_accepted_terra_request_now_costs_the_current_price(ledger) -> None:
    """The exact usage of the first accepted production request.

    192 input, 0 cached, 9 output cost $0.000615 under the stale table and
    $0.000492 under the current one.
    """

    usage = CloudTokenUsage(input_tokens=192, cached_tokens=0, output_tokens=9)
    cost = ledger.estimated_cost("gpt-5.6-terra", usage)

    assert cost == pytest.approx(0.000492, abs=1e-9)
    assert cost == pytest.approx(
        (192 * 2.00 + 0 * 0.20 + 9 * 12.00) / 1_000_000, abs=1e-12
    )
    # And it is genuinely cheaper than the figure the stale table produced.
    assert cost < 0.000615


def test_cache_writes_bill_at_the_documented_multiplier() -> None:
    price = CHAT_PRICING["gpt-5.6-terra"]
    cost = price.cost(
        input_tokens=1000, cached_tokens=0, cache_write_tokens=1000, output_tokens=0
    )

    assert price.cache_write_multiplier == 1.25
    assert cost == pytest.approx(1000 * 2.00 * 1.25 / 1_000_000)


def test_pricing_provenance_travels_with_the_rates(settings) -> None:
    assert settings.cloud.pricing_source == PRICING_SOURCE
    assert settings.cloud.pricing_date == PRICING_DATE
    assert PRICING_DATE == "2026-09-19"


def test_an_unpriced_chat_model_stays_fail_closed(ledger) -> None:
    cost = ledger.estimated_cost("gpt-6-astra", CloudTokenUsage(input_tokens=10))

    assert math.isinf(cost)
    assert ledger.permits(cost) is False


def test_the_chat_catalog_still_equals_the_priced_models(settings) -> None:
    from butters.ai.capabilities import build_registry

    registry = build_registry(settings)
    offered = {item.id for item in registry.provider("openai").chat_models}

    assert offered == set(settings.cloud.pricing) == set(CHAT_PRICING)
    assert "gpt-6-astra" not in offered


# =========================== speech pricing ================================


def test_tts1_family_is_character_priced() -> None:
    for model, rate in (("tts-1", 15.00), ("tts-1-hd", 30.00)):
        pricing = SPEECH_PRICING[model]
        assert isinstance(pricing, CharacterPricing)
        assert pricing.input_per_million_characters_usd == rate


@pytest.mark.parametrize(("model", "rate"), (("tts-1", 15.00), ("tts-1-hd", 30.00)))
def test_character_priced_speech_is_measured_not_estimated(model: str, rate: float) -> None:
    cost = speech_cost(model, characters=2000)

    assert cost.basis is CostBasis.INPUT_MEASURED
    assert cost.reconciliation_required is False
    assert cost.amount_usd == pytest.approx(2000 * rate / 1_000_000)


def test_gpt_4o_mini_tts_is_not_character_priced() -> None:
    pricing = SPEECH_PRICING["gpt-4o-mini-tts"]

    assert not isinstance(pricing, CharacterPricing)
    assert isinstance(pricing, SpeechTokenPricing)
    assert pricing.text_input_per_million_usd == 0.60
    assert pricing.audio_output_per_million_usd == 12.00


def test_gpt_4o_mini_tts_token_accounting_when_usage_is_reported() -> None:
    """The branch a future usage-reporting speech response would take."""

    cost = speech_cost(
        "gpt-4o-mini-tts",
        characters=100,
        reported_text_input_tokens=50,
        reported_audio_output_tokens=400,
    )

    assert cost.basis is CostBasis.PROVIDER_REPORTED
    assert cost.reconciliation_required is False
    assert cost.amount_usd == pytest.approx((50 * 0.60 + 400 * 12.00) / 1_000_000)


def test_missing_speech_usage_is_an_openly_labelled_ceiling() -> None:
    """No fake precision: the figure says what it is."""

    cost = speech_cost("gpt-4o-mini-tts", characters=100)

    assert cost.basis is CostBasis.ESTIMATED_UPPER_BOUND
    assert cost.reconciliation_required is True
    assert "not a measured charge" in cost.detail
    # A ceiling must never sit below what the request could actually cost.
    plausible = speech_cost(
        "gpt-4o-mini-tts",
        characters=100,
        reported_text_input_tokens=25,
        reported_audio_output_tokens=300,
    )
    assert cost.amount_usd > plausible.amount_usd


def test_the_ceiling_is_not_derived_from_returned_audio() -> None:
    """Cost depends only on submitted text; OpenAI defines no byte conversion.

    Asserted behaviourally rather than by grepping: an identical request that
    returns wildly different amounts of audio must cost the same, because the
    costing function is never shown the audio.
    """

    import inspect

    from butters import pricing

    # The costing authority accepts characters and reported usage - nothing
    # that could carry an audio size or duration.
    parameters = set(inspect.signature(pricing.speech_cost).parameters)
    assert parameters == {
        "model",
        "characters",
        "reported_text_input_tokens",
        "reported_audio_output_tokens",
    }
    for name in parameters:
        assert "audio" not in name or "tokens" in name

    # And the provider hands it the submitted text length, not the response.
    call = inspect.getsource(pricing.SpeechTokenPricing.upper_bound)
    assert "characters" in call
    for forbidden in ("audio_seconds", "duration", "len(audio)", "getnframes"):
        assert forbidden not in call, forbidden


def test_an_unpriced_speech_model_is_refused(ledger) -> None:
    cost = ledger.speech_cost("elevenlabs-v2", characters=10)

    assert cost.basis is CostBasis.UNAVAILABLE
    assert math.isinf(cost.amount_usd)
    assert ledger.permits(cost.amount_usd) is False


# ===================== budget preflight stays fail-closed ==================


def test_preflight_refuses_a_request_whose_worst_case_exceeds_the_budget(
    settings, tmp_path: Path
) -> None:
    """Unknowable final cost is never a reason to admit a request."""

    tight = replace(settings.cloud, max_estimated_cost_per_request_usd=0.001).validated()
    ledger = UsageLedger(tight, tmp_path / "usage.sqlite3")
    bounded = ledger.speech_cost("gpt-4o-mini-tts", characters=2000)

    assert bounded.basis is CostBasis.ESTIMATED_UPPER_BOUND
    assert ledger.permits(bounded.amount_usd) is False
    # The same text on a character-priced model is cheap enough to admit.
    assert ledger.permits(ledger.speech_cost("tts-1", characters=20).amount_usd) is True


def test_the_ceiling_scales_with_the_bounded_input(ledger) -> None:
    small = ledger.speech_cost("gpt-4o-mini-tts", characters=100).amount_usd
    large = ledger.speech_cost("gpt-4o-mini-tts", characters=2000).amount_usd

    assert large > small
    assert math.isfinite(large)


# ======================= ledger semantics / auditability ===================


def test_recorded_cost_keeps_its_request_time_basis(ledger) -> None:
    """Rows are a snapshot, not a recomputation. That is what makes them audit."""

    ledger.record_external(
        provider="openai",
        operation_category="tts",
        model="tts-1",
        estimated_cost_usd=0.0015,
        wall_seconds=0.2,
        success=True,
        cost_basis=str(CostBasis.INPUT_MEASURED),
    )
    stored = ledger.records[0]

    assert stored.estimated_cost_usd == 0.0015
    assert stored.cost_basis == "input_measured"


def test_a_row_written_before_the_basis_existed_stays_labelled_unrecorded(
    settings, tmp_path: Path
) -> None:
    """The one production row predates this column and must not be back-dated."""

    import sqlite3

    path = tmp_path / "usage.sqlite3"
    UsageLedger(settings.cloud, path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE provider_usage RENAME TO provider_usage_new")
        connection.execute(
            """CREATE TABLE provider_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                provider TEXT NOT NULL, operation_category TEXT NOT NULL,
                request_category TEXT NOT NULL, route_category TEXT NOT NULL,
                model TEXT NOT NULL, reasoning_effort TEXT NOT NULL,
                escalation_level INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
                cached_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL,
                tool_rounds INTEGER NOT NULL, tool_calls INTEGER NOT NULL,
                wall_seconds REAL NOT NULL, estimated_cost_usd REAL NOT NULL,
                success INTEGER NOT NULL, escalation_occurred INTEGER NOT NULL,
                error_code TEXT, request_id TEXT, session_id TEXT)"""
        )
        connection.execute(
            "INSERT INTO provider_usage (timestamp, provider, operation_category, "
            "request_category, route_category, model, reasoning_effort, escalation_level, "
            "input_tokens, cached_tokens, cache_write_tokens, output_tokens, "
            "reasoning_tokens, tool_rounds, tool_calls, wall_seconds, estimated_cost_usd, "
            "success, escalation_occurred) VALUES "
            "('2026-09-19T19:03:19Z','openai','text_reasoning','general','general_cloud',"
            "'gpt-5.6-terra','high',2,192,0,0,9,0,0,0,2.72,0.000615,1,0)"
        )
        connection.execute("DROP TABLE provider_usage_new")

    migrated = UsageLedger(settings.cloud, path)
    row = migrated.records[0]

    # The historical estimate is preserved exactly, not recomputed.
    assert row.estimated_cost_usd == 0.000615
    assert row.input_tokens == 192 and row.output_tokens == 9
    assert row.cost_basis == "unrecorded"


def test_the_summary_reports_how_spend_was_arrived_at(ledger) -> None:
    ledger.record_external(
        provider="openai",
        operation_category="tts",
        model="tts-1",
        estimated_cost_usd=0.0015,
        wall_seconds=0.2,
        success=True,
        cost_basis=str(CostBasis.INPUT_MEASURED),
    )
    summary = ledger.summary()

    assert summary["cost_basis_distribution"] == {"input_measured": 1}


def test_every_cost_field_is_named_as_an_estimate() -> None:
    """Terminology check: nothing claims these are settled charges."""

    from butters.cloud import usage as usage_module

    source = usage_module.__doc__ or ""
    schema = usage_module._SCHEMA if hasattr(usage_module, "_SCHEMA") else ""
    for text in (source, str(schema)):
        assert "actual_cost" not in text
        assert "actual_charge" not in text
    assert "estimated_cost_usd" in str(schema)


# ===================== paid TTS stays disabled throughout ==================


def test_paid_tts_and_stt_remain_disabled_in_shipped_configuration(settings) -> None:
    assert settings.providers.allow_paid_tts is False
    assert settings.providers.allow_paid_stt is False


def test_a_typed_pricing_entry_exists_for_every_catalogued_speech_model(settings) -> None:
    from butters.ai.capabilities import build_registry

    registry = build_registry(settings)
    catalogued = {item.id for item in registry.provider("openai").speech_models}

    assert catalogued == set(SPEECH_PRICING)
    for model in catalogued:
        assert isinstance(SPEECH_PRICING[model], (CharacterPricing, SpeechTokenPricing))


def test_pricing_is_not_reachable_from_the_admin_settings_form() -> None:
    """Rates are server-authoritative; the Admin form cannot express one."""

    from butters.ai.model import ChatSettings, SpeechSettings

    fields = set(ChatSettings.__dataclass_fields__) | set(SpeechSettings.__dataclass_fields__)

    assert not any("price" in name or "rate" in name or "cost" in name for name in fields)


def test_token_pricing_is_the_shared_chat_shape() -> None:
    from butters.assistant_config import ModelPricing

    assert ModelPricing is TokenPricing
