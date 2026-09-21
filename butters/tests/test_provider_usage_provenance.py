"""Every usage row says where its numbers came from.

Two provenance gaps, both found by reading production rather than code.

Text-reasoning rows landed with `cost_basis='unrecorded'` even when OpenAI
had returned complete token usage, because `UsageLedger.record()` never
accepted a basis and fell through to the dataclass default. And speech rows
never persisted the character count they were priced from, so the 2026-09-21
calibration had to recover it by inverting `estimated_cost_usd` through the
reservation factor — an inversion that stops working the moment that factor
moves.

Neither fix touches a historical row. `unrecorded` on an old row is the
truth about that row: it was written without an explicit basis.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from butters.assistant_config import load_assistant_settings
from butters.cloud.model import CloudTokenUsage, ReasoningConfiguration
from butters.cloud.usage import CloudUsageRecord, UsageLedger
from butters.pricing import CostBasis

SETTINGS = load_assistant_settings().cloud
ADAPTIVE_MODELS = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")


def ledger(tmp_path: Path) -> UsageLedger:
    return UsageLedger(SETTINGS, tmp_path / "usage.sqlite3")


def rows(path: Path, columns: str = "*") -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT {columns} FROM provider_usage").fetchall()


# Complete usage, exactly as the Responses parser builds it.
REPORTED = CloudTokenUsage(
    input_tokens=386, cached_tokens=0, cache_write_tokens=0,
    output_tokens=69, reasoning_tokens=34,
)


# ============================ 1. cost basis ================================


@pytest.mark.parametrize("model", ADAPTIVE_MODELS)
def test_a_successful_reasoning_call_records_provider_reported(tmp_path, model) -> None:
    """Identical for every adaptive tier: the basis follows the evidence."""

    led = ledger(tmp_path)
    record = led.record(
        "general",
        ReasoningConfiguration(1, model, "medium"),
        REPORTED,
        tool_rounds=0,
        wall_seconds=1.0,
        success=True,
        escalation_occurred=False,
        cost_basis=str(CostBasis.PROVIDER_REPORTED),
    )
    assert record.cost_basis == "provider_reported"
    assert rows(led.database_path, "cost_basis") == [("provider_reported",)]


@pytest.mark.parametrize("model", ADAPTIVE_MODELS)
def test_the_recorded_tokens_are_exactly_the_provider_values(tmp_path, model) -> None:
    led = ledger(tmp_path)
    record = led.record(
        "general", ReasoningConfiguration(1, model, "high"), REPORTED,
        tool_rounds=0, wall_seconds=1.0, success=True, escalation_occurred=False,
        cost_basis=str(CostBasis.PROVIDER_REPORTED),
    )
    assert (record.input_tokens, record.output_tokens, record.reasoning_tokens) == (386, 69, 34)
    assert record.cached_tokens == 0 and record.cache_write_tokens == 0


@pytest.mark.parametrize("model", ADAPTIVE_MODELS)
def test_the_cost_arithmetic_is_unchanged_by_the_basis(tmp_path, model) -> None:
    """A label is a label. It must not move a number."""

    led = ledger(tmp_path)
    configuration = ReasoningConfiguration(1, model, "medium")
    expected = led.estimated_cost(model, REPORTED)
    for basis in (str(CostBasis.PROVIDER_REPORTED), str(CostBasis.UNRECORDED)):
        record = led.record(
            "general", configuration, REPORTED, tool_rounds=0, wall_seconds=1.0,
            success=True, escalation_occurred=False, cost_basis=basis,
        )
        assert record.estimated_cost_usd == expected


def test_a_caller_that_supplies_no_basis_still_gets_unrecorded(tmp_path) -> None:
    """The default protects legacy and careless callers from claiming one."""

    led = ledger(tmp_path)
    record = led.record(
        "general", ReasoningConfiguration(1, "gpt-5.6-terra", "medium"), REPORTED,
        tool_rounds=0, wall_seconds=1.0, success=True, escalation_occurred=False,
    )
    assert record.cost_basis == "unrecorded"
    assert CloudUsageRecord.__dataclass_fields__["cost_basis"].default == "unrecorded"


def test_a_failed_call_is_never_labelled_provider_reported(tmp_path) -> None:
    """Nothing was reported, so nothing may claim it was.

    What is recorded is the conservative preflight reservation, which is an
    upper bound by construction.
    """

    led = ledger(tmp_path)
    record = led.record(
        "general", ReasoningConfiguration(1, "gpt-5.6-sol", "xhigh"), CloudTokenUsage(),
        tool_rounds=0, wall_seconds=0.0, success=False, escalation_occurred=False,
        error_code="upstream_error", estimated_cost_override=0.05,
        cost_basis=str(CostBasis.ESTIMATED_UPPER_BOUND),
    )
    assert record.cost_basis == "estimated_upper_bound"
    assert record.cost_basis != "provider_reported"
    # The zeros are not presented as a measurement.
    assert (record.input_tokens, record.output_tokens) == (0, 0)
    assert record.estimated_cost_usd == 0.05


def test_the_basis_survives_a_reload(tmp_path) -> None:
    led = ledger(tmp_path)
    led.record(
        "general", ReasoningConfiguration(1, "gpt-5.6-terra", "medium"), REPORTED,
        tool_rounds=0, wall_seconds=1.0, success=True, escalation_occurred=False,
        cost_basis=str(CostBasis.PROVIDER_REPORTED),
    )
    reopened = UsageLedger(SETTINGS, tmp_path / "usage.sqlite3")
    assert reopened.summary()["cost_basis_distribution"] == {"provider_reported": 1}


def test_every_production_call_site_states_its_provenance() -> None:
    """No reasoning row may reach the ledger without an explicit basis."""

    source = Path(__file__).parents[1] / "src/butters"
    for name in ("web/service.py", "cloud/orchestrator.py"):
        body = (source / name).read_text()
        calls = body.count("self.ledger.record(")
        stated = body.count("cost_basis=str(CostBasis.")
        assert stated >= calls, (name, calls, stated)


# ========================= 2. input characters =============================


def test_a_fresh_database_has_the_column(tmp_path) -> None:
    led = ledger(tmp_path)
    with sqlite3.connect(led.database_path) as connection:
        columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(provider_usage)")}
    assert columns["input_characters"] == "INTEGER"


def test_the_migration_is_additive_and_idempotent(tmp_path) -> None:
    """An existing database keeps every row; old rows become NULL."""

    path = tmp_path / "usage.sqlite3"
    # A database written before the column existed.
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """CREATE TABLE provider_usage (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   timestamp TEXT NOT NULL, provider TEXT NOT NULL,
                   operation_category TEXT NOT NULL, request_category TEXT NOT NULL,
                   route_category TEXT NOT NULL, model TEXT NOT NULL,
                   reasoning_effort TEXT NOT NULL, escalation_level INTEGER NOT NULL,
                   input_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL,
                   cache_write_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                   reasoning_tokens INTEGER NOT NULL, tool_rounds INTEGER NOT NULL,
                   tool_calls INTEGER NOT NULL, wall_seconds REAL NOT NULL,
                   estimated_cost_usd REAL NOT NULL, cost_basis TEXT NOT NULL DEFAULT 'unrecorded',
                   success INTEGER NOT NULL, escalation_occurred INTEGER NOT NULL,
                   error_code TEXT, request_id TEXT, session_id TEXT);
               CREATE TABLE request_usage (
                   id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                   request_id TEXT NOT NULL, session_id TEXT NOT NULL, source TEXT NOT NULL,
                   route_category TEXT NOT NULL, model TEXT, provider TEXT,
                   model_avoided INTEGER NOT NULL, wall_seconds REAL NOT NULL,
                   success INTEGER NOT NULL, error_code TEXT);
               CREATE TABLE spend_totals (day TEXT PRIMARY KEY, estimated_cost_usd REAL NOT NULL);
               INSERT INTO provider_usage
                 (timestamp,provider,operation_category,request_category,route_category,
                  model,reasoning_effort,escalation_level,input_tokens,cached_tokens,
                  cache_write_tokens,output_tokens,reasoning_tokens,tool_rounds,tool_calls,
                  wall_seconds,estimated_cost_usd,cost_basis,success,escalation_occurred)
                 VALUES ('2026-09-19T20:21:57Z','openai','tts','tts','tts',
                  'gpt-4o-mini-tts','not_applicable',0,0,0,0,0,0,0,0,1.84,0.001842,
                  'estimated_upper_bound',1,0);"""
        )
    before = rows(path, "timestamp, model, estimated_cost_usd, cost_basis")

    UsageLedger(SETTINGS, path)
    UsageLedger(SETTINGS, path)  # again: the migration must be idempotent

    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(provider_usage)")]
    assert columns.count("input_characters") == 1
    # Every pre-existing row survives untouched, and its character count is
    # NULL rather than a figure reconstructed from its cost.
    assert rows(path, "timestamp, model, estimated_cost_usd, cost_basis") == before
    assert rows(path, "input_characters") == [(None,)]


@pytest.mark.parametrize(
    ("model", "basis", "characters"),
    [
        ("tts-1", CostBasis.INPUT_MEASURED, 20),
        ("gpt-4o-mini-tts", CostBasis.ESTIMATED_UPPER_BOUND, 19),
    ],
)
def test_a_speech_row_persists_the_exact_character_count(
    tmp_path, model: str, basis: CostBasis, characters: int
) -> None:
    led = ledger(tmp_path)
    priced = led.speech_cost(model, characters=characters)
    record = led.record_external(
        provider="openai", operation_category="tts", model=model,
        estimated_cost_usd=priced.amount_usd, wall_seconds=1.0, success=True,
        cost_basis=str(priced.basis), input_characters=characters,
    )
    assert record.input_characters == characters
    assert record.cost_basis == str(basis)
    # The price is exactly what the pricing table produced; nothing here moved it.
    assert record.estimated_cost_usd == priced.amount_usd
    assert rows(led.database_path, "input_characters, cost_basis") == [
        (characters, str(basis))
    ]


def test_a_reasoning_row_leaves_the_character_count_null(tmp_path) -> None:
    """Characters are not that operation's billable dimension."""

    led = ledger(tmp_path)
    led.record(
        "general", ReasoningConfiguration(1, "gpt-5.6-terra", "medium"), REPORTED,
        tool_rounds=0, wall_seconds=1.0, success=True, escalation_occurred=False,
        cost_basis=str(CostBasis.PROVIDER_REPORTED),
    )
    assert rows(led.database_path, "input_characters") == [(None,)]


def test_the_reservation_factor_is_untouched() -> None:
    from butters.pricing import SPEECH_PRICING

    speech = SPEECH_PRICING["gpt-4o-mini-tts"]
    assert speech.worst_case_audio_tokens_per_character == 8.0
    assert speech.worst_case_input_tokens_per_character == 0.5


def test_a_calibration_can_now_read_characters_without_inverting_cost(tmp_path) -> None:
    """The entire point of the column.

    The 2026-09-21 exercise solved `characters = cost / rate`. That inversion
    silently breaks when the factor or the price changes; this query does not.
    """

    led = ledger(tmp_path)
    for characters in (19, 53, 120):
        priced = led.speech_cost("gpt-4o-mini-tts", characters=characters)
        led.record_external(
            provider="openai", operation_category="tts", model="gpt-4o-mini-tts",
            estimated_cost_usd=priced.amount_usd, wall_seconds=1.0, success=True,
            cost_basis=str(priced.basis), input_characters=characters,
        )
    with sqlite3.connect(led.database_path) as connection:
        found = connection.execute(
            """SELECT model, operation_category, input_characters, cost_basis
               FROM provider_usage WHERE input_characters IS NOT NULL
               ORDER BY input_characters"""
        ).fetchall()
    assert [row[2] for row in found] == [19, 53, 120]
    assert {row[3] for row in found} == {"estimated_upper_bound"}


# ================================ privacy ==================================


def test_the_ledger_stores_counts_and_never_content(tmp_path) -> None:
    led = ledger(tmp_path)
    secret = "please read my private diary entry aloud"
    priced = led.speech_cost("gpt-4o-mini-tts", characters=len(secret))
    led.record_external(
        provider="openai", operation_category="tts", model="gpt-4o-mini-tts",
        estimated_cost_usd=priced.amount_usd, wall_seconds=1.0, success=True,
        cost_basis=str(priced.basis), input_characters=len(secret),
    )
    body = Path(led.database_path).read_bytes()
    assert secret.encode() not in body
    for fragment in (b"private diary", b"read my", b"aloud"):
        assert fragment not in body
    # The count is there; the text is not.
    assert rows(led.database_path, "input_characters") == [(len(secret),)]


def test_no_content_column_exists(tmp_path) -> None:
    led = ledger(tmp_path)
    with sqlite3.connect(led.database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_usage)")}
    for forbidden in ("text", "prompt", "response", "transcript", "audio",
                      "content", "authorization", "api_key"):
        assert forbidden not in columns, forbidden


def test_the_speech_path_passes_the_same_metric_it_prices() -> None:
    """`len(text)` is priced, so `len(text)` is what is recorded."""

    body = (Path(__file__).parents[1] / "src/butters/web/service.py").read_text()
    block = body[body.index("preflight = self.ledger.speech_cost("):]
    block = block[: block.index("return result")]
    assert "speech_cost(preset.model, characters=len(text))" in block
    assert block.count("input_characters=len(text)") == 2  # failure and success
    # The text itself never reaches the ledger.
    assert "text=text" not in block


# ==================== reconciliation stays separate ========================


def test_provider_reconciliation_does_not_touch_the_basis() -> None:
    accounting = (
        Path(__file__).parents[1] / "src/butters/cloud/provider_accounting.py"
    ).read_text()
    assert "cost_basis" not in accounting
    assert "input_characters" not in accounting
    assert "UPDATE provider_usage" not in accounting
