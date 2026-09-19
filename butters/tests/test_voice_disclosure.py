"""Butters says plainly that its spoken voice is synthetic.

OpenAI's text-to-speech guidance requires disclosing that a generated voice is
not a human one. The wording is derived server-side from the provider actually
in effect, so the page cannot claim OpenAI after a switch back to the
on-device engine, nor fall silent after a switch to it.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import httpx
from butters.assistant_config import load_assistant_settings
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
INDEX = (STATIC / "index.html").read_text()
APP_JS = (STATIC / "assets/app.js").read_text()
STYLES = (STATIC / "assets/styles.css").read_text()


class NoCloud:
    available = False


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


def _service(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=("admin@example.com",),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    service = BetaAssistantService(
        settings, vocabulary, general_reasoner=NoCloud(), state_dir=tmp_path
    )
    app = create_app(settings, vocabulary, service, stt_engine_factory=Engine)
    return app, service


# ------------------------------ the markup ---------------------------------


def test_the_chat_page_carries_a_disclosure_before_any_script_runs() -> None:
    """A safe, truthful default ships in the HTML itself."""

    assert 'id="voice-disclosure"' in INDEX
    element = re.search(r'<p id="voice-disclosure"[^>]*>([^<]+)</p>', INDEX)
    assert element, "the disclosure element is missing"
    assert "AI-generated" in element.group(1)
    assert "not a human voice" in element.group(1)
    assert 'role="note"' in element.group(0)


def test_the_disclosure_is_styled_and_does_not_break_the_layout() -> None:
    assert ".voice-disclosure{" in STYLES
    # The shell gained a row, so its grid template must account for it.
    assert ".chat-shell{grid-template-rows:auto auto 1fr auto auto}" in STYLES


def test_the_page_takes_its_wording_from_the_server() -> None:
    assert "applyVoiceDisclosure(data.voice_disclosure)" in APP_JS
    # And keeps the safe default rather than blanking it if the server is quiet.
    body = APP_JS[APP_JS.index("function applyVoiceDisclosure") :]
    assert "if (text) node.textContent = text;" in body


def test_the_disclosure_is_not_repeated_per_synthesis() -> None:
    """Disclosure by nuisance is not the goal; it is shown once."""

    assert APP_JS.count("applyVoiceDisclosure(") == 2  # definition + one call
    # Nothing prepends it to spoken text or to each assistant message.
    assert "AI-generated" not in APP_JS


# ----------------------------- the server ----------------------------------


async def _session_payload(app) -> dict:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/api/session")
        assert response.status_code == 200, response.text
        return response.json()


async def _disclosure_follows_the_effective_provider(tmp_path, monkeypatch):
    app, service = _service(tmp_path, monkeypatch)

    payload = await _session_payload(app)
    local = payload["voice_disclosure"]
    assert local["ai_generated"] is True
    assert local["provider"] == "local"
    assert local["text"] == "Voice responses are AI-generated using an on-device model."

    service.ai.apply_speech(
        {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "cedar", "speed": 1.0}
    )
    payload = await _session_payload(app)
    cloud = payload["voice_disclosure"]
    assert cloud["provider"] == "openai"
    assert cloud["text"] == "Voice responses are AI-generated using OpenAI TTS."


def test_disclosure_follows_the_effective_provider(tmp_path, monkeypatch) -> None:
    asyncio.run(_disclosure_follows_the_effective_provider(tmp_path, monkeypatch))


async def _every_provider_discloses_something_truthful(tmp_path, monkeypatch):
    app, service = _service(tmp_path, monkeypatch)
    for provider, model, voice in (
        ("local", "local-piper", "kathleen"),
        ("openai", "tts-1", "alloy"),
        ("openai", "gpt-4o-mini-tts", "cedar"),
    ):
        service.ai.apply_speech(
            {"provider": provider, "model": model, "voice": voice, "speed": 1.0}
        )
        disclosure = (await _session_payload(app))["voice_disclosure"]
        assert disclosure["ai_generated"] is True
        assert "AI-generated" in disclosure["text"]
        assert disclosure["provider"] == provider


def test_every_provider_discloses_something_truthful(tmp_path, monkeypatch) -> None:
    asyncio.run(_every_provider_discloses_something_truthful(tmp_path, monkeypatch))


def test_the_disclosure_carries_no_configuration_detail(tmp_path, monkeypatch) -> None:
    """It names the provider, never the voice, model, key, or cost."""

    _app, service = _service(tmp_path, monkeypatch)
    service.ai.apply_speech(
        {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "cedar", "speed": 1.0}
    )
    disclosure = json.dumps(service.voice_disclosure())

    for leaked in ("cedar", "gpt-4o-mini-tts", "sk-", "api_key", "cost", "0.0018"):
        assert leaked not in disclosure, leaked
