"""Composer lifecycle regressions for the browser chat surface.

Two layers are covered. The HTTP tests prove the conversation is not one-shot
on the server. The asset tests pin the client state machine that produced the
reported iPhone lockout, where the header read "Ready" while the text field
stayed disabled because a turn was still waiting on spoken playback that had
already been stopped. No browser or JavaScript runtime is available in this
repository, so the client contract is asserted against the shipped asset.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from beta1_harness import build_app, client, start_session
from butters.assistant_config import load_assistant_settings

APP_JS = (
    Path(__file__).resolve().parents[1] / "src/butters/web/static/assets/app.js"
).read_text(encoding="utf-8")

QUESTION = "what is the humidity in box three"


def _block(source: str, opening: str) -> str:
    """Return the balanced brace block that follows the first `opening` match."""

    start = source.index(opening)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[source.index("{", start) : index + 1]
    raise AssertionError(f"unbalanced block after {opening!r}")


def _headers(session: dict[str, object]) -> dict[str, str]:
    return {
        "origin": "http://testserver",
        "x-butters-csrf": str(session["csrf_token"]),
    }


def test_repeated_questions_reuse_one_session_and_token(tmp_path: Path) -> None:
    """The chat must not be one-shot: a second question needs no page reload."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                session = await start_session(http)
                headers = _headers(session)
                for _ in range(3):
                    response = await http.post(
                        "/api/chat", headers=headers, json={"text": QUESTION}
                    )
                    assert response.status_code == 200
                    assert "42" in response.json()["response_text"]
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_a_rejected_question_does_not_end_the_conversation(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                session = await start_session(http)
                headers = _headers(session)
                rejected = await http.post(
                    "/api/chat", headers=headers, json={"text": "   "}
                )
                recovered = await http.post(
                    "/api/chat", headers=headers, json={"text": QUESTION}
                )

                assert rejected.status_code >= 400
                assert recovered.status_code == 200
                assert "42" in recovered.json()["response_text"]
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_exhausting_speech_does_not_block_the_next_question(tmp_path: Path) -> None:
    """Spoken playback is scarce and optional; text must survive without it."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        try:
            async with client(app) as http:
                session = await start_session(http)
                headers = _headers(session)
                speech_statuses = []
                for _ in range(4):
                    answer = await http.post(
                        "/api/chat", headers=headers, json={"text": QUESTION}
                    )
                    assert answer.status_code == 200
                    speech = await http.post(
                        "/api/speech",
                        headers=headers,
                        json={"trace_id": answer.json()["trace_id"]},
                    )
                    speech_statuses.append(speech.status_code)

                assert 429 in speech_statuses
                final = await http.post(
                    "/api/chat", headers=headers, json={"text": QUESTION}
                )
                assert final.status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_the_text_field_is_never_disabled() -> None:
    """The lockout was a disabled input a stalled turn never re-enabled."""

    assert "input.disabled" not in APP_JS
    assert "disabled" not in _block(APP_JS, "async function sendText")


def test_a_pending_request_blocks_only_the_send_button() -> None:
    body = _block(APP_JS, "function setPending(value)")

    assert "sendButton.disabled = value" in body
    assert "input" not in body


def test_every_send_outcome_restores_the_composer() -> None:
    body = _block(APP_JS, "async function sendText")

    assert "setPending(false)" in _block(body, "} finally")
    assert body.count("setPending(false)") == 1
    assert body.index("finally") < body.index("await playResponse")


def test_a_lost_chat_response_has_a_bounded_recovery_path() -> None:
    body = _block(APP_JS, "async function sendText")
    finalizer = _block(body, "} finally")

    assert "const controller = new AbortController()" in body
    assert "controller.abort()" in body
    assert "signal: controller.signal" in body
    assert 'error.name === "AbortError"' in body
    assert "window.clearTimeout(requestTimer)" in finalizer
    assert finalizer.index("clearTimeout") < finalizer.index("setPending(false)")


def test_the_chat_timeout_cannot_fire_before_the_service_gives_up() -> None:
    """A backstop that beats the server reports a timeout for an answered turn.

    The service persists the assistant turn as soon as it finishes, so a client
    that aborts first tells the user their question timed out while the reply is
    already in the restored conversation.
    """

    cloud = load_assistant_settings().cloud
    # max_wall_seconds only gates entry to a tool round, so the final round may
    # start just under that budget and still spend one full request timeout per
    # attempt, retries included, before the service reports failure.
    worst_case_ms = (
        cloud.max_wall_seconds + cloud.timeout_seconds * (cloud.max_retries + 1)
    ) * 1000
    match = re.search(r"const CHAT_LIMIT_MS = (\d+);", APP_JS)

    assert match is not None
    assert int(match.group(1)) >= worst_case_ms


def test_playback_settles_on_every_media_outcome() -> None:
    """Pause, decode failure, blocked autoplay, and a stall all release the turn."""

    body = _block(APP_JS, "async function speak")

    for outcome in ('"ended"', '"error"', '"pause"'):
        assert f"addEventListener({outcome}, finish" in body
    assert "setTimeout(finish" in body
    assert ".catch(finish)" in body
    assert "finishPlayback(playback);" in _block(body, "} finally")


def test_stopping_speech_releases_the_waiting_turn() -> None:
    body = _block(APP_JS, "function stopPlayback()")

    assert "playback.audio.pause()" in body
    assert "finishPlayback(playback)" in body
    assert 'stopAudio.addEventListener("click", stopPlayback)' in APP_JS


def test_overlapping_playbacks_keep_independent_settlement_state() -> None:
    """An old media event must not settle, pause, or clear a newer response."""

    finish = _block(APP_JS, "function finishPlayback(playback)")
    speak = _block(APP_JS, "async function speak")

    assert "playback.settled" in finish
    assert "playback.timer" in finish
    assert "playback.settle" in finish
    assert "playbackSettle" not in APP_JS
    assert "playbackTimer" not in APP_JS
    assert "const playback =" in speak
    assert "const finish = () => finishPlayback(playback)" in speak
    assert "playback.audio.pause()" in _block(speak, "} finally")
    assert "if (currentPlayback === playback)" in _block(speak, "} finally")


def test_a_new_turn_cancels_speech_that_is_still_being_fetched() -> None:
    stop = _block(APP_JS, "function stopPlayback()")
    speak = _block(APP_JS, "async function speak")

    assert "playback.stopped = true" in stop
    assert "playback.stopped || currentPlayback !== playback" in speak
    assert speak.index("stopPlayback();") < speak.index('fetch("/api/speech"')


def test_stale_turns_cannot_start_playback_and_voice_invalidates_old_audio() -> None:
    play = _block(APP_JS, "async function playResponse")
    send = _block(APP_JS, "async function sendText")
    voice = _block(APP_JS, "async function beginVoice")

    assert play.index("if (!isCurrentTurn(turn)) return;") < play.index("await speak")
    assert send.index("cleanupVoice();") < send.index("beginTurn();")
    assert voice.index("beginTurn();") < voice.index("transitionVoice")
    assert voice.index("stopPlayback();") < voice.index("transitionVoice")


def test_stale_voice_events_cannot_overwrite_a_newer_text_or_voice_turn() -> None:
    voice = _block(APP_JS, "async function beginVoice")
    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")
    current = _block(APP_JS, "function voiceIsCurrent(turn)")
    cleanup = _block(
        APP_JS,
        "function cleanupVoice(expectedTurn = null, preserveError = false)",
    )

    assert "voiceTurn = turn" in voice
    assert "handleVoiceEvent(message, turn)" in voice
    assert handler.index("!voiceIsCurrent(turn)") < handler.index("JSON.parse")
    assert "voiceTurn === turn" in current
    assert "isCurrentTurn(turn)" in current
    assert "playResponse(turn," in handler
    assert "playResponse(beginTurn()" not in handler
    assert "socket.onmessage = null" in cleanup
    assert 'window.addEventListener("beforeunload", () => cleanupVoice())' in APP_JS


def test_submitting_cannot_start_a_second_concurrent_request() -> None:
    body = _block(APP_JS, 'form.addEventListener("submit"')

    assert "if (pending) return;" in body


def test_focus_is_only_restored_inside_the_submit_gesture() -> None:
    """Focusing outside a gesture is what leaves an iOS field unresponsive."""

    body = _block(APP_JS, 'form.addEventListener("submit"')

    assert APP_JS.count("input.focus()") == 1
    assert "input.focus()" in body
    assert "document.activeElement === input" in body


def test_microphone_uses_tap_to_record_and_handles_pointer_cancellation() -> None:
    toggle = _block(APP_JS, "function toggleVoice")
    cancellation = _block(APP_JS, "function handleVoicePointerCancel")

    assert 'micButton.addEventListener("click", toggleVoice)' in APP_JS
    assert "VOICE_STATE.IDLE" in toggle and "beginVoice(event)" in toggle
    assert "VOICE_STATE.LISTENING" in toggle and "stopVoice(event)" in toggle
    assert "pointer_cancel" in cancellation
    assert 'micButton.addEventListener("pointercancel", handleVoicePointerCancel)' in APP_JS
    assert 'window.addEventListener("pointerup"' not in APP_JS
    assert 'micButton.addEventListener("pointerdown"' not in APP_JS


def test_a_malformed_body_still_reaches_the_error_path() -> None:
    assert APP_JS.count("response.json()") == 1
    reader = _block(APP_JS, "async function readJson")
    sender = _block(APP_JS, "async function sendText")

    assert "response.json()" in reader
    assert 'throw new Error("Invalid server response")' in reader
    assert 'typeof data.response_text !== "string"' in sender


def test_a_malformed_voice_event_does_not_escape_its_handler() -> None:
    body = _block(APP_JS, "function handleVoiceEvent")

    assert body.index("JSON.parse") < body.index("catch")
    assert "cleanupVoice(turn);" in body


def test_the_browser_client_still_sends_its_session_and_csrf_proof() -> None:
    """Composer changes must not weaken the authenticated-request contract."""

    for endpoint in ("/api/chat", "/api/speech", "/api/session/conversation"):
        body = _block(APP_JS, f'fetch("{endpoint}"')
        assert '"X-Butters-CSRF": csrf' in body
    assert APP_JS.count('credentials: "same-origin"') == 4
