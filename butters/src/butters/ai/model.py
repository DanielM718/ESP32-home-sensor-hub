"""Typed, capability-validated Admin AI and speech settings.

Unset means unset. A control the administrator has not set is stored as
``None`` and is omitted from the upstream request body rather than replaced by
an invented default, so Butters never asserts a value the administrator did
not choose.

A parameter the selected model does not support is refused, not dropped. A
silent drop would let Admin display a temperature that the provider never
sees; refusing keeps the displayed configuration and the sent configuration
the same object.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from butters.ai.capabilities import (
    TRUNCATION_MODES,
    VERBOSITY_LEVELS,
    CapabilityError,
    CapabilityRegistry,
)
from butters.cloud.adaptive import (
    DEFAULT_MAX_AUTOMATIC_TIER,
    DEFAULT_ROUTING_MODE,
    ROUTING_MODES,
    TIER_MODELS,
)

MAX_INSTRUCTIONS_CHARS = 1000


@dataclass(frozen=True, slots=True)
class ChatSettings:
    provider: str
    model: str
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    verbosity: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    truncation: str | None = None
    parallel_tool_calls: bool | None = None
    max_tool_calls: int | None = None
    store_responses: bool | None = None
    prompt_cache_enabled: bool | None = None
    # How the per-request cloud model and effort are chosen. Unset means this
    # profile was written before adaptive routing existed, and an unset
    # profile keeps behaving exactly as it did: fixed.
    routing_mode: str | None = None
    # The strongest model adaptive mode may reach on its own. Unset means the
    # whole reviewed ladder; the MAXIMUM rung stays separately gated by
    # `cloud.allow_automatic_maximum`.
    max_automatic_tier: str | None = None
    # Whether to ask the provider for a summary of its own reasoning and show
    # it. Display and request behaviour only - it never changes effort, and
    # it never reaches the spoken answer.
    reasoning_summary_enabled: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def effective_routing_mode(self) -> str:
        return self.routing_mode or DEFAULT_ROUTING_MODE

    @property
    def effective_max_automatic_tier(self) -> str:
        return self.max_automatic_tier or DEFAULT_MAX_AUTOMATIC_TIER

    @property
    def adaptive(self) -> bool:
        return self.effective_routing_mode == "adaptive"


@dataclass(frozen=True, slots=True)
class SpeechSettings:
    provider: str
    model: str
    voice: str
    speed: float | None = None
    instructions: str | None = None
    audio_format: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_CHAT_FIELDS = frozenset(ChatSettings.__dataclass_fields__)
_SPEECH_FIELDS = frozenset(SpeechSettings.__dataclass_fields__)


def validate_chat(
    registry: CapabilityRegistry, payload: dict[str, object]
) -> ChatSettings:
    unknown = set(payload) - _CHAT_FIELDS
    if unknown:
        raise CapabilityError(
            "unknown_parameter",
            f"unsupported chat parameter: {min(unknown)}",
        )
    provider_id = _required_string(payload, "provider")
    model_id = _required_string(payload, "model")
    provider = registry.provider(provider_id)
    if not provider.chat_models:
        raise CapabilityError(
            "provider_denied", f"{provider_id} has no Butters Chat models"
        )
    model = provider.chat_model(model_id)

    effort = _optional_string(payload, "reasoning_effort")
    if effort is not None:
        _require(model.supports_reasoning_effort, "reasoning_effort", model.id)
        if effort not in model.reasoning_efforts:
            raise CapabilityError(
                "invalid_reasoning_effort", f"{model.id} does not accept effort {effort}"
            )

    verbosity = _optional_string(payload, "verbosity")
    if verbosity is not None:
        _require(model.supports_verbosity, "verbosity", model.id)
        if verbosity not in VERBOSITY_LEVELS:
            raise CapabilityError("invalid_verbosity", "verbosity is not allow-listed")

    truncation = _optional_string(payload, "truncation")
    if truncation is not None:
        _require(model.supports_truncation, "truncation", model.id)
        if truncation not in TRUNCATION_MODES:
            raise CapabilityError("invalid_truncation", "truncation is not allow-listed")

    output_limit = _optional_int(payload, "max_output_tokens")
    if output_limit is not None and not 64 <= output_limit <= registry.max_output_tokens:
        raise CapabilityError(
            "invalid_max_output_tokens",
            f"max_output_tokens must be 64 to {registry.max_output_tokens}",
        )

    temperature = _optional_float(payload, "temperature")
    if temperature is not None:
        _require(model.supports_temperature, "temperature", model.id)
        if not 0.0 <= temperature <= 2.0:
            raise CapabilityError("invalid_temperature", "temperature must be 0 to 2")

    top_p = _optional_float(payload, "top_p")
    if top_p is not None:
        _require(model.supports_top_p, "top_p", model.id)
        if not 0.0 < top_p <= 1.0:
            raise CapabilityError("invalid_top_p", "top_p must be greater than 0 and at most 1")

    tool_calls = _optional_int(payload, "max_tool_calls")
    if tool_calls is not None:
        _require(model.supports_max_tool_calls, "max_tool_calls", model.id)
        if not 0 <= tool_calls <= 32:
            raise CapabilityError("invalid_max_tool_calls", "max_tool_calls must be 0 to 32")

    parallel = _optional_bool(payload, "parallel_tool_calls")
    if parallel is not None:
        _require(model.supports_parallel_tool_calls, "parallel_tool_calls", model.id)
    store_responses = _optional_bool(payload, "store_responses")
    if store_responses is not None:
        _require(model.supports_store, "store_responses", model.id)
    prompt_cache = _optional_bool(payload, "prompt_cache_enabled")
    if prompt_cache is not None:
        _require(model.supports_prompt_cache_key, "prompt_cache_enabled", model.id)

    routing_mode = _optional_string(payload, "routing_mode")
    if routing_mode is not None and routing_mode not in ROUTING_MODES:
        raise CapabilityError("invalid_routing_mode", "routing mode is not allow-listed")

    max_tier = _optional_string(payload, "max_automatic_tier")
    if max_tier is not None and max_tier not in TIER_MODELS:
        raise CapabilityError(
            "invalid_max_automatic_tier", "maximum automatic tier is not allow-listed"
        )

    reasoning_summary = _optional_bool(payload, "reasoning_summary_enabled")
    if reasoning_summary:
        _require(model.supports_reasoning_summary, "reasoning_summary", model.id)

    return ChatSettings(
        provider.id,
        model.id,
        effort,
        output_limit,
        verbosity,
        temperature,
        top_p,
        truncation,
        parallel,
        tool_calls,
        store_responses,
        prompt_cache,
        routing_mode,
        max_tier,
        reasoning_summary,
    )


def validate_speech(
    registry: CapabilityRegistry, payload: dict[str, object]
) -> SpeechSettings:
    unknown = set(payload) - _SPEECH_FIELDS
    if unknown:
        raise CapabilityError(
            "unknown_parameter",
            f"unsupported speech parameter: {min(unknown)}",
        )
    provider_id = _required_string(payload, "provider")
    model_id = _required_string(payload, "model")
    voice_id = _required_string(payload, "voice")
    provider = registry.provider(provider_id)
    if not provider.speech_models:
        raise CapabilityError(
            "provider_denied", f"{provider_id} has no speech models"
        )
    model = provider.speech_model(model_id)
    if voice_id not in model.voice_ids():
        raise CapabilityError(
            "invalid_voice", f"{voice_id} is not a voice of {model.id}"
        )

    speed = _optional_float(payload, "speed")
    if speed is not None:
        _require(model.supports_speed, "speed", model.id)
        low, high = model.speed_range
        if not low <= speed <= high:
            raise CapabilityError(
                "invalid_speed", f"{model.id} accepts a speed of {low} to {high}"
            )

    instructions = _optional_string(payload, "instructions")
    if instructions is not None:
        instructions = instructions.strip()
        if not instructions:
            instructions = None
    if instructions is not None:
        if not model.supports_instructions:
            raise CapabilityError(
                "unsupported_parameter",
                f"{model.id} does not accept speaking instructions",
            )
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            raise CapabilityError(
                "invalid_instructions",
                f"speaking instructions must be at most {MAX_INSTRUCTIONS_CHARS} characters",
            )

    audio_format = _optional_string(payload, "audio_format")
    if audio_format is not None and audio_format not in model.formats:
        raise CapabilityError(
            "invalid_audio_format", f"{model.id} does not produce {audio_format}"
        )

    return SpeechSettings(provider.id, model.id, voice_id, speed, instructions, audio_format)


def default_chat(registry: CapabilityRegistry, settings_model: str) -> ChatSettings:
    """The reviewed starting point: today's hardcoded Chat model and effort."""

    provider = registry.chat_providers()[0]
    model = next(
        (item for item in provider.chat_models if item.id == settings_model),
        provider.chat_models[0],
    )
    return ChatSettings(provider.id, model.id, "high", registry.max_output_tokens, None)


def default_speech(registry: CapabilityRegistry, provider_id: str) -> SpeechSettings:
    provider = registry.provider(provider_id)
    model = provider.speech_models[0]
    return SpeechSettings(
        provider.id,
        model.id,
        model.voice_ids()[0],
        1.0,
        None,
        None,
    )


def clear_unsupported(
    registry: CapabilityRegistry, settings: ChatSettings
) -> ChatSettings:
    """Drop parameters a stored profile kept but the current model rejects.

    This runs only when a stored profile is read back after its model changed
    underneath it - a pricing table edit, for instance. Live Admin submissions
    are refused instead, so nothing is dropped behind the administrator's back
    during normal operation.
    """

    model = registry.chat_model(settings.provider, settings.model)
    changes: dict[str, object] = {}
    if not model.supports_reasoning_effort:
        changes["reasoning_effort"] = None
    if not model.supports_verbosity:
        changes["verbosity"] = None
    if not model.supports_temperature:
        changes["temperature"] = None
    if not model.supports_top_p:
        changes["top_p"] = None
    if not model.supports_truncation:
        changes["truncation"] = None
    if not model.supports_parallel_tool_calls:
        changes["parallel_tool_calls"] = None
    if not model.supports_max_tool_calls:
        changes["max_tool_calls"] = None
    if not model.supports_store:
        changes["store_responses"] = None
    if not model.supports_prompt_cache_key:
        changes["prompt_cache_enabled"] = None
    if not model.supports_reasoning_summary:
        changes["reasoning_summary_enabled"] = None
    return replace(settings, **changes) if changes else settings


def _require(supported: bool, name: str, model_id: str) -> None:
    if not supported:
        raise CapabilityError(
            "unsupported_parameter", f"{model_id} does not support {name}"
        )


def _required_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError("invalid_request", f"{name} is required")
    return value.strip()


def _optional_string(payload: dict[str, object], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CapabilityError("invalid_request", f"{name} must be a string")
    return value


def _optional_int(payload: dict[str, object], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapabilityError("invalid_request", f"{name} must be an integer")
    return value


def _optional_float(payload: dict[str, object], name: str) -> float | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapabilityError("invalid_request", f"{name} must be a number")
    return float(value)


def _optional_bool(payload: dict[str, object], name: str) -> bool | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise CapabilityError("invalid_request", f"{name} must be a boolean")
    return value
