"""Parity and policy regressions for the model-visible tool catalog.

The SkillRegistry is authoritative for what Butters can do. These tests pin
the separate, narrower policy that decides what a model may see, so the two
cannot drift apart silently again.
"""

from __future__ import annotations

import pytest

from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.llm.catalog import (
    DOCUMENTED_EXCLUSIONS,
    MAX_MODEL_TOOLS,
    MODEL_SAFE_ACTION_CLASSES,
    derive_safe_tool_catalog,
    model_eligible,
    model_visibility_report,
)
from butters.skills.model import ActionClass, AuthenticationLevel, SkillAudience
from butters.stt.normalization import DomainVocabulary


@pytest.fixture(scope="module")
def assembled():
    assistant = create_assistant(load_assistant_settings(), DomainVocabulary((), ()))
    return assistant, assistant.router.entities, assistant.router.metrics


@pytest.fixture(scope="module")
def catalog(assembled):
    assistant, entities, metrics = assembled
    return derive_safe_tool_catalog(assistant.skills, entities, metrics)


def _names(catalog) -> set[str]:
    return {tool.name for tool in catalog}


# --- parity ---------------------------------------------------------------


def test_every_registered_skill_is_exposed_or_documented(assembled) -> None:
    """A new safe skill may not silently disappear from model visibility."""

    assistant, entities, metrics = assembled
    report = model_visibility_report(assistant.skills, entities, metrics)

    undeclared = sorted(
        name for name, item in report.items() if item["reason"] == "undeclared"
    )
    assert undeclared == [], (
        "these registered skills are neither model-visible nor documented as "
        f"excluded: {undeclared}"
    )


def test_the_report_accounts_for_every_registered_skill(assembled) -> None:
    assistant, entities, metrics = assembled
    report = model_visibility_report(assistant.skills, entities, metrics)

    assert set(report) == {spec.name for spec in assistant.skills.skills}


def test_documented_exclusions_name_real_skills(assembled) -> None:
    """A stale exclusion would hide a policy gap behind a dead entry."""

    assistant, _entities, _metrics = assembled
    registered = {spec.name for spec in assistant.skills.skills}

    assert set(DOCUMENTED_EXCLUSIONS) <= registered


def test_every_documented_exclusion_states_a_reason() -> None:
    for name, reason in DOCUMENTED_EXCLUSIONS.items():
        assert reason.strip(), f"{name} is excluded without a reason"


# --- what must be present -------------------------------------------------


def test_the_same_entity_multi_metric_read_is_model_visible(catalog) -> None:
    """get_sensor_values keeps a multi-metric request to one efficient call."""

    assert "get_sensor_values" in _names(catalog)


def test_a_representative_read_only_capability_is_model_visible(catalog) -> None:
    assert "get_sensor_value" in _names(catalog)
    assert "get_room_air_quality" in _names(catalog)


def test_a_representative_analytical_capability_is_model_visible(catalog) -> None:
    assert "correlate_metrics" in _names(catalog)
    assert "summarize_sensor_window" in _names(catalog)


def test_the_multi_metric_schema_accepts_several_metrics_at_once(catalog) -> None:
    tool = next(item for item in catalog if item.name == "get_sensor_values")
    metrics = tool.parameters["properties"]["metrics"]

    assert metrics["type"] == "array"
    assert metrics["minItems"] == 1
    assert "temperature" in metrics["items"]["enum"]
    assert "humidity" in metrics["items"]["enum"]


# --- what must never be present -------------------------------------------


def test_no_action_skill_is_ever_model_visible(assembled, catalog) -> None:
    assistant, _entities, _metrics = assembled
    actions = {
        spec.name
        for spec in assistant.skills.skills
        if spec.action_class is ActionClass.ACTION
    }

    assert actions, "the registry must contain an action for this test to mean anything"
    assert actions.isdisjoint(_names(catalog))


def test_no_administrator_skill_is_ever_model_visible(assembled, catalog) -> None:
    assistant, _entities, _metrics = assembled
    administrator = {
        spec.name
        for spec in assistant.skills.skills
        if spec.audience is SkillAudience.ADMINISTRATOR
    }

    assert administrator
    assert administrator.isdisjoint(_names(catalog))


def test_no_authenticated_skill_is_ever_model_visible(assembled, catalog) -> None:
    assistant, _entities, _metrics = assembled
    authenticated = {
        spec.name
        for spec in assistant.skills.skills
        if spec.authentication is not AuthenticationLevel.NONE
    }

    assert authenticated
    assert authenticated.isdisjoint(_names(catalog))


def test_every_exposed_tool_is_non_mutating(assembled, catalog) -> None:
    assistant, _entities, _metrics = assembled
    exposed = _names(catalog) - {"clarify_request", "unsupported_request"}

    for name in exposed:
        spec = assistant.skills.get(name)
        assert spec is not None, f"{name} is not registered"
        assert spec.action_class in MODEL_SAFE_ACTION_CLASSES
        assert spec.side_effects == "none"


def test_no_arbitrary_execution_surface_is_exposed(catalog) -> None:
    forbidden = ("shell", "command", "exec", "service_call", "entity_id", "broker")

    for name in _names(catalog):
        assert not any(token in name for token in forbidden), name


def test_the_catalog_stays_bounded(catalog) -> None:
    assert len(catalog) <= MAX_MODEL_TOOLS + 2


def test_every_tool_declares_a_strict_closed_schema(catalog) -> None:
    for tool in catalog:
        assert tool.parameters["type"] == "object"
        assert tool.parameters["additionalProperties"] is False


def test_the_control_outcomes_are_not_executable(catalog) -> None:
    control = {tool.name: tool for tool in catalog if not tool.executable}

    assert set(control) == {"clarify_request", "unsupported_request"}


# --- the policy predicate itself ------------------------------------------


def test_the_policy_rejects_a_mutating_class_regardless_of_other_metadata(
    assembled,
) -> None:
    """Eligibility is categorical: no later edit can promote an ACTION."""

    assistant, _entities, _metrics = assembled
    action = next(
        spec
        for spec in assistant.skills.skills
        if spec.action_class is ActionClass.ACTION
    )
    eligible, reason = model_eligible(action)

    assert eligible is False
    assert reason in {
        "mutating_action_class",
        "requires_authentication",
        "declares_side_effects",
        "requires_explicit_user_intent",
    }


def test_the_policy_rejects_an_administrator_observation(assembled) -> None:
    assistant, _entities, _metrics = assembled
    admin = next(
        spec
        for spec in assistant.skills.skills
        if spec.audience is SkillAudience.ADMINISTRATOR
    )

    assert model_eligible(admin) == (False, "administrator_audience")
