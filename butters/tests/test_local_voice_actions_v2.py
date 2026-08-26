from __future__ import annotations

import time
from dataclasses import replace

from butters.actions.coordinator import ActionCoordinator
from butters.actions.store import ActionStateStore
from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.integrations.model import SensorSnapshot, ServerHealthSnapshot
from butters.live.authorization import LocalVoiceAuthorization
from butters.skills.model import AuthenticationLevel
from butters.stt.normalization import DomainVocabulary


class Sensors:
    def snapshot(self):
        return SensorSnapshot("2026-08-15T12:00:00Z", ())


class Health:
    def snapshot(self):
        return ServerHealthSnapshot(1, 0, 0, 0, 1, 0, 1, 1, 40, "0x0", ())


class Printer:
    def current(self):
        raise RuntimeError("not used")

    def environment_summary(self):
        raise RuntimeError("not used")

    def intelligence(self):
        raise RuntimeError("not used")

    def current_session(self):
        return None

    def recent_sessions(self, _limit):
        return ()

    def session(self, _print_id):
        return None


class Desktop:
    def __init__(self) -> None:
        self.calls = 0

    def status(self, machine):
        from butters.integrations.desktop import DesktopState

        return DesktopState(machine, True, True, True)

    def start_remote_session(self, machine, *, cancel_event=None):
        self.calls += 1
        return {
            "machine": machine,
            "wake_sent": False,
            "network_reachable": True,
            "ssh_ready": True,
            "headless_mode_requested": True,
            "parsec_ready": True,
            "verification_complete": True,
            "elapsed_ms": 1,
            "failed_stage": None,
            "error": None,
        }


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def _voice(tmp_path):
    settings = load_assistant_settings()
    settings = replace(
        settings,
        broker=replace(settings.broker, enabled=True),
        desktop=replace(settings.desktop, restart_enabled=True, headless_enabled=True),
    )
    state = ActionStateStore(tmp_path / "actions.sqlite3", settings.actions)
    assistant = create_assistant(
        settings,
        DomainVocabulary((), ()),
        sensor_adapter=Sensors(),
        server_adapter=Health(),
        printer_adapter=Printer(),
        action_state=state,
    )
    implementation = assistant.skills.get(
        "start_remote_desktop_session"
    ).implementation.__self__
    desktop = Desktop()
    implementation.desktop = desktop
    clock = Clock()
    voice = LocalVoiceAuthorization(
        assistant,
        ActionCoordinator(assistant.skills, state),
        context_seconds=30,
        clock=clock,
    )
    return voice, state, desktop, clock


def _wait_for_call(desktop: Desktop) -> None:
    deadline = time.time() + 2
    while time.time() < deadline and desktop.calls == 0:
        time.sleep(0.01)


def test_voice_action_uses_one_wake_session_for_request_and_exact_yes(tmp_path) -> None:
    voice, _state, desktop, _clock = _voice(tmp_path)
    no_wake = voice.handle_text("Turn on my computer and get Parsec ready")
    assert no_wake and no_wake.policy_status == "local_console_required"
    voice.note_physical_wake()
    prompt = voice.handle_text("Turn on my computer and get Parsec ready")
    assert prompt and prompt.policy_status == "confirmation_required"
    assert desktop.calls == 0
    confirmed = voice.handle_text("do it")
    assert confirmed and confirmed.policy_status == "action_started"
    _wait_for_call(desktop)
    assert desktop.calls == 1


def test_voice_no_unrelated_speech_and_each_action_needs_confirmation(tmp_path) -> None:
    voice, _state, desktop, _clock = _voice(tmp_path)
    voice.note_physical_wake()
    voice.handle_text("Turn on my computer and get Parsec ready")
    cancelled = voice.handle_text("no")
    assert cancelled and cancelled.policy_status == "cancelled"
    assert desktop.calls == 0

    voice.note_physical_wake()
    voice.handle_text("Turn on my computer and get Parsec ready")
    unrelated = voice.handle_text("maybe later")
    assert unrelated and unrelated.policy_status == "confirmation_denied"
    assert desktop.calls == 0

    voice.note_physical_wake()
    second = voice.handle_text("Turn on my computer and get Parsec ready")
    assert second and second.policy_status == "confirmation_required"
    assert desktop.calls == 0


def test_expired_or_new_voice_session_cannot_confirm_frozen_action(tmp_path) -> None:
    voice, state, desktop, clock = _voice(tmp_path)
    voice.note_physical_wake()
    voice.handle_text("Turn on my computer and get Parsec ready")
    clock.now += 31
    expired = voice.handle_text("yes")
    assert expired and expired.policy_status == "confirmation_expired"
    assert desktop.calls == 0

    voice.note_physical_wake()
    voice.handle_text("Turn on my computer and get Parsec ready")
    voice.note_physical_wake()
    assert voice.handle_text("yes") is None
    assert desktop.calls == 0
    assert not state.jobs(identity="local-console")


def test_physical_listening_timeout_cancels_pending_confirmation(tmp_path) -> None:
    voice, state, desktop, _clock = _voice(tmp_path)
    voice.note_physical_wake()
    voice.handle_text("Turn on my computer and get Parsec ready")

    voice.cancel_pending_confirmation()

    assert voice.handle_text("yes") is None
    assert desktop.calls == 0
    assert not state.jobs(identity="local-console")


def test_local_console_cannot_satisfy_fresh_desktop_restart(tmp_path) -> None:
    voice, state, desktop, _clock = _voice(tmp_path)
    voice.note_physical_wake()
    response = voice.handle_text("Restart my desktop")
    assert response and response.policy_status == "fresh_authentication_required"
    assert "passkey" in response.response_text.casefold()
    assert desktop.calls == 0
    assert not state.jobs(identity="local-console")
    spec = voice.assistant.skills.get("restart_desktop")
    assert spec and spec.authentication is AuthenticationLevel.FRESH
