from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from butters.assistant_config import load_assistant_settings
from butters.cloud.model import CloudTokenUsage, EscalationLevel, ReasoningConfiguration
from butters.cloud.usage import UsageLedger


def test_persistent_usage_survives_reinstantiation_and_contains_no_content(tmp_path: Path) -> None:
    settings = load_assistant_settings().cloud
    path = tmp_path / "usage.sqlite3"
    first = UsageLedger(settings, path)
    configuration = ReasoningConfiguration(EscalationLevel.ANALYSIS, settings.terra_model, "high")
    first.record(
        "general",
        configuration,
        CloudTokenUsage(input_tokens=1000, cached_tokens=100, output_tokens=100, reasoning_tokens=20),
        tool_rounds=1,
        tool_calls=1,
        wall_seconds=1.5,
        success=True,
        escalation_occurred=False,
        route_category="general_cloud",
        request_id="request-safe-id",
        session_id="session-safe-id",
    )
    first.record_request(
        request_id="request-safe-id",
        session_id="session-safe-id",
        source="text",
        route_category="deterministic",
        model=None,
        provider=None,
        model_avoided=True,
        wall_seconds=0.01,
        success=True,
    )

    second = UsageLedger(settings, path)

    assert len(second.records) == 1
    assert second.records[0].input_tokens == 1000
    assert second.summary()["today"]["requests"] == 1
    assert second.summary()["today"]["model_avoided"] == 1
    assert second.summary()["route_distribution"] == {"deterministic": 1}
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(provider_usage)").fetchall()
        }
    assert not columns & {"prompt", "transcript", "response", "audio", "evidence"}
    with sqlite3.connect(path) as connection:
        request_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(request_usage)").fetchall()
        }
    assert not request_columns & {"prompt", "transcript", "response", "audio", "evidence"}
    raw = path.read_bytes()
    assert b"secret prompt" not in raw


def test_persistent_budget_uses_existing_spend_and_unknown_pricing_fails_closed(tmp_path: Path) -> None:
    base = load_assistant_settings().cloud
    settings = replace(base, daily_budget_usd=0.01, monthly_budget_usd=0.01)
    path = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(settings, path)
    configuration = ReasoningConfiguration(EscalationLevel.ANALYSIS, settings.sol_model, "high")
    ledger.record(
        "general",
        configuration,
        CloudTokenUsage(input_tokens=100_000, output_tokens=100_000),
        tool_rounds=0,
        wall_seconds=1,
        success=True,
        escalation_occurred=False,
    )

    restarted = UsageLedger(settings, path)

    assert not restarted.permits(0.001)
    assert not restarted.permits(restarted.conservative_request_estimate("unknown-model", 100, 100))


def test_record_retention_is_bounded_without_erasing_budget_totals(tmp_path: Path) -> None:
    base = load_assistant_settings().cloud
    settings = replace(base, max_usage_records=2, daily_budget_usd=100.0, monthly_budget_usd=100.0)
    path = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(settings, path)
    configuration = ReasoningConfiguration(EscalationLevel.ANALYSIS, settings.terra_model, "high")
    usage = CloudTokenUsage(input_tokens=1000, output_tokens=1000)
    expected = ledger.estimated_cost(settings.terra_model, usage)

    for _index in range(3):
        ledger.record(
            "general",
            configuration,
            usage,
            tool_rounds=0,
            wall_seconds=0.1,
            success=True,
            escalation_occurred=False,
        )

    restarted = UsageLedger(settings, path)

    assert len(restarted.records) == 2
    daily, monthly = restarted._cost_totals(  # noqa: SLF001 - budget invariant
        restarted.records[-1].timestamp[:10],
        restarted.records[-1].timestamp[:7],
    )
    assert daily == pytest.approx(expected * 3)
    assert monthly == pytest.approx(expected * 3)


def test_nested_diagnostic_provider_usage_inherits_request_context(tmp_path: Path) -> None:
    settings = load_assistant_settings().cloud
    ledger = UsageLedger(settings, tmp_path / "usage.sqlite3")
    configuration = ReasoningConfiguration(EscalationLevel.ANALYSIS, settings.terra_model, "high")

    with ledger.request_context(
        request_id="request-context-id",
        session_id="session-context-id",
        route_category="diagnostic_cloud",
    ):
        ledger.record(
            "grafana",
            configuration,
            CloudTokenUsage(input_tokens=10, output_tokens=5),
            tool_rounds=0,
            wall_seconds=0.1,
            success=True,
            escalation_occurred=False,
        )

    record = ledger.records[0]
    assert record.request_id == "request-context-id"
    assert record.session_id == "session-context-id"
    assert record.route_category == "diagnostic_cloud"


def test_uncertain_provider_failure_charges_conservative_estimate(tmp_path: Path) -> None:
    settings = load_assistant_settings().cloud
    ledger = UsageLedger(settings, tmp_path / "usage.sqlite3")
    configuration = ReasoningConfiguration(EscalationLevel.ANALYSIS, settings.terra_model, "high")

    record = ledger.record(
        "general",
        configuration,
        CloudTokenUsage(),
        tool_rounds=0,
        wall_seconds=1.0,
        success=False,
        escalation_occurred=False,
        error_code="timeout",
        estimated_cost_override=0.125,
    )

    assert record.estimated_cost_usd == pytest.approx(0.125)
    assert ledger.summary()["today"]["cost_usd"] == pytest.approx(0.125)
