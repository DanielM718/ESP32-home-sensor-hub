from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from beta1_harness import WebSocketHarness, build_app, client
from butters.web.stt_pool import STTEnginePool, STTEnginePoolError


class TrackingEngine:
    initialization_seconds = 0.25

    def __init__(self, *, fail_finalize: bool = False) -> None:
        self.fail_finalize = fail_finalize
        self.starts = 0
        self.resets = 0
        self.closes = 0

    def start_utterance(self) -> None:
        self.starts += 1

    def accept_audio(self, _frame):
        return "what is the humidity in box three"

    def get_partial_transcript(self) -> str:
        return "what is the humidity in box three"

    def endpoint_detected(self) -> bool:
        return False

    def finalize(self) -> str:
        if self.fail_finalize:
            raise RuntimeError("recognizer fixture failed")
        return "what is the humidity in box three"

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.closes += 1


def test_pool_prewarms_once_and_reuses_the_same_stateful_engine() -> None:
    engines: list[TrackingEngine] = []

    def factory() -> TrackingEngine:
        engine = TrackingEngine()
        engines.append(engine)
        return engine

    pool = STTEnginePool(factory, max_size=1, acquire_timeout_seconds=0.1)
    cold = pool.warm()
    warm = pool.acquire()

    assert not cold.reused
    assert warm.reused and warm.engine is cold.engine
    assert len(engines) == 1
    assert pool.stats()["cold_loads"] == 1
    assert pool.stats()["reuses"] == 1

    pool.release(warm.engine)
    pool.close()
    assert engines[0].closes == 1


def test_pool_never_shares_one_engine_concurrently_and_wait_is_bounded() -> None:
    pool = STTEnginePool(
        TrackingEngine,
        max_size=1,
        acquire_timeout_seconds=0.01,
    )
    lease = pool.acquire()

    with pytest.raises(STTEnginePoolError, match="timed out"):
        pool.acquire()

    pool.release(lease.engine)
    pool.close()


def test_unhealthy_engine_is_discarded_and_replaced() -> None:
    engines: list[TrackingEngine] = []

    def factory() -> TrackingEngine:
        engine = TrackingEngine()
        engines.append(engine)
        return engine

    pool = STTEnginePool(factory, max_size=1, acquire_timeout_seconds=0.1)
    first = pool.acquire()
    pool.release(first.engine, reusable=False)
    second = pool.acquire()

    assert second.engine is not first.engine
    assert first.engine.closes == 1
    assert len(engines) == 2

    pool.release(second.engine)
    pool.close()


async def _voice_turn(
    app,
    *,
    session_id: str,
    csrf_token: str,
) -> tuple[dict[str, object], ...]:
    socket = WebSocketHarness(
        app,
        "/ws/voice",
        headers={
            "origin": "http://testserver",
            "cookie": f"butters_session={session_id}",
        },
    )
    await socket.connect()
    await socket.send_json(
        {
            "type": "start",
            "csrf_token": csrf_token,
            "sample_rate": 16000,
            "channels": 1,
            "encoding": "pcm_s16le",
            "client_permission_ms": 25,
            "client_setup_ms": 40,
        }
    )
    listening = await socket.receive()
    assert listening["type"] == "listening"
    await socket.send_bytes((1000).to_bytes(2, "little", signed=True) * 640)
    await socket.send_json(
        {"type": "stop", "endpoint_reason": "tap", "client_capture_ms": 800}
    )
    received: list[dict[str, object]] = [listening]
    while True:
        event = await socket.receive()
        assert isinstance(event, dict)
        received.append(event)
        if event.get("type") in {"assistant", "error"}:
            break
    await socket.finish()
    return tuple(received)


def test_repeated_websocket_turns_reuse_model_and_persist_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        engines: list[TrackingEngine] = []

        def factory() -> TrackingEngine:
            engine = TrackingEngine()
            engines.append(engine)
            return engine

        app, service, _settings = build_app(
            tmp_path,
            stt_engine_factory=factory,
        )
        session = service.sessions.create(peer_key="identity:voice@example.com")
        try:
            first = await _voice_turn(
                app,
                session_id=session.session_id,
                csrf_token=session.csrf_token,
            )
            second = await _voice_turn(
                app,
                session_id=session.session_id,
                csrf_token=session.csrf_token,
            )

            assert len(engines) == 1
            assert app.state.stt_pool.stats()["cold_loads"] == 1
            assert app.state.stt_pool.stats()["reuses"] == 1
            for events in (first, second):
                types = [item.get("type") for item in events]
                assert "partial" in types
                assert types.index("final") < types.index("assistant")
                answer = next(
                    item for item in events if item.get("type") == "assistant"
                )
                assert "42" in str(answer["response_text"])

            voice_traces = [
                item
                for item in service.traces.recent(10, include_text=True)
                if item["source"] == "voice"
            ]
            assert len(voice_traces) == 2
            for trace in voice_traces:
                final_event = next(
                    item
                    for item in trace["events"]
                    if item["stage"] == "stt" and item["status"] == "final"
                )
                fields = final_event["fields"]
                for key in (
                    "audio_preprocessing_ms",
                    "streaming_inference_ms",
                    "finalization_latency_ms",
                    "server_stop_to_final_ms",
                    "audio_seconds",
                    "real_time_factor",
                ):
                    assert key in fields

            assert [(item.role, item.text) for item in session.messages] == [
                ("user", "what is the humidity in box three"),
                ("assistant", "Filament box three humidity is 42 percent."),
                ("user", "what is the humidity in box three"),
                ("assistant", "Filament box three humidity is 42 percent."),
            ]
            async with client(app) as http:
                restored = await http.get(
                    "/api/session",
                    headers={
                        "cookie": f"butters_session={session.session_id}",
                        "tailscale-user-login": "voice@example.com",
                    },
                )
            assert restored.status_code == 200
            assert restored.json()["messages"][-1]["text"] == (
                "Filament box three humidity is 42 percent."
            )
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_voice_and_text_turns_share_one_ordered_conversation(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, service, _settings = build_app(
            tmp_path,
            stt_engine_factory=TrackingEngine,
        )
        session = service.sessions.create(peer_key="identity:mixed@example.com")
        try:
            before = service.handle_text(
                session,
                "What is the humidity in box three?",
                source="text",
            )
            voice = await _voice_turn(
                app,
                session_id=session.session_id,
                csrf_token=session.csrf_token,
            )
            after = service.handle_text(
                session,
                "Humidity in box 3",
                source="text",
            )

            assert before.response_text and after.response_text
            assert any(item.get("type") == "assistant" for item in voice)
            assert [item.role for item in session.messages] == [
                "user",
                "assistant",
                "user",
                "assistant",
                "user",
                "assistant",
            ]
            assert [item.text for item in session.messages if item.role == "user"] == [
                "What is the humidity in box three?",
                "what is the humidity in box three",
                "Humidity in box 3",
            ]
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_stt_failure_discards_native_engine_and_next_turn_recovers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engines: list[TrackingEngine] = []

        def factory() -> TrackingEngine:
            engine = TrackingEngine(fail_finalize=not engines)
            engines.append(engine)
            return engine

        app, service, _settings = build_app(
            tmp_path,
            stt_engine_factory=factory,
        )
        first_session = service.sessions.create(peer_key="identity:failure@example.com")
        second_session = service.sessions.create(
            peer_key="identity:recovery@example.com"
        )
        try:
            failed = await _voice_turn(
                app,
                session_id=first_session.session_id,
                csrf_token=first_session.csrf_token,
            )
            recovered = await _voice_turn(
                app,
                session_id=second_session.session_id,
                csrf_token=second_session.csrf_token,
            )

            error = next(item for item in failed if item.get("type") == "error")
            assert error["code"] == "stt_error"
            assert any(item.get("type") == "assistant" for item in recovered)
            assert len(engines) == 2
            assert engines[0].closes == 1
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_app_lifespan_prewarms_local_stt_without_per_turn_model_load(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engines: list[TrackingEngine] = []

        def factory() -> TrackingEngine:
            engine = TrackingEngine()
            engines.append(engine)
            return engine

        app, _service, _settings = build_app(
            tmp_path,
            stt_engine_factory=factory,
        )
        async with app.router.lifespan_context(app):
            stats = app.state.stt_pool.stats()
            assert len(engines) == 1
            assert stats["available"] == 1
            assert stats["cold_loads"] == 1
            assert stats["last_initialization_ms"] == 250.0

        assert engines[0].closes == 1

    asyncio.run(scenario())
