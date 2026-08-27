"""Server-side ordering regressions for the browser interaction generation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from beta1_harness import build_app, client

from butters.web.sessions import SessionError


def test_an_older_clear_cannot_erase_a_newer_turn(tmp_path: Path) -> None:
    _app, service, _settings = build_app(tmp_path)
    session = service.sessions.create(peer_key="peer:test")

    service.handle_text(
        session,
        "what is the humidity in box three",
        interaction_generation=3,
    )
    before = tuple(session.messages)

    cleared = service.clear_conversation(session, interaction_generation=2)

    assert cleared is False
    assert tuple(session.messages) == before
    assert session.interaction_generation == 3


def test_a_clear_that_wins_the_lock_precedes_the_newer_turn(tmp_path: Path) -> None:
    _app, service, _settings = build_app(tmp_path)
    session = service.sessions.create(peer_key="peer:test")
    service.handle_text(session, "server status")

    assert service.clear_conversation(session, interaction_generation=2) is True
    service.handle_text(
        session,
        "what is the humidity in box three",
        interaction_generation=3,
    )

    assert [item.role for item in session.messages] == ["user", "assistant"]
    assert "humidity" in session.messages[0].text
    assert session.interaction_generation == 3


def test_a_duplicate_or_stale_turn_is_rejected_before_conversation_mutation(
    tmp_path: Path,
) -> None:
    _app, service, _settings = build_app(tmp_path)
    session = service.sessions.create(peer_key="peer:test")
    service.handle_text(
        session,
        "what is the humidity in box three",
        interaction_generation=4,
    )
    before = tuple(session.messages)

    with pytest.raises(SessionError) as failure:
        service.handle_text(
            session,
            "wake my desktop",
            interaction_generation=4,
        )

    assert failure.value.code == "stale_generation"
    assert tuple(session.messages) == before


def test_http_clear_preserves_csrf_and_reports_the_server_generation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                opened = await http.get("/api/session")
                payload = opened.json()
                csrf = payload["csrf_token"]
                assert payload["interaction_generation"] == 0

                cleared = await http.delete(
                    "/api/session/conversation",
                    headers={
                        "origin": "http://testserver",
                        "x-butters-csrf": csrf,
                        "x-butters-generation": "1",
                    },
                )
                assert cleared.status_code == 200
                assert cleared.json() == {"status": "cleared", "csrf_token": csrf}

                answered = await http.post(
                    "/api/chat",
                    headers={
                        "origin": "http://testserver",
                        "x-butters-csrf": csrf,
                        "x-butters-generation": "2",
                    },
                    json={"text": "what is the humidity in box three"},
                )
                assert answered.status_code == 200
                session = service.sessions.require(http.cookies.get("butters_session"))
                assert session.interaction_generation == 2
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_malformed_generation_fails_before_chat_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                opened = await http.get("/api/session")
                response = await http.post(
                    "/api/chat",
                    headers={
                        "origin": "http://testserver",
                        "x-butters-csrf": opened.json()["csrf_token"],
                        "x-butters-generation": "NaN",
                    },
                    json={"text": "what is the humidity in box three"},
                )
                assert response.status_code == 400
                session = service.sessions.require(http.cookies.get("butters_session"))
                assert session.messages == []
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_only_pre_execution_invalid_session_is_marked_safe_to_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        try:
            async with client(app) as missing:
                rejected = await missing.post(
                    "/api/chat",
                    headers={
                        "origin": "http://testserver",
                        "x-butters-csrf": "not-used",
                    },
                    json={"text": "server status"},
                )
                assert rejected.status_code == 401
                assert rejected.json()["error"] == "invalid_session"
                assert rejected.json()["safe_to_retry"] is True

            async with client(app) as http:
                opened = await http.get("/api/session")

                def late_invalid_session(*_args, **_kwargs):
                    raise SessionError(
                        "invalid_session", "simulated failure after dispatch"
                    )

                service.handle_text = late_invalid_session
                failed = await http.post(
                    "/api/chat",
                    headers={
                        "origin": "http://testserver",
                        "x-butters-csrf": opened.json()["csrf_token"],
                    },
                    json={"text": "server status"},
                )
                assert failed.status_code == 401
                assert failed.json()["error"] == "invalid_session"
                assert failed.json()["safe_to_retry"] is False
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())
