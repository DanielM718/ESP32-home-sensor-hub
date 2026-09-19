"""Saved configuration, credential validation, and effective runtime - kept apart.

A successful database write is not a successful configuration change. This
controller therefore reports three independent facts and never collapses them:

* SAVED - what is durably stored for the selected provider;
* CREDENTIAL - whether the stored credential authenticates upstream;
* EFFECTIVE - what the live provider objects are actually using right now.

Every turn of Butters Chat reads EFFECTIVE, so Admin cannot display a voice
that synthesis is not using. When an activation fails the previous effective
configuration stays in force, the failure is reported, and nothing claims the
new setting is live.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from butters.ai.capabilities import CapabilityError, CapabilityRegistry, build_registry
from butters.ai.credentials import (
    CredentialError,
    OpenAICredentialStore,
    OpenAICredentialValidator,
    fingerprint,
    normalize_candidate,
)
from butters.ai.model import (
    ChatSettings,
    SpeechSettings,
    validate_chat,
    validate_speech,
)
from butters.ai.store import AISettingsStore
from butters.assistant_config import AssistantSettings


class RuntimeActivationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    """The live objects a successful activation produced."""

    chat: Any
    speech: Any
    transcription: Any
    credential_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class EffectiveRuntime:
    chat: ChatSettings
    speech: SpeechSettings
    credential_fingerprint: str | None
    activated_at: float

    def as_dict(self) -> dict[str, object]:
        return {
            "chat": self.chat.as_dict(),
            "speech": self.speech.as_dict(),
            "credential_fingerprint": self.credential_fingerprint,
            "activated_at": self.activated_at,
        }


class AIRuntimeController:
    def __init__(
        self,
        settings: AssistantSettings,
        state_dir: Path,
        *,
        registry: CapabilityRegistry | None = None,
        credential_store: OpenAICredentialStore | None = None,
        settings_store: AISettingsStore | None = None,
        validator: OpenAICredentialValidator | None = None,
        chat_factory: Callable[[str | None], Any] | None = None,
        speech_factory: Callable[[str | None], Any] | None = None,
        transcription_factory: Callable[[str | None], Any] | None = None,
        install: Callable[[ProviderBundle], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.registry = registry or build_registry(settings)
        self.credentials = credential_store or OpenAICredentialStore(Path(state_dir))
        self.store = settings_store or AISettingsStore(Path(state_dir) / "state.sqlite3")
        self.validator = validator or OpenAICredentialValidator(settings.cloud.base_url)
        self._chat_factory = chat_factory or (lambda _key: None)
        self._speech_factory = speech_factory or (lambda _key: None)
        self._transcription_factory = transcription_factory or (lambda _key: None)
        self._install = install or (lambda _bundle: None)
        self._clock = clock
        self._activation_error: dict[str, object] | None = None
        self._effective = self._initial_effective()

    # ----- state ----------------------------------------------------------

    @property
    def effective(self) -> EffectiveRuntime:
        return self._effective

    def saved_chat(self) -> ChatSettings:
        return self.store.chat(self.registry, self.settings.cloud.terra_model)

    def saved_speech(self) -> SpeechSettings:
        return self.store.speech(self.registry, self.settings.providers.tts_default)

    def state(self) -> dict[str, object]:
        saved_chat = self.saved_chat()
        saved_speech = self.saved_speech()
        return {
            "credential": self.credentials.state().as_dict(),
            "saved": {"chat": saved_chat.as_dict(), "speech": saved_speech.as_dict()},
            "effective": self._effective.as_dict(),
            "in_sync": {
                "chat": saved_chat == self._effective.chat,
                "speech": saved_speech == self._effective.speech,
            },
            "activation_error": self._activation_error,
            "paid_chat_enabled": self.settings.cloud.enabled
            and self.settings.cloud.allow_paid_calls,
            "paid_tts_enabled": self.settings.providers.allow_paid_tts,
        }

    # ----- chat / speech settings ----------------------------------------

    def apply_chat(self, payload: dict[str, object]) -> dict[str, object]:
        candidate = validate_chat(self.registry, payload)
        self.store.save_chat(candidate)
        return self._activate(chat=candidate)

    def apply_speech(self, payload: dict[str, object]) -> dict[str, object]:
        candidate = validate_speech(self.registry, payload)
        self.store.save_speech(candidate)
        return self._activate(speech=candidate)

    # ----- credential -----------------------------------------------------

    def credential_state(self) -> dict[str, object]:
        return self.credentials.state().as_dict()

    def test_credential(self, *, model: str | None = None) -> dict[str, object]:
        secret = self.credentials.secret()
        if secret is None:
            raise CredentialError(
                "credential_missing", "no OpenAI credential is configured"
            )
        outcome = self.validator.validate(secret, model=model)
        if self.credentials.source() == "butters_store":
            self.credentials.record_validation(outcome)
        return {"validation": outcome.as_dict(), "credential": self.credential_state()}

    def set_credential(self, raw_candidate: object) -> dict[str, object]:
        """Validate the candidate first; replace only a validated credential.

        The previously working credential and the previously effective runtime
        are both preserved if anything fails, including a failure that happens
        after the file has already been written.
        """

        candidate = normalize_candidate(raw_candidate)
        effective_model = self._effective.chat.model
        outcome = self.validator.validate(candidate, model=effective_model)
        if not outcome.authenticated:
            # The old credential file was never touched.
            return {
                "replaced": False,
                "validation": outcome.as_dict(),
                "credential": self.credential_state(),
                "effective": self._effective.as_dict(),
                "activation_error": self._activation_error,
            }
        previous_bytes = self.credentials.snapshot()
        previous_metadata = self.credentials.metadata_snapshot()
        previous_effective = self._effective
        previous_error = self._activation_error
        self.credentials.store(candidate, validation=outcome)
        try:
            self._activate(force=True, raise_on_failure=True)
        except RuntimeActivationError as exc:
            self.credentials.restore(previous_bytes, previous_metadata)
            self._effective = previous_effective
            self._activation_error = previous_error
            # Put the previously working providers back in place.
            self._activate(force=True)
            return {
                "replaced": False,
                "validation": outcome.as_dict(),
                "credential": self.credential_state(),
                "effective": self._effective.as_dict(),
                "activation_error": {"code": exc.code, "message": str(exc)},
            }
        return {
            "replaced": True,
            "validation": outcome.as_dict(),
            "credential": self.credential_state(),
            "effective": self._effective.as_dict(),
            "activation_error": self._activation_error,
        }

    def remove_credential(self) -> dict[str, object]:
        """Remove Butters' own copy. This never revokes anything at OpenAI."""

        removed = self.credentials.remove()
        self._activate(force=True)
        return {
            "removed": removed,
            "credential": self.credential_state(),
            "effective": self._effective.as_dict(),
            "upstream_revoked": False,
            "notice": (
                "This credential may still exist in your OpenAI account. Revoke "
                "it in the OpenAI Platform if it is no longer needed."
            ),
        }

    # ----- activation -----------------------------------------------------

    def _initial_effective(self) -> EffectiveRuntime:
        chat = self.saved_chat()
        speech = self.saved_speech()
        effective = EffectiveRuntime(chat, speech, None, self._clock())
        self._effective = effective
        try:
            self._activate(force=True, raise_on_failure=True)
        except RuntimeActivationError:
            # A missing credential at boot is an ordinary state, not an error
            # that should prevent the service from starting.
            pass
        return self._effective

    def _activate(
        self,
        *,
        chat: ChatSettings | None = None,
        speech: SpeechSettings | None = None,
        force: bool = False,
        raise_on_failure: bool = False,
    ) -> dict[str, object]:
        target_chat = chat or (self.saved_chat() if force else self._effective.chat)
        target_speech = speech or (
            self.saved_speech() if force else self._effective.speech
        )
        secret = None
        try:
            secret = self.credentials.secret()
        except CredentialError as exc:
            self._fail("credential_unreadable", str(exc), raise_on_failure)
            return self.state()
        # A missing credential is a reported credential state, not an
        # activation failure: the runtime is genuinely configured this way and
        # simply cannot reach the paid provider. Activation fails only when the
        # provider objects could not actually be rebuilt and installed.
        try:
            bundle = ProviderBundle(
                self._chat_factory(secret),
                self._speech_factory(secret),
                self._transcription_factory(secret),
                None if secret is None else fingerprint(secret),
            )
            self._install(bundle)
        except (CapabilityError, RuntimeError, OSError, ValueError) as exc:
            self._fail("activation_failed", str(exc), raise_on_failure)
            return self.state()
        self._effective = EffectiveRuntime(
            target_chat,
            target_speech,
            bundle.credential_fingerprint,
            self._clock(),
        )
        self._activation_error = None
        return self.state()

    def _fail(self, code: str, message: str, raise_on_failure: bool) -> None:
        self._activation_error = {"code": code, "message": message}
        if raise_on_failure:
            raise RuntimeActivationError(code, message)
