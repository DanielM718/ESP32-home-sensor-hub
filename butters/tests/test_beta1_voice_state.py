"""Browser voice contracts where no JavaScript/browser runtime is installed.

These tests deliberately pin the shipped state machine and accessible markup.
They do not claim WebKit or iPhone execution; the ASGI voice protocol is tested
separately with live Python state in ``test_beta1_stt_lifecycle.py``.
"""

from __future__ import annotations

from pathlib import Path

STATIC_ROOT = Path(__file__).resolve().parents[1] / "src/butters/web/static"
APP_JS = (STATIC_ROOT / "assets/app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (STATIC_ROOT / "assets/styles.css").read_text(encoding="utf-8")


def _block(source: str, opening: str) -> str:
    start = source.index(opening)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError(f"unbalanced block after {opening!r}")


def test_voice_lifecycle_is_explicit_bounded_and_recoverable() -> None:
    transitions = _block(APP_JS, "const VOICE_TRANSITIONS")
    transition = _block(APP_JS, "function transitionVoice")

    for state in (
        "idle",
        "requesting_permission",
        "connecting",
        "listening",
        "stopping",
        "transcribing",
        "routing",
        "error",
    ):
        assert state in transitions
    for bounded in (
        "requesting_permission",
        "connecting",
        "listening",
        "stopping",
        "transcribing",
        "routing",
    ):
        assert f"{bounded}:" in APP_JS
    assert "VOICE_TIMEOUT_MS[next]" in transition
    assert "failVoice" in transition
    assert 'stopVoice(null, turn, "maximum_duration")' in transition


def test_first_permission_grant_continues_directly_into_recording() -> None:
    begin = _block(APP_JS, "async function beginVoice")

    audio_context = begin.index("new AudioContextClass()")
    permission = begin.index("await navigator.mediaDevices.getUserMedia")
    connect = begin.index("transitionVoice(VOICE_STATE.CONNECTING")
    listen = begin.index("transitionVoice(VOICE_STATE.LISTENING")
    assert audio_context < permission < connect < listen
    assert "await audioContext.resume()" in begin
    assert "await ready" in begin
    assert "holding" not in begin
    assert APP_JS.count('micButton.addEventListener("click", toggleVoice)') == 1


def test_permission_denial_and_connection_failure_have_visible_error_paths() -> None:
    begin = _block(APP_JS, "async function beginVoice")

    assert 'error.name === "NotAllowedError"' in begin
    assert 'error.name === "SecurityError"' in begin
    assert "Microphone permission was denied." in begin
    assert 'socket.addEventListener("error"' in begin
    assert 'socket.addEventListener("close"' in begin
    assert "voice connection failed" in begin
    assert "failVoice(" in begin


def test_tap_stop_transitions_to_real_transcription_and_cannot_overlap() -> None:
    begin = _block(APP_JS, "async function beginVoice")
    stop = _block(APP_JS, "function stopVoice")
    toggle = _block(APP_JS, "function toggleVoice")

    assert "voiceState !== VOICE_STATE.IDLE" in begin
    assert "voiceSession =" in begin
    assert "voiceState !== VOICE_STATE.LISTENING" in stop
    assert stop.index('type: "stop"') < stop.index(
        "transitionVoice(VOICE_STATE.TRANSCRIBING"
    )
    assert "beginVoice(event)" in toggle
    assert "stopVoice(event)" in toggle
    assert 'window.addEventListener("pointerup"' not in APP_JS
    assert 'micButton.addEventListener("pointerdown"' not in APP_JS


def test_cancellation_stops_capture_and_detaches_stale_socket_callbacks() -> None:
    cancel = _block(APP_JS, "function cancelVoice")
    cleanup = _block(
        APP_JS,
        "function cleanupVoice(expectedTurn = null, preserveError = false)",
    )
    pointer = _block(APP_JS, "function handleVoicePointerCancel")

    assert 'type: "cancel"' in cancel
    assert "cleanupVoice(turn)" in cancel
    assert "stopCaptureResources()" in cleanup
    assert "socket.onmessage = null" in cleanup
    assert "socket.onclose = null" in cleanup
    assert "voiceTurn = 0" in cleanup and "voiceSession = null" in cleanup
    assert 'stopVoice(event, voiceTurn, "pointer_cancel")' in pointer


def test_real_partial_and_final_transcripts_are_shown_in_normal_chat() -> None:
    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")

    assert 'data.type === "partial"' in handler
    assert "Heard so far:" in handler
    assert 'data.type === "final"' in handler
    assert "data.raw_text" in handler and "data.normalized_text" in handler
    assert 'addMessage("user", finalText)' in handler
    assert handler.index('addMessage("user", finalText)') < handler.index(
        "transitionVoice(VOICE_STATE.ROUTING"
    )
    assert 'id="partial"' in INDEX_HTML
    assert 'role="status"' in INDEX_HTML
    assert 'aria-label="Voice transcript"' in INDEX_HTML


def test_assistant_text_is_visible_before_optional_audio_playback() -> None:
    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")

    text_index = handler.index('addMessage("assistant", data.response_text)')
    cleanup_index = handler.index("cleanupVoice(turn)")
    playback_index = handler.index("playResponse(turn, traceId)")
    assert text_index < cleanup_index < playback_index
    assert "voiceOutputEnabled" not in handler[:text_index]


def test_voice_output_switch_is_accessible_persistent_and_mic_independent() -> None:
    loader = _block(APP_JS, "function loadVoiceOutputPreference")
    setter = _block(APP_JS, "function setVoiceOutputPreference")
    player = _block(APP_JS, "async function playResponse")
    begin = _block(APP_JS, "async function beginVoice")

    assert 'id="voice-output-toggle"' in INDEX_HTML
    assert 'role="switch"' in INDEX_HTML
    assert 'aria-checked="true"' in INDEX_HTML
    assert "Voice: On" in INDEX_HTML
    assert "window.localStorage.getItem(VOICE_OUTPUT_KEY)" in loader
    assert "stored === null ? true" in loader
    assert "window.localStorage.setItem" in setter
    assert "if (!voiceOutputEnabled) stopPlayback()" in setter
    assert "traceId && voiceOutputEnabled" in player
    assert "voiceOutputEnabled" not in begin
    assert "min-height:44px" in STYLES_CSS


def test_play_rejection_focus_change_and_navigation_cannot_hide_text() -> None:
    speak = _block(APP_JS, "async function speak")
    cleanup = _block(
        APP_JS,
        "function cleanupVoice(expectedTurn = null, preserveError = false)",
    )

    assert "started.catch(finish)" in speak
    assert 'audio.addEventListener("pause", finish' in speak
    assert "setTimeout(finish, PLAYBACK_LIMIT_MS)" in speak
    assert 'window.addEventListener("beforeunload", () => cleanupVoice())' in APP_JS
    assert "socket.onmessage = null" in cleanup


def test_stale_voice_and_audio_events_are_owned_by_their_turn() -> None:
    current = _block(APP_JS, "function voiceIsCurrent(turn)")
    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")
    playback = _block(APP_JS, "function finishPlayback(playback)")

    assert "voiceTurn === turn" in current and "isCurrentTurn(turn)" in current
    assert handler.index("!voiceIsCurrent(turn)") < handler.index("JSON.parse")
    assert "playback.settled" in playback
    assert "if (currentPlayback === playback)" in APP_JS
