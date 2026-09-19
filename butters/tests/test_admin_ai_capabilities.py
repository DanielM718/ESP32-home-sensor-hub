"""The provider capability registry is authoritative for what Admin may set.

These tests assert the structural property the registry exists for: there is
one description of which providers, models, voices, and parameters are real,
the browser cannot widen it, and a combination the selected model does not
support is refused by the server rather than merely hidden in the page.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from butters.ai.capabilities import CapabilityError, build_registry
from butters.ai.model import validate_chat, validate_speech
from butters.ai.store import AISettingsStore
from butters.assistant_config import load_assistant_settings

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
ADMIN_HTML = (STATIC / "admin.html").read_text()
ADMIN_JS = (STATIC / "assets/admin.js").read_text()


@pytest.fixture()
def registry():
    return build_registry(load_assistant_settings())


# ------------------------------- catalog ------------------------------------


def test_chat_catalog_is_derived_from_reviewed_pricing(registry) -> None:
    """A model cannot be offered unless Butters already holds its pricing."""

    settings = load_assistant_settings()
    offered = {item.id for item in registry.provider("openai").chat_models}

    assert offered == set(settings.cloud.pricing)


def test_chat_catalog_follows_a_pricing_change_without_a_second_edit() -> None:
    """The dropdown cannot drift away from the gate that authorizes a call."""

    base = load_assistant_settings()
    narrowed = replace(base, cloud=replace(base.cloud, max_output_tokens=900))
    registry = build_registry(narrowed)

    assert registry.max_output_tokens == 900
    assert [item.id for item in registry.provider("openai").chat_models] == list(
        narrowed.cloud.pricing
    )


def test_chat_selector_excludes_speech_and_non_chat_endpoints(registry) -> None:
    chat_models = {item.id for item in registry.provider("openai").chat_models}
    speech_models = {item.id for item in registry.provider("openai").speech_models}

    assert not chat_models & speech_models
    assert not any("tts" in item or "embed" in item for item in chat_models)
    assert speech_models == {"gpt-4o-mini-tts", "tts-1", "tts-1-hd"}


def test_local_provider_offers_exactly_the_one_voice_it_can_produce(registry) -> None:
    """The bundled Piper model has one speaker; offering a list would be a lie."""

    local = registry.provider("local")

    assert not local.chat_models
    assert [item.id for item in local.speech_models] == ["local-piper"]
    assert local.speech_models[0].voice_ids() == ("kathleen",)
    assert local.speech_models[0].supports_instructions is False


def test_serialized_catalog_carries_per_model_support_flags(registry) -> None:
    catalog = registry.as_dict()
    openai = next(item for item in catalog["chat_providers"] if item["id"] == "openai")
    expressive = next(
        item
        for item in next(
            provider
            for provider in catalog["speech_providers"]
            if provider["id"] == "openai"
        )["speech_models"]
        if item["id"] == "gpt-4o-mini-tts"
    )
    classic = next(
        item
        for item in next(
            provider
            for provider in catalog["speech_providers"]
            if provider["id"] == "openai"
        )["speech_models"]
        if item["id"] == "tts-1"
    )

    assert openai["chat_models"][0]["supports"]["reasoning_effort"] is True
    assert openai["chat_models"][0]["supports"]["temperature"] is False
    assert expressive["supports"]["instructions"] is True
    assert classic["supports"]["instructions"] is False
    assert "cedar" in {voice["id"] for voice in expressive["voices"]}
    assert "cedar" not in {voice["id"] for voice in classic["voices"]}


# --------------------------- server-side refusal ----------------------------


def test_unsupported_model_provider_pair_is_rejected(registry) -> None:
    with pytest.raises(CapabilityError) as denied:
        validate_chat(registry, {"provider": "openai", "model": "gpt-4o-mini-tts"})
    assert denied.value.code == "model_denied"

    with pytest.raises(CapabilityError) as unknown:
        validate_chat(registry, {"provider": "anthropic", "model": "gpt-5.6-sol"})
    assert unknown.value.code == "provider_denied"


def test_local_provider_is_refused_as_a_chat_provider(registry) -> None:
    with pytest.raises(CapabilityError) as denied:
        validate_chat(registry, {"provider": "local", "model": "local-piper"})
    assert denied.value.code in {"provider_denied", "model_denied"}


def test_unsupported_reasoning_level_is_rejected(registry) -> None:
    with pytest.raises(CapabilityError) as denied:
        validate_chat(
            registry,
            {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "extreme",
            },
        )
    assert denied.value.code == "invalid_reasoning_effort"


def test_sampling_control_the_model_rejects_is_refused_not_dropped(registry) -> None:
    """Explicit semantics: a supplied unsupported parameter fails loudly.

    Silently dropping it would let Admin display a temperature the provider
    never receives, which is exactly the mismatch this work removes.
    """

    for field, value in (("temperature", 0.7), ("top_p", 0.4)):
        with pytest.raises(CapabilityError) as denied:
            validate_chat(
                registry,
                {"provider": "openai", "model": "gpt-5.6-sol", field: value},
            )
        assert denied.value.code == "unsupported_parameter"


def test_unset_controls_stay_unset_rather_than_defaulted(registry) -> None:
    saved = validate_chat(registry, {"provider": "openai", "model": "gpt-5.6-terra"})

    assert saved.reasoning_effort is None
    assert saved.verbosity is None
    assert saved.max_output_tokens is None
    assert saved.store_responses is None


def test_unknown_parameter_names_are_refused(registry) -> None:
    with pytest.raises(CapabilityError) as denied:
        validate_chat(
            registry,
            {"provider": "openai", "model": "gpt-5.6-terra", "frequency_penalty": 1},
        )
    assert denied.value.code == "unknown_parameter"


def test_output_ceiling_is_enforced_server_side(registry) -> None:
    with pytest.raises(CapabilityError) as denied:
        validate_chat(
            registry,
            {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "max_output_tokens": registry.max_output_tokens + 1,
            },
        )
    assert denied.value.code == "invalid_max_output_tokens"


# ------------------------------- speech -------------------------------------


def test_voice_must_belong_to_the_selected_speech_model(registry) -> None:
    with pytest.raises(CapabilityError) as denied:
        validate_speech(
            registry,
            {"provider": "openai", "model": "tts-1", "voice": "cedar"},
        )
    assert denied.value.code == "invalid_voice"

    accepted = validate_speech(
        registry,
        {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "cedar"},
    )
    assert accepted.voice == "cedar"


def test_identifiers_are_canonical_and_case_sensitive(registry) -> None:
    """Admin sends a registry identifier; it never guesses at capitalization."""

    with pytest.raises(CapabilityError):
        validate_speech(
            registry,
            {"provider": "OpenAI", "model": "gpt-4o-mini-tts", "voice": "cedar"},
        )
    with pytest.raises(CapabilityError) as denied:
        validate_speech(
            registry,
            {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "Cedar"},
        )
    assert denied.value.code == "invalid_voice"


def test_speed_range_is_taken_from_the_selected_model(registry) -> None:
    # The local engine is time-scaled and has a narrower reviewed band.
    with pytest.raises(CapabilityError) as denied:
        validate_speech(
            registry,
            {
                "provider": "local",
                "model": "local-piper",
                "voice": "kathleen",
                "speed": 3.0,
            },
        )
    assert denied.value.code == "invalid_speed"

    accepted = validate_speech(
        registry,
        {
            "provider": "openai",
            "model": "gpt-4o-mini-tts",
            "voice": "cedar",
            "speed": 3.0,
        },
    )
    assert accepted.speed == 3.0


def test_instructions_are_accepted_only_by_a_model_that_consumes_them(registry) -> None:
    accepted = validate_speech(
        registry,
        {
            "provider": "openai",
            "model": "gpt-4o-mini-tts",
            "voice": "cedar",
            "instructions": "Warm and calm.",
        },
    )
    assert accepted.instructions == "Warm and calm."

    for model, provider in (("tts-1", "openai"), ("local-piper", "local")):
        with pytest.raises(CapabilityError) as denied:
            validate_speech(
                registry,
                {
                    "provider": provider,
                    "model": model,
                    "voice": "alloy" if provider == "openai" else "kathleen",
                    "instructions": "Warm and calm.",
                },
            )
        assert denied.value.code == "unsupported_parameter"


def test_blank_instructions_are_treated_as_unset_everywhere(registry) -> None:
    saved = validate_speech(
        registry,
        {
            "provider": "local",
            "model": "local-piper",
            "voice": "kathleen",
            "instructions": "   ",
        },
    )
    assert saved.instructions is None


# --------------------------- provider switching -----------------------------


def test_switching_provider_preserves_each_profile_separately(tmp_path: Path, registry) -> None:
    store = AISettingsStore(tmp_path / "state.sqlite3")
    store.save_speech(
        validate_speech(
            registry,
            {
                "provider": "openai",
                "model": "gpt-4o-mini-tts",
                "voice": "marin",
                "speed": 1.2,
                "instructions": "Brisk.",
            },
        )
    )
    openai_profile = store.speech(registry, "local")

    store.save_speech(
        validate_speech(
            registry,
            {"provider": "local", "model": "local-piper", "voice": "kathleen", "speed": 0.9},
        )
    )
    local_profile = store.speech(registry, "local")

    store.save_speech(openai_profile)
    restored = store.speech(registry, "local")

    assert local_profile.provider == "local" and local_profile.speed == 0.9
    # Switching back restores the exact configuration, style included.
    assert restored == openai_profile
    assert restored.voice == "marin" and restored.instructions == "Brisk."


def test_a_stale_style_cannot_survive_into_a_model_that_ignores_it(registry) -> None:
    """A style saved for the expressive model is refused for tts-1."""

    with pytest.raises(CapabilityError):
        validate_speech(
            registry,
            {
                "provider": "openai",
                "model": "tts-1",
                "voice": "alloy",
                "instructions": "Brisk.",
            },
        )


def test_store_refuses_anything_shaped_like_a_credential(tmp_path: Path) -> None:
    store = AISettingsStore(tmp_path / "state.sqlite3")
    with pytest.raises(CapabilityError) as denied:
        store._write("chat", "openai", {"provider": "openai", "api_key": "sk-x"})
    assert denied.value.code == "secret_rejected"


# ------------------------------ Admin page ----------------------------------


def test_admin_page_has_no_free_text_model_or_voice_entry() -> None:
    """The historical free-text model/voice inputs are gone, not merely styled."""

    for identifier in ("tts-provider", "tts-model", "tts-voice", "chat-provider", "chat-model"):
        assert re.search(rf'<select id="{identifier}">', ADMIN_HTML), identifier
    for removed in ('id="voice-model"', 'id="voice-name"'):
        assert removed not in ADMIN_HTML, removed
    assert 'id="tts-speed" type="range"' in ADMIN_HTML


def test_advanced_controls_are_collapsed_by_default() -> None:
    for block in ('<details id="chat-advanced"', '<details id="tts-advanced"'):
        assert block in ADMIN_HTML
        assert f"{block} class=\"advanced-block\" open" not in ADMIN_HTML


def test_admin_javascript_hardcodes_no_model_or_voice_identifier() -> None:
    """The page consumes the catalog; it does not carry a second copy of it."""

    section = ADMIN_JS[ADMIN_JS.index("let aiCatalog = null;"):]
    for identifier in ("gpt-5.6-", "gpt-4o-mini-tts", "tts-1", "cedar", "kathleen", "local-piper"):
        assert identifier not in section, identifier
