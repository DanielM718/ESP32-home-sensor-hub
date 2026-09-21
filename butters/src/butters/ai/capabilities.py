"""Authoritative provider capability registry for chat and speech synthesis.

The historical Admin surface scattered model and voice knowledge across a
template, a JavaScript file, and several backend conditionals, so a valid
combination in one place was a silent failure in another. This module follows
the precedent set when the model-visible tool catalog stopped being a
hand-maintained list: the backend derives the catalog, the UI consumes it, and
a parity test fails when the two disagree.

Two rules hold throughout:

1. A control exists only when the selected provider *and* model declare it.
   Anything the provider does not support is not merely hidden in the browser;
   the server refuses it and never places it in an upstream request body.
2. Identifiers are canonical. The browser sends an exact registry identifier or
   the request is rejected. Case folding is applied once, here, so an
   administrator never has to reproduce a model or voice spelling by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from butters.assistant_config import AssistantSettings
from butters.cloud.adaptive import ROUTING_MODES, TIER_MODELS


class CapabilityError(ValueError):
    """Raised when a requested provider/model/parameter combination is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Reasoning effort levels Butters already accepts on its own request path.
REASONING_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
VERBOSITY_LEVELS: tuple[str, ...] = ("low", "medium", "high")
# Provider-generated reasoning *summaries*, never raw chain-of-thought. These
# are the values the OpenAI Responses API documents for `reasoning.summary`.
REASONING_SUMMARY_MODES: tuple[str, ...] = ("auto", "concise", "detailed")
TRUNCATION_MODES: tuple[str, ...] = ("auto", "disabled")

# Display names for the reviewed cloud model identifiers. A model that reaches
# the registry without an entry here is labelled with its own canonical ID
# rather than invented prose.
_CHAT_MODEL_LABELS: dict[str, str] = {
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-sol": "GPT-5.6 Sol",
}
_CHAT_MODEL_NOTES: dict[str, str] = {
    "gpt-5.6-luna": "Lowest cost. Short factual answers.",
    "gpt-5.6-terra": "Balanced default for Butters Chat.",
    "gpt-5.6-sol": "Highest capability and highest cost.",
}


@dataclass(frozen=True, slots=True)
class VoiceOption:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class ChatModel:
    """One chat model and the request controls it genuinely accepts.

    `supports_temperature` and `supports_top_p` are false for every reviewed
    model today: the GPT-5.6 reasoning family rejects sampling controls. The
    flags exist so a future non-reasoning model turns the controls on by
    declaring them, not by someone editing a template.
    """

    id: str
    label: str
    note: str = ""
    supports_reasoning_effort: bool = True
    reasoning_efforts: tuple[str, ...] = REASONING_EFFORTS
    supports_verbosity: bool = True
    supports_temperature: bool = False
    supports_top_p: bool = False
    supports_truncation: bool = True
    supports_parallel_tool_calls: bool = True
    supports_max_tool_calls: bool = True
    supports_store: bool = True
    supports_prompt_cache_key: bool = True
    # A reasoning model can be asked for a summary of its own reasoning. This
    # is a distinct capability from reasoning *effort*: effort changes how
    # much the model thinks, the summary only asks it to describe that.
    supports_reasoning_summary: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "note": self.note,
            "supports": {
                "reasoning_effort": self.supports_reasoning_effort,
                "verbosity": self.supports_verbosity,
                "temperature": self.supports_temperature,
                "top_p": self.supports_top_p,
                "truncation": self.supports_truncation,
                "parallel_tool_calls": self.supports_parallel_tool_calls,
                "max_tool_calls": self.supports_max_tool_calls,
                "store": self.supports_store,
                "prompt_cache_key": self.supports_prompt_cache_key,
                "reasoning_summary": self.supports_reasoning_summary,
            },
            "reasoning_efforts": list(self.reasoning_efforts),
        }


@dataclass(frozen=True, slots=True)
class SpeechModel:
    """One speech-synthesis model, its voices, and its supported controls."""

    id: str
    label: str
    voices: tuple[VoiceOption, ...]
    note: str = ""
    supports_instructions: bool = False
    supports_speed: bool = True
    speed_range: tuple[float, float] = (0.25, 4.0)
    speed_step: float = 0.05
    formats: tuple[str, ...] = ("wav",)

    def voice_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.voices)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "note": self.note,
            "voices": [{"id": item.id, "label": item.label} for item in self.voices],
            "supports": {
                "instructions": self.supports_instructions,
                "speed": self.supports_speed,
            },
            "speed": {
                "minimum": self.speed_range[0],
                "maximum": self.speed_range[1],
                "step": self.speed_step,
            },
            "formats": list(self.formats),
        }


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    label: str
    requires_credential: bool
    chat_models: tuple[ChatModel, ...] = ()
    speech_models: tuple[SpeechModel, ...] = ()
    note: str = ""

    def chat_model(self, model_id: str) -> ChatModel:
        for item in self.chat_models:
            if item.id == model_id:
                return item
        raise CapabilityError(
            "model_denied", f"{model_id} is not a chat model for {self.id}"
        )

    def speech_model(self, model_id: str) -> SpeechModel:
        for item in self.speech_models:
            if item.id == model_id:
                return item
        raise CapabilityError(
            "model_denied", f"{model_id} is not a speech model for {self.id}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "note": self.note,
            "requires_credential": self.requires_credential,
            "chat_models": [item.as_dict() for item in self.chat_models],
            "speech_models": [item.as_dict() for item in self.speech_models],
        }


def _voices(*pairs: tuple[str, str]) -> tuple[VoiceOption, ...]:
    return tuple(VoiceOption(identifier, label) for identifier, label in pairs)


# gpt-4o-mini-tts carries the full current voice set and is the only model that
# consumes speaking instructions. Both lists are the provider's, not Butters'
# preference: a voice missing here cannot be selected, so an omission is a
# capability the administrator loses rather than a harmless shortening.
_EXPRESSIVE_VOICES = _voices(
    ("alloy", "Alloy"),
    ("ash", "Ash"),
    ("ballad", "Ballad"),
    ("cedar", "Cedar"),
    ("coral", "Coral"),
    ("echo", "Echo"),
    ("fable", "Fable"),
    ("marin", "Marin"),
    ("nova", "Nova"),
    ("onyx", "Onyx"),
    ("sage", "Sage"),
    ("shimmer", "Shimmer"),
    ("verse", "Verse"),
)
# The tts-1 pair supports a subset of the expressive set: it lacks Ballad and
# Verse, and the two newest voices, Cedar and Marin, are gpt-4o-mini-tts only.
_CLASSIC_VOICES = _voices(
    ("alloy", "Alloy"),
    ("ash", "Ash"),
    ("coral", "Coral"),
    ("echo", "Echo"),
    ("fable", "Fable"),
    ("nova", "Nova"),
    ("onyx", "Onyx"),
    ("sage", "Sage"),
    ("shimmer", "Shimmer"),
)

OPENAI_SPEECH_MODELS: tuple[SpeechModel, ...] = (
    SpeechModel(
        "gpt-4o-mini-tts",
        "GPT-4o mini TTS",
        _EXPRESSIVE_VOICES,
        note="Accepts a speaking style instruction.",
        supports_instructions=True,
    ),
    SpeechModel(
        "tts-1",
        "TTS-1",
        _CLASSIC_VOICES,
        note="Lower latency. Ignores speaking style.",
    ),
    SpeechModel(
        "tts-1-hd",
        "TTS-1 HD",
        _CLASSIC_VOICES,
        note="Higher fidelity. Ignores speaking style.",
    ),
)

# The local engine synthesizes with one Piper model directory, so it has
# exactly one voice. Presenting a choice here would be a lie: the engine has no
# parameter that would change the speaker.
LOCAL_SPEECH_MODEL = SpeechModel(
    "local-piper",
    "Local Piper",
    _voices(("kathleen", "Kathleen (bundled Piper model)")),
    note="One bundled voice. Changing the voice requires a different model directory.",
    supports_instructions=False,
    speed_range=(0.5, 2.0),
)


def build_registry(settings: AssistantSettings) -> CapabilityRegistry:
    """Derive the registry from reviewed configuration rather than a literal list.

    Chat models come from `cloud.pricing`, which is the same mapping that gates
    a paid request. A model cannot therefore appear in the Admin dropdown
    unless Butters already holds reviewed pricing for it, and a pricing change
    cannot leave the dropdown behind.
    """

    chat_models = tuple(
        ChatModel(
            model_id,
            _CHAT_MODEL_LABELS.get(model_id, model_id),
            _CHAT_MODEL_NOTES.get(model_id, ""),
        )
        for model_id in settings.cloud.pricing
    )
    openai = Provider(
        "openai",
        "OpenAI",
        True,
        chat_models=chat_models,
        speech_models=OPENAI_SPEECH_MODELS,
        note="Requires an OpenAI API credential stored in Butters.",
    )
    local = Provider(
        "local",
        "Local (on-device)",
        False,
        chat_models=(),
        speech_models=(LOCAL_SPEECH_MODEL,),
        note="Runs on this host. No credential and no per-character cost.",
    )
    return CapabilityRegistry((openai, local), max_output_tokens=settings.cloud.max_output_tokens)


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    providers: tuple[Provider, ...]
    max_output_tokens: int
    _index: dict[str, Provider] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        # frozen dataclass: populate the lookup without a second constructor.
        self._index.update({item.id: item for item in self.providers})

    def provider(self, provider_id: str) -> Provider:
        found = self._index.get(provider_id)
        if found is None:
            raise CapabilityError(
                "provider_denied", f"{provider_id} is not a supported provider"
            )
        return found

    def chat_providers(self) -> tuple[Provider, ...]:
        return tuple(item for item in self.providers if item.chat_models)

    def speech_providers(self) -> tuple[Provider, ...]:
        return tuple(item for item in self.providers if item.speech_models)

    def chat_model(self, provider_id: str, model_id: str) -> ChatModel:
        return self.provider(provider_id).chat_model(model_id)

    def speech_model(self, provider_id: str, model_id: str) -> SpeechModel:
        return self.provider(provider_id).speech_model(model_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "max_output_tokens": self.max_output_tokens,
            "reasoning_efforts": list(REASONING_EFFORTS),
            "verbosity_levels": list(VERBOSITY_LEVELS),
            "routing_modes": list(ROUTING_MODES),
            "automatic_tiers": list(TIER_MODELS),
            "truncation_modes": list(TRUNCATION_MODES),
            "chat_providers": [item.as_dict() for item in self.chat_providers()],
            "speech_providers": [item.as_dict() for item in self.speech_providers()],
        }
