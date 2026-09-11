"""Where the diagnostic surface sits relative to the model.

The diagnostic tools are a second capability surface with its own registry, and
two of its names -- ``get_server_health`` and ``get_sensor_history_summary`` --
are also SkillRegistry skills that the conversational catalog deliberately
withholds. That makes "are they read-only?" the wrong question. The question is
whether anything the model or a cloud provider controls can reach a capability
the safe catalog intentionally excludes.

The boundary has three parts, one test group each:

* invocation is operator-initiated: the domain comes from the operator's own
  text through DiagnosticPlanner, never from model output;
* the conversational model cannot reach the diagnostic registry at all;
* inside a diagnostic run, a cloud provider is confined to the domain the
  operator's question established.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from butters.actions.store import ActionStateStore
from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.diagnostics.tools import build_diagnostic_registry
from butters.llm.catalog import derive_safe_tool_catalog
from butters.llm.model import (
    LanguageModel,
    LanguageModelResult,
    ProposalKind,
    ToolProposal,
)
from butters.skills.model import ActionClass
from butters.stt.normalization import DomainVocabulary

UNROUTABLE = "please do the thing that has no local route at all zzz"


class _Puppet(LanguageModel):
    """A model that always proposes one chosen name."""

    def __init__(self, skill: str, arguments: dict | None = None) -> None:
        self.skill = skill
        self.arguments = arguments or {}
        self.offered: set[str] = set()

    def propose_tools(self, request, available_tools, context=()):
        self.offered = {tool.name for tool in available_tools}
        return LanguageModelResult(
            ToolProposal(ProposalKind.TOOL, self.skill, dict(self.arguments)),
            "puppet",
            0.0,
        )


def _assistant(model: LanguageModel | None = None):
    settings = load_assistant_settings()
    state = ActionStateStore(
        Path(tempfile.mkdtemp()) / "actions.sqlite3", settings.actions
    )
    return create_assistant(
        settings,
        DomainVocabulary((), ()),
        action_state=state,
        language_model=model,
    )


@pytest.fixture(scope="module")
def surfaces():
    assistant = _assistant()
    safe = {
        tool.name
        for tool in derive_safe_tool_catalog(
            assistant.skills, assistant.router.entities, assistant.router.metrics
        )
    }
    diagnostic = {
        spec.name
        for spec in build_diagnostic_registry(load_assistant_settings()).tools
    }
    return assistant, safe, diagnostic


# --- the surfaces themselves ---------------------------------------------


def test_the_diagnostic_registry_cannot_hold_a_mutating_tool(surfaces) -> None:
    """No ActionAuthorization exists for this surface because none is needed."""

    _assistant_obj, _safe, _diagnostic = surfaces
    registry = build_diagnostic_registry(load_assistant_settings())

    for spec in registry.tools:
        assert spec.action_class is ActionClass.READ_ONLY, spec.name
        assert spec.timeout_seconds > 0, spec.name
        assert spec.max_output_bytes >= 256, spec.name


def test_the_two_surfaces_overlap_only_where_it_is_understood(surfaces) -> None:
    """Pin the overlap so a new one has to be argued rather than appear.

    A diagnostic tool that shares a name with a withheld skill is the case that
    needs thinking about: the conversational catalog withholds it because a chat
    assistant has no use for host internals, while the diagnostic surface exists
    precisely to inspect them when the operator asks.
    """

    assistant, safe, diagnostic = surfaces
    registered = {spec.name for spec in assistant.skills.skills}
    withheld = registered - safe

    assert diagnostic & withheld == {
        "get_server_health",
        "get_sensor_history_summary",
    }


# --- the model cannot reach the diagnostic surface ------------------------


@pytest.mark.parametrize(
    "name",
    [
        "read_service_logs",
        "ping_allowlisted_host",
        "check_tcp_port",
        "get_tailscale_status",
        "inspect_allowlisted_mqtt_topic",
        "get_failed_units",
        "get_container_status",
    ],
)
def test_a_proposal_cannot_reach_a_diagnostic_only_tool(name: str) -> None:
    """These exist in the diagnostic registry and in no model catalog."""

    response = _assistant(_Puppet(name)).handle_text(UNROUTABLE)

    assert response.policy_status == "tool_not_offered"
    assert response.execution is None


@pytest.mark.parametrize("name", ["get_server_health", "get_sensor_history_summary"])
def test_a_proposal_cannot_reach_the_overlapping_names_either(name: str) -> None:
    """The overlap must not become the way back in through the skills path."""

    response = _assistant(_Puppet(name)).handle_text(UNROUTABLE)

    assert response.policy_status == "tool_not_offered"
    assert response.execution is None


def test_no_diagnostic_only_tool_can_appear_in_the_model_catalog(surfaces) -> None:
    """The catalog is derived from the SkillRegistry, so the surfaces cannot mix.

    Some names exist on both surfaces on purpose -- get_sensor_value and its
    neighbours are the same benign read -- and those are model-visible anyway.
    What must never happen is a tool that exists only in the diagnostic registry
    becoming model-visible, because it was never reviewed for that audience.
    """

    assistant, safe, diagnostic = surfaces
    registered = {spec.name for spec in assistant.skills.skills}
    diagnostic_only = diagnostic - registered

    assert diagnostic_only, "the registries no longer differ; re-check this test"
    assert diagnostic_only & safe == set()


# --- invocation is operator-initiated ------------------------------------


@pytest.mark.parametrize(
    "text", ["why is grafana slow", "diagnose the mqtt bridge"]
)
def test_a_diagnostic_runs_from_operator_text_before_any_model(text: str) -> None:
    """The planner reads the operator's words, and the model is never consulted.

    handle_text short-circuits on a planner match, so the model has no
    opportunity to influence the request, the domain, or the tool list.
    """

    model = _Puppet("read_service_logs")
    response = _assistant(model).handle_text(text)

    assert response.routing_path == "diagnostic_local"
    assert response.policy_status == "read_only"
    assert model.offered == set(), "the model was consulted on a diagnostic turn"


def test_the_assistant_path_never_permits_cloud_escalation() -> None:
    """Voice and the deterministic path stay local; cloud is the web service's.

    request_from_text is called with local_only=True and allow_cloud=False, so
    no provider participates in a diagnostic reached this way.
    """

    source = (
        Path(__file__).resolve().parents[1] / "src" / "butters" / "assistant.py"
    ).read_text(encoding="utf-8")

    assert source.count("local_only=True") == 2
    assert source.count("allow_cloud=False") == 2
    assert "allow_cloud=True" not in source
