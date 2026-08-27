"""Adversarial availability tests: session admission and voice slot release."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from beta1_harness import (
    TranscribingEngine,
    admin_headers,
    build_app,
    client,
)
from butters.assistant_config import load_assistant_settings
from butters.cloud.usage import UsageLedger
from butters.web.security import RateLimiter
from butters.web.sessions import SessionError, SessionManager


SMALL_POOL: dict[str, object] = {
    "max_active_sessions": 8,
    "admin_session_reserve": 2,
    "max_sessions_per_peer": 2,
}


def _peer(identity: str) -> dict[str, str]:
    return {"tailscale-user-login": identity}


def test_anonymous_session_flood_cannot_lock_out_administrator(tmp_path: Path) -> None:
    """H-1: filling the unreserved pool must not disable the admin surface."""

    async def scenario() -> None:
        app, service, settings = build_app(tmp_path, **SMALL_POOL)
        try:
            async with client(app) as http:
                issued = 0
                for index in range(12):
                    http.cookies.clear()
                    response = await http.get("/api/session", headers=_peer(f"user{index}@example.com"))
                    if response.status_code == 200:
                        issued += 1
                    else:
                        assert response.status_code in {429, 503}
                assert issued == settings.web.max_active_sessions - settings.web.admin_session_reserve

                # The reserve still admits an authorized administrator...
                http.cookies.clear()
                admin = await http.get("/api/session", headers=admin_headers())
                assert admin.status_code == 200
                token = admin.json()["csrf_token"]

                # ...and the privileged mutation surface still works.
                toggle = await http.post(
                    "/api/admin/skills/toggle",
                    headers=admin_headers("http://testserver", token),
                    json={"name": "get_sensor_value", "enabled": True},
                )
                assert toggle.status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_one_peer_cannot_consume_the_normal_session_pool(tmp_path: Path) -> None:
    """H-1: per-peer fairness keeps a single caller from filling the pool."""

    async def scenario() -> None:
        app, _service, settings = build_app(tmp_path, **SMALL_POOL)
        try:
            async with client(app) as http:
                codes = []
                for _index in range(5):
                    http.cookies.clear()
                    response = await http.get("/api/session", headers=_peer("noisy@example.com"))
                    codes.append(response.status_code)
                assert codes.count(200) == settings.web.max_sessions_per_peer
                assert codes[-1] == 429

                # A different caller is unaffected by the noisy one.
                http.cookies.clear()
                other = await http.get("/api/session", headers=_peer("quiet@example.com"))
                assert other.status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_session_creation_rate_limit_precedes_allocation(tmp_path: Path) -> None:
    """H-1: admission is refused before a session object is ever allocated."""

    async def scenario() -> None:
        app, service, _settings = build_app(
            tmp_path,
            max_active_sessions=64,
            max_sessions_per_peer=64,
            admin_session_reserve=4,
            session_create_rate_per_minute=1.0,
            session_create_burst=2,
        )
        try:
            async with client(app) as http:
                statuses = []
                for _index in range(6):
                    http.cookies.clear()
                    response = await http.get("/api/session", headers=_peer("burst@example.com"))
                    statuses.append(response.status_code)
                assert statuses[:2] == [200, 200]
                assert statuses[2:] == [429, 429, 429, 429]
                assert len(service.sessions.summaries()) == 2
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_session_rate_limiter_key_retention_stays_bounded() -> None:
    """H-1: the admission limiter must not become an unbounded peer registry."""

    limiter = RateLimiter(rate_per_minute=12, burst=6, max_keys=64)
    for index in range(4000):
        limiter.check(f"identity:user{index}@example.com")
    assert limiter.tracked_keys <= 64


def test_idle_expiry_restores_capacity_without_evicting_active_sessions() -> None:
    """H-1: capacity returns through expiry, never by displacing a live chat."""

    now = [1000.0]
    manager = SessionManager(
        max_active=3,
        ttl_seconds=60,
        admin_reserve=0,
        max_per_peer=3,
        clock=lambda: now[0],
    )
    idle_one = manager.create(peer_key="identity:a@example.com")
    idle_two = manager.create(peer_key="identity:a@example.com")
    active = manager.create(peer_key="identity:a@example.com")

    with pytest.raises(SessionError) as full:
        manager.create(peer_key="identity:b@example.com")
    assert full.value.code == "session_capacity"

    now[0] += 45
    manager.add_message(active, "user", "still talking")
    now[0] += 30  # idle sessions cross the TTL; the active one does not

    assert manager.expire() == 2
    assert manager.get(idle_one.session_id) is None
    assert manager.get(idle_two.session_id) is None
    assert manager.get(active.session_id) is active
    assert manager.create(peer_key="identity:b@example.com") is not None


def test_administrator_reserve_is_not_consumable_by_anonymous_callers() -> None:
    """H-1: the reserve is enforced in the manager, not only at the route."""

    manager = SessionManager(max_active=4, admin_reserve=2, max_per_peer=4)
    for index in range(2):
        manager.create(peer_key=f"identity:user{index}@example.com")
    with pytest.raises(SessionError) as denied:
        manager.create(peer_key="identity:user9@example.com")
    assert denied.value.code == "session_capacity"
    assert manager.create(peer_key="identity:admin@example.com", administrator=True) is not None


def test_voice_slot_is_released_when_recognizer_close_fails(tmp_path: Path) -> None:
    """H-2: a failing teardown must not permanently retire a voice slot."""

    class ResetAndCloseFailEngine(TranscribingEngine):
        def reset(self) -> None:
            raise RuntimeError("native recognizer reset failed")

    async def scenario() -> None:
        engines: list[ResetAndCloseFailEngine] = []

        def factory() -> ResetAndCloseFailEngine:
            engine = ResetAndCloseFailEngine(fail_close=True)
            engines.append(engine)
            return engine

        app, service, settings = build_app(tmp_path, stt_engine_factory=factory)
        limit = settings.browser_audio.max_concurrent_sessions
        try:
            from beta1_harness import WebSocketHarness, peer_identity_headers

            for attempt in range(limit + 2):
                session = service.sessions.create(peer_key=f"identity:voice{attempt}@example.com")
                socket = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers={
                        "origin": "http://testserver",
                        "cookie": f"butters_session={session.session_id}",
                        **peer_identity_headers(session.peer_key),
                    },
                )
                await socket.connect()
                await socket.send_json(
                    {
                        "type": "start",
                        "csrf_token": session.csrf_token,
                        "sample_rate": 16000,
                        "channels": 1,
                        "encoding": "pcm_s16le",
                    }
                )
                event = await socket.receive()
                assert event["type"] == "listening", (attempt, event)
                await socket.disconnect()
                await socket.finish()
            # Every recognizer was closed even though every close raised.
            assert len(engines) == limit + 2
            assert all(engine.closed for engine in engines)
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_voice_slot_is_released_when_the_worker_queue_rejects_teardown(tmp_path: Path) -> None:
    """H-2: teardown must not depend on the capacity gate it is trying to free.

    A saturated worker pool used to make the teardown call itself raise
    ``queue_full`` from inside ``finally``, skipping every release below it.
    Reproduce that exact exception directly so the guarantee is deterministic.
    """

    from butters.web.security import SecurityError

    class QueueFullEngine(TranscribingEngine):
        def reset(self) -> None:
            raise SecurityError("queue_full", "assistant worker queue is full", 503)

        def close(self) -> None:
            self.closed = True
            raise SecurityError("queue_full", "assistant worker queue is full", 503)

    async def scenario() -> None:
        engines: list[QueueFullEngine] = []

        def factory() -> QueueFullEngine:
            engine = QueueFullEngine()
            engines.append(engine)
            return engine

        app, service, settings = build_app(tmp_path, stt_engine_factory=factory)
        limit = settings.browser_audio.max_concurrent_sessions
        try:
            from beta1_harness import WebSocketHarness, peer_identity_headers

            for attempt in range(limit + 2):
                session = service.sessions.create(peer_key=f"identity:queue{attempt}@example.com")
                socket = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers={
                        "origin": "http://testserver",
                        "cookie": f"butters_session={session.session_id}",
                        **peer_identity_headers(session.peer_key),
                    },
                )
                await socket.connect()
                await socket.send_json(
                    {
                        "type": "start",
                        "csrf_token": session.csrf_token,
                        "sample_rate": 16000,
                        "channels": 1,
                        "encoding": "pcm_s16le",
                    }
                )
                event = await socket.receive()
                assert event["type"] == "listening", (attempt, event)
                await socket.disconnect()
                await socket.finish()
            assert len(engines) == limit + 2
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_admin_usage_report_is_bounded_at_retention_scale(tmp_path: Path, monkeypatch) -> None:
    """M-3: the usage report must not depend on loading the whole ledger."""

    settings = load_assistant_settings().cloud
    path = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(settings, path)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = [
        (
            stamp, "openai", "text_reasoning", "general", "general_cloud",
            settings.terra_model, "high", 1, 100, 0, 0, 50, 0, 0, 0, 0.5, 0.0001, 1, 0, None,
            "request-id", "session-id",
        )
    ] * 20_000
    request_rows = [
        (stamp, "request-id", "session-id", "text", "deterministic", None, None, 1, 0.01, 1, None)
    ] * 20_000
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO provider_usage (timestamp,provider,operation_category,request_category,"
            "route_category,model,reasoning_effort,escalation_level,input_tokens,cached_tokens,"
            "cache_write_tokens,output_tokens,reasoning_tokens,tool_rounds,tool_calls,wall_seconds,"
            "estimated_cost_usd,success,escalation_occurred,error_code,request_id,session_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.executemany(
            "INSERT INTO request_usage (timestamp,request_id,session_id,source,route_category,"
            "model,provider,model_avoided,wall_seconds,success,error_code) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            request_rows,
        )

    # Full-table materialization is fatal here, proving the report never uses it.
    def explode(*_args: object, **_kwargs: object):
        raise AssertionError("the usage report must not materialize the full ledger")

    monkeypatch.setattr(UsageLedger, "records", property(explode))
    monkeypatch.setattr(UsageLedger, "request_records", explode)

    started = time.perf_counter()
    summary = ledger.summary()
    recent = ledger.recent(100)
    recent_requests = ledger.recent_requests(100)
    elapsed = time.perf_counter() - started

    assert summary["today"]["requests"] == 20_000
    assert summary["today"]["model_avoided"] == 20_000
    assert summary["route_distribution"] == {"deterministic": 20_000}
    assert len(recent) == 100 and len(recent_requests) == 100
    assert elapsed < 2.0, f"bounded usage report took {elapsed:.2f}s"


def test_usage_summary_matches_between_memory_and_sqlite_backends(tmp_path: Path) -> None:
    """M-3: the SQL rewrite must not change reported accounting semantics."""

    from butters.cloud.model import CloudTokenUsage, EscalationLevel, ReasoningConfiguration

    settings = load_assistant_settings().cloud
    memory = UsageLedger(settings, None)
    disk = UsageLedger(settings, tmp_path / "usage.sqlite3")
    configuration = ReasoningConfiguration(EscalationLevel.ANALYSIS, settings.terra_model, "high")
    for ledger in (memory, disk):
        ledger.record(
            "general",
            configuration,
            CloudTokenUsage(input_tokens=1000, output_tokens=100),
            tool_rounds=1,
            wall_seconds=1.5,
            success=True,
            escalation_occurred=False,
            route_category="general_cloud",
            request_id="request-safe-id",
            session_id="session-safe-id",
        )
        ledger.record_request(
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

    left = memory.summary()
    right = disk.summary()
    for key in ("today", "last_7_days", "current_month", "route_distribution",
                "model_distribution", "provider_distribution",
                "deterministic_or_model_avoided"):
        assert left[key] == right[key], key


def test_clearing_a_conversation_never_blocks_the_event_loop(tmp_path: Path) -> None:
    """Clearing waits on the per-session turn lock, so it must not run inline.

    A conversation clear issued while that session already has a turn in flight
    must not stall unrelated requests: the whole service shares one event loop.
    """

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)
        holding = threading.Event()
        finish_turn = threading.Event()

        async with client(app) as http:
            started = await http.get("/api/session")
            token = started.json()["csrf_token"]
            session = service.sessions.require(
                started.cookies.get("butters_session")
            )

            def hold_one_turn() -> None:
                with session.turn_lock:
                    holding.set()
                    finish_turn.wait(10.0)

            worker = threading.Thread(target=hold_one_turn, daemon=True)
            worker.start()
            try:
                assert holding.wait(5.0)
                clearing = asyncio.create_task(
                    http.delete(
                        "/api/session/conversation",
                        headers={
                            "origin": "http://testserver",
                            "x-butters-csrf": token,
                        },
                    )
                )
                await asyncio.sleep(0.05)
                assert not clearing.done()
                health = await asyncio.wait_for(http.get("/healthz"), timeout=2.0)
                assert health.status_code == 200

                finish_turn.set()
                cleared = await asyncio.wait_for(clearing, timeout=5.0)
                assert cleared.status_code == 200
            finally:
                finish_turn.set()
                worker.join(5.0)
                await app.state.shutdown_workers()

    asyncio.run(scenario())
