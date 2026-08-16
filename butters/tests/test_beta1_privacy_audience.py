"""Trace lifetime and administrator-sensitive skill authorization."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from beta1_harness import admin_headers, build_app, client
from butters.skills.model import SkillAudience
from butters.web.sessions import SessionManager
from butters.web.trace import TraceBuffer, TraceStage


ADMIN_ONLY_SKILLS = ("get_project_status", "get_network_observation")
NORMAL_SKILLS = ("get_sensor_value", "get_host_observation", "get_stack_observation")


def test_traces_expire_by_time_not_only_by_count() -> None:
    """L-3: traces quote conversation text, so they must age out."""

    now = [1000.0]
    traces = TraceBuffer(64, ttl_seconds=300.0, clock=lambda: now[0])
    first = traces.start("session-one", "text")
    first.emit(TraceStage.REQUEST, "accepted", fields={"raw_text": "private question"})

    assert traces.get(first.trace_id) is not None
    now[0] += 299
    assert traces.get(first.trace_id) is not None

    now[0] += 2
    assert traces.get(first.trace_id) is None
    assert traces.recent(10) == []


def test_expiring_a_session_drops_the_traces_that_quote_it() -> None:
    """L-3: conversation content leaves memory with its conversation."""

    now = [500.0]
    traces = TraceBuffer(64, ttl_seconds=3600.0, clock=lambda: now[0])
    sessions = SessionManager(
        max_active=4,
        ttl_seconds=60.0,
        admin_reserve=0,
        clock=lambda: now[0],
        on_expire=traces.drop_sessions,
    )
    session = sessions.create(peer_key="identity:user@example.com")
    other = sessions.create(peer_key="identity:other@example.com")
    kept = traces.start(other.session_id, "text")
    dropped = traces.start(session.session_id, "text")
    dropped.emit(TraceStage.REQUEST, "accepted", fields={"raw_text": "private question"})

    now[0] += 30
    sessions.get(other.session_id)  # the still-active conversation is refreshed
    now[0] += 31  # only the untouched session crosses its TTL
    sessions.expire()

    assert traces.get(dropped.trace_id) is None
    assert traces.get(kept.trace_id) is not None
    assert "private question" not in str(traces.recent(10))


def test_clearing_a_conversation_also_clears_its_traces(tmp_path: Path) -> None:
    """L-3: the Clear button must remove the transcript from the admin view too."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                session = await http.get("/api/session")
                token = session.json()["csrf_token"]
                chat = await http.post(
                    "/api/chat",
                    headers={"origin": "http://testserver", "x-butters-csrf": token},
                    json={"text": "what is the humidity in box three"},
                )
                assert chat.status_code == 200
                assert "humidity in box three" in str(service.traces.recent(10))

                cleared = await http.delete(
                    "/api/session/conversation",
                    headers={"origin": "http://testserver", "x-butters-csrf": token},
                )
                assert cleared.status_code == 200
                assert service.traces.recent(10) == []
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


@pytest.mark.parametrize("skill", ADMIN_ONLY_SKILLS)
def test_administrator_sensitive_skills_are_declared_in_metadata(tmp_path: Path, skill: str) -> None:
    """M-8: audience is a first-class SkillSpec property, not a route special case."""

    app, service, _settings = build_app(tmp_path)
    try:
        spec = service.assistant.skills.get(skill)
        assert spec is not None
        assert spec.audience is SkillAudience.ADMINISTRATOR
        assert service.assistant.skills.requires_administrator(skill)
    finally:
        asyncio.run(app.state.shutdown_workers())


@pytest.mark.parametrize("skill", NORMAL_SKILLS)
def test_ordinary_observations_remain_available_to_normal_callers(tmp_path: Path, skill: str) -> None:
    app, service, _settings = build_app(tmp_path)
    try:
        spec = service.assistant.skills.get(skill)
        assert spec is not None
        assert spec.audience is SkillAudience.NORMAL
        assert not service.assistant.skills.requires_administrator(skill)
    finally:
        asyncio.run(app.state.shutdown_workers())


@pytest.mark.parametrize(
    ("skill", "arguments"),
    (
        ("get_project_status", {"view": "status"}),
        ("get_project_status", {"view": "recent_commits"}),
        ("get_network_observation", {"view": "listeners"}),
        ("get_network_observation", {"view": "routes"}),
    ),
)
def test_registry_denies_administrator_skills_to_normal_callers(
    tmp_path: Path, skill: str, arguments: dict[str, str]
) -> None:
    """M-8: enforcement lives in the registry, so every caller inherits it."""

    app, service, _settings = build_app(tmp_path)
    try:
        registry = service.assistant.skills
        execution = registry.execute(skill, arguments)
        assert not execution.ok
        assert execution.failure is not None
        assert execution.failure.code == "administrator_required"
        proposal = registry.validate_proposal(skill, arguments)
        assert proposal is not None and proposal.code == "administrator_required"
        # The same call is permitted for an authorized administrator.
        assert registry.validate_proposal(skill, arguments, administrator=True) is None
    finally:
        asyncio.run(app.state.shutdown_workers())


@pytest.mark.parametrize(
    "text",
    (
        "show me git status",
        "what are the recent commits",
        "is the repo dirty",
        "what is the current branch",
        "listening ports",
        "routing table",
    ),
)
def test_normal_conversation_never_returns_repository_or_network_internals(
    tmp_path: Path, text: str
) -> None:
    """M-8: the normal page must not expose development or deployment internals."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                session = await http.get("/api/session")
                token = session.json()["csrf_token"]
                response = await http.post(
                    "/api/chat",
                    headers={"origin": "http://testserver", "x-butters-csrf": token},
                    json={"text": text},
                )
                assert response.status_code == 200
                payload = response.json()
                assert payload["route"] == "unsupported", (text, payload)
                assert payload["reason_codes"] == ["administrator_required"]
                assert payload["response_text"] == (
                    "I can't answer that request from the normal chat."
                )
                assert payload["skill"] is None
                assert payload["model"] is None
                # The sensitive adapter was never invoked, so nothing to leak.
                events = service.traces.recent(5)[0]["events"]
                stages = [(item["stage"], item["status"]) for item in events]
                assert ("tool", "complete") not in stages
                assert ("policy", "denied") in stages
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_administrator_routing_test_can_still_reach_sensitive_skills(tmp_path: Path) -> None:
    """M-8: administrators keep full access through the privileged surface."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                session = await http.get("/api/session", headers=admin_headers())
                token = session.json()["csrf_token"]
                response = await http.post(
                    "/api/admin/routing/test",
                    headers=admin_headers("http://testserver", token),
                    json={"text": "listening ports", "override": "deterministic_local"},
                )
                assert response.status_code == 200
                payload = response.json()
                assert payload["skill"] == "get_network_observation"
                assert payload["route"] == "deterministic"
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_cloud_tool_exposure_excludes_administrator_skills_for_normal_callers(
    tmp_path: Path,
) -> None:
    """M-8: a cloud model can never be handed a tool its caller could not run."""

    app, service, _settings = build_app(tmp_path)
    try:
        text = "why is the tailscale status and dashboard behaving that way"
        normal = {item["name"] for item in service._relevant_skill_tools(text, False)}
        privileged = {item["name"] for item in service._relevant_skill_tools(text, True)}
        assert "get_network_observation" not in normal
        assert "get_network_observation" in privileged
    finally:
        asyncio.run(app.state.shutdown_workers())


def test_skill_metadata_can_be_filtered_to_the_normal_audience(tmp_path: Path) -> None:
    app, service, _settings = build_app(tmp_path)
    try:
        names = {item["name"] for item in service.assistant.skills.metadata(administrator=False)}
        for skill in ADMIN_ONLY_SKILLS:
            assert skill not in names
        for skill in NORMAL_SKILLS:
            assert skill in names
    finally:
        asyncio.run(app.state.shutdown_workers())
