"""Browser interaction-generation, Clear, renewal, and cancellation contracts.

No JavaScript runtime is available in this repository, so these are source
contract assertions against the shipped asset, exactly like the existing
composer and voice-state suites. They pin the structure that makes the
guarantees hold; they are not browser execution coverage.
"""

from __future__ import annotations

from pathlib import Path


APP_JS = (
    Path(__file__).resolve().parents[1] / "src/butters/web/static/assets/app.js"
).read_text(encoding="utf-8")


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


# --- generation model -----------------------------------------------------


def test_every_new_turn_retires_the_previous_generation() -> None:
    begin = _block(APP_JS, "function beginTurn()")

    assert begin.index("retireGeneration()") < begin.index("currentTurn += 1")


def test_retiring_a_generation_aborts_the_work_it_owns() -> None:
    retire = _block(APP_JS, "function retireGeneration()")

    assert "generationWork[key] = null" in retire
    assert "controller.abort()" in retire
    assert "const generationWork = {chat: null, speech: null};" in APP_JS


def test_the_chat_request_is_registered_so_it_can_be_retired() -> None:
    send = _block(APP_JS, "async function sendText")

    assert "generationWork.chat = controller" in send


def test_the_speech_request_is_registered_so_it_can_be_retired() -> None:
    speak = _block(APP_JS, "async function speak")

    assert "generationWork.speech = controller" in speak
    assert "signal: controller.signal" in speak


# --- Clear ----------------------------------------------------------------


def test_clear_terminates_playback_capture_and_the_voice_socket() -> None:
    clear = _block(APP_JS, "async function clearConversation()")

    assert clear.index("beginTurn()") < clear.index("stopPlayback()")
    assert "cleanupVoice()" in clear
    assert "setPending(false)" in clear
    assert clear.index("stopPlayback()") < clear.index('fetch("/api/session/conversation"')


def test_clear_aborts_synthesis_that_is_still_being_fetched() -> None:
    stop = _block(APP_JS, "function stopPlayback()")

    assert "generationWork.speech" in stop
    assert stop.index("speech.abort()") < stop.index("const playback = currentPlayback")


def test_a_late_reply_cannot_repopulate_a_cleared_conversation() -> None:
    send = _block(APP_JS, "async function sendText")
    success = send[send.index("if (response.ok)") :]

    # The generation guard must precede every render of the reply.
    assert success.index("if (!isCurrentTurn(turn))") < success.index(
        'addMessage("assistant", data.response_text)'
    )


def test_a_late_failure_cannot_repopulate_a_cleared_conversation() -> None:
    send = _block(APP_JS, "async function sendText")
    failure = send[send.index("} catch (error)") :]

    assert failure.index("if (!isCurrentTurn(turn))") < failure.index("addMessage")
    assert failure.index("if (!isCurrentTurn(turn))") < failure.index("setState")


def test_a_retired_generation_cannot_restore_a_newer_composer() -> None:
    send = _block(APP_JS, "async function sendText")
    finalizer = _block(send, "} finally")

    assert "if (isCurrentTurn(turn)) setPending(false);" in finalizer


def test_clear_returns_the_browser_to_a_usable_idle_state() -> None:
    clear = _block(APP_JS, "async function clearConversation()")

    assert 'setState("Ready", "idle")' in clear
    # Even a failed server clear leaves the composer usable rather than stuck.
    assert 'setState("Could not clear", "error")' in clear
    assert "setPending(false)" in clear


# --- session renewal ------------------------------------------------------


def test_chat_is_replayed_only_when_the_server_proved_it_never_ran() -> None:
    send = _block(APP_JS, "async function sendText")

    # invalid_session is raised by the session guard before /api/chat reaches
    # the assistant, so it is the only replayable outcome.
    assert 'data.error !== "invalid_session"' in send
    assert "renewed" in send


def test_chat_renewal_is_attempted_at_most_once() -> None:
    send = _block(APP_JS, "async function sendText")

    assert "if (renewed || " in send
    assert "renewed = true" in send
    assert send.count("await renewSession()") == 1


def test_renewal_failure_stops_cleanly_without_retrying() -> None:
    send = _block(APP_JS, "async function sendText")

    assert "if (!(await renewSession()) || !isCurrentTurn(turn))" in send


def test_renewal_is_single_flight_and_never_recurses() -> None:
    renew = _block(APP_JS, "function renewSession()")

    assert "if (renewing) return renewing;" in renew
    assert "renewing = null" in renew
    # Renewal re-runs the ordinary bootstrap endpoint; it never calls itself.
    assert "renewSession(" not in renew


def test_privileged_actions_are_never_automatically_replayed() -> None:
    api = _block(APP_JS, "async function api(path, options = {})")

    # The shared helper used by passkey, lock, and action endpoints performs no
    # renewal and no retry of any kind.
    assert "renewSession" not in api
    assert "retry" not in api.lower()


def test_voice_recovery_never_replays_captured_audio() -> None:
    begin = _block(APP_JS, "async function beginVoice(event)")
    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")

    assert "renewSession()" in begin
    assert "renewSession()" in handler
    # Recovery re-bootstraps the session only; nothing re-sends PCM frames.
    for body in (begin, handler):
        recovery = body[body.index("renewSession()") :]
        assert "socket.send" not in recovery


# --- cancellation semantics ----------------------------------------------


def test_the_client_never_claims_a_cancellation_it_cannot_observe() -> None:
    assert "CAPTURE_CANCELLED" in APP_JS
    assert "STOPPED_WAITING" in APP_JS
    assert "SUPPRESSED" in APP_JS
    # There is deliberately no client-side "backend cancelled" outcome.
    assert "BACKEND_CANCELLED" not in APP_JS


def test_a_client_timeout_is_reported_as_the_browser_giving_up() -> None:
    send = _block(APP_JS, "async function sendText")

    assert "I stopped waiting for that response" in send
    assert "The server may still be finishing it" in send
    # The old wording claimed the request itself had timed out and failed.
    assert "The request timed out. Please try again." not in APP_JS


def test_capture_cancellation_is_named_as_capture_not_as_a_server_stop() -> None:
    cancel = _block(APP_JS, "function cancelVoice(turn = voiceTurn")

    assert "noteClientStop(CLIENT_STOP.CAPTURE_CANCELLED)" in cancel


# --- frontend state consistency ------------------------------------------


def test_a_failed_session_bootstrap_withholds_the_composer_controls() -> None:
    init = _block(APP_JS, "async function initialize()")
    ready = _block(APP_JS, "function setSessionReady(ready)")

    assert "setSessionReady(false)" in init
    assert 'csrf = ""' in init
    assert "sendButton.disabled" in ready
    assert "micButton.disabled" in ready
    # The text field itself is still never disabled.
    assert "input.disabled" not in APP_JS


def test_the_composer_starts_withheld_before_the_session_exists() -> None:
    tail = APP_JS[APP_JS.index("micButton.addEventListener") :]

    assert tail.index("setSessionReady(false)") < tail.index("initialize()")


def test_a_pending_request_and_a_missing_session_both_withhold_send() -> None:
    body = _block(APP_JS, "function setPending(value)")

    assert "sendButton.disabled = value || !sessionReady" in body
    assert "input" not in body


def test_stop_speaking_is_offered_while_synthesis_is_still_pending() -> None:
    speak = _block(APP_JS, "async function speak")

    assert speak.index("stopAudio.hidden = false") < speak.index('fetch("/api/speech"')


def test_a_speech_failure_does_not_fail_the_chat_turn() -> None:
    play = _block(APP_JS, "async function playResponse(turn, traceId)")

    assert '"Ready · voice unavailable"' in play
    # The turn still settles on an idle state, so the composer stays usable.
    assert '"idle"' in play
    assert '"error"' not in play
