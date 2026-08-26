from __future__ import annotations

import base64
import time
from dataclasses import dataclass

import pytest
from butters.actions.coordinator import ActionCoordinator, ActionCoordinatorError
from butters.actions.store import ActionStateError, ActionStateStore
from butters.assistant_config import ActionSettings, AuthenticationSettings
from butters.auth.manager import (
    AuthenticationVerification,
    PasskeyManager,
    RegistrationVerification,
    WebAuthnError,
)
from butters.auth.store import AuthStateError, AuthStateStore
from butters.skills.model import (
    ActionClass,
    AuthenticationContext,
    AuthenticationLevel,
    NoArguments,
    SkillError,
    StructuredSkillResult,
)
from butters.skills.policy import PolicyValidator, allow_arguments
from butters.skills.registry import SkillRegistry, SkillSpec, strict_arguments


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


class FakeWebAuthn:
    def registration_options(self, **kwargs):
        return {"challenge": "test", "rp": {"id": kwargs["settings"].rp_id}}

    def verify_registration(self, credential, *, settings, challenge):
        if credential.get("challenge", challenge) != challenge:
            raise WebAuthnError("registration_denied", "wrong challenge")
        if credential.get("origin") != settings.origin:
            raise WebAuthnError("registration_denied", "wrong origin")
        if credential.get("rp_id") != settings.rp_id:
            raise WebAuthnError("registration_denied", "wrong RP")
        if credential.get("uv") is not True:
            raise WebAuthnError("user_verification_required", "UV required")
        return RegistrationVerification(
            credential.get("credential_id", b"credential-one"),
            b"public",
            0,
            "multi_device",
            True,
        )

    def authentication_options(self, **kwargs):
        return {"challenge": "test", "rpId": kwargs["settings"].rp_id}

    def verify_authentication(self, credential, *, settings, challenge, record):
        del record
        if credential.get("challenge", challenge) != challenge:
            raise WebAuthnError("authentication_denied", "wrong challenge")
        if (
            credential.get("origin") != settings.origin
            or credential.get("rp_id") != settings.rp_id
        ):
            raise WebAuthnError("authentication_denied", "trust mismatch")
        return AuthenticationVerification(
            0, "multi_device", True, credential.get("uv") is True
        )


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _credential(
    settings: AuthenticationSettings, *, uv: bool = True
) -> dict[str, object]:
    return {
        "id": _encoded(b"credential-one"),
        "origin": settings.origin,
        "rp_id": settings.rp_id,
        "uv": uv,
    }


def _registered_manager(tmp_path):
    clock = Clock()
    settings = AuthenticationSettings().validated()
    store = AuthStateStore(tmp_path / "security.sqlite3", settings, clock=clock)
    manager = PasskeyManager(store, settings, backend=FakeWebAuthn())
    token, _expires = store.create_bootstrap()
    begin = manager.begin_registration(
        session_id="session-a",
        identity="identity:a",
        label="Phone",
        bootstrap_token=token,
    )
    manager.finish_registration(
        ceremony_id=begin["ceremony_id"],
        session_id="session-a",
        identity="identity:a",
        credential=_credential(settings),
    )
    return clock, settings, store, manager


def test_bootstrap_registration_is_single_use_and_local_recovery_restores_bootstrap(
    tmp_path,
) -> None:
    clock, settings, store, manager = _registered_manager(tmp_path)
    assert store.credential_count() == 1
    with pytest.raises(AuthStateError, match="no longer available"):
        store.create_bootstrap()
    store.local_recovery_revoke_all()
    token, expires = store.create_bootstrap()
    assert token and expires == clock.now + settings.bootstrap_seconds
    store.consume_bootstrap(token)
    with pytest.raises(AuthStateError) as replay:
        store.consume_bootstrap(token)
    assert replay.value.code == "bootstrap_denied"
    del manager


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("origin", "https://wrong.example", "registration_denied"),
        ("rp_id", "wrong.example", "registration_denied"),
        ("uv", False, "user_verification_required"),
    ),
)
def test_registration_rejects_wrong_trust_or_missing_uv(
    tmp_path, field, value, code
) -> None:
    settings = AuthenticationSettings().validated()
    store = AuthStateStore(tmp_path / "security.sqlite3", settings)
    manager = PasskeyManager(store, settings, backend=FakeWebAuthn())
    token, _ = store.create_bootstrap()
    begin = manager.begin_registration(
        session_id="session-a",
        identity="identity:a",
        label="Phone",
        bootstrap_token=token,
    )
    credential = _credential(settings)
    credential[field] = value
    with pytest.raises(WebAuthnError) as denied:
        manager.finish_registration(
            ceremony_id=begin["ceremony_id"],
            session_id="session-a",
            identity="identity:a",
            credential=credential,
        )
    assert denied.value.code == code


def test_ceremony_session_identity_expiry_replay_and_elevation_isolation(
    tmp_path,
) -> None:
    clock, settings, _store, manager = _registered_manager(tmp_path)
    begin = manager.begin_authentication(
        session_id="session-a", identity="identity:a", purpose="elevation"
    )
    with pytest.raises(WebAuthnError) as wrong_session:
        manager.finish_authentication(
            ceremony_id=begin["ceremony_id"],
            session_id="session-b",
            identity="identity:a",
            credential=_credential(settings),
        )
    assert wrong_session.value.code == "ceremony_session_denied"
    outcome = manager.finish_authentication(
        ceremony_id=begin["ceremony_id"],
        session_id="session-a",
        identity="identity:a",
        credential=_credential(settings),
    )
    assert outcome.context and outcome.context.level is AuthenticationLevel.ELEVATED
    expiry = outcome.context.expires_at
    clock.now += 30
    assert manager.status("session-a", "identity:a")["expires_at"] == expiry
    assert manager.status("session-b", "identity:a")["elevated"] is False
    assert manager.status("session-a", "identity:b")["elevated"] is False
    with pytest.raises(WebAuthnError) as replay:
        manager.finish_authentication(
            ceremony_id=begin["ceremony_id"],
            session_id="session-a",
            identity="identity:a",
            credential=_credential(settings),
        )
    assert replay.value.code == "ceremony_replayed"
    expired = manager.begin_authentication(
        session_id="session-a", identity="identity:a", purpose="elevation"
    )
    clock.now += settings.challenge_seconds + 1
    with pytest.raises(WebAuthnError) as timeout:
        manager.finish_authentication(
            ceremony_id=expired["ceremony_id"],
            session_id="session-a",
            identity="identity:a",
            credential=_credential(settings),
        )
    assert timeout.value.code == "ceremony_expired"


def test_revoked_credential_and_missing_uv_are_denied(tmp_path) -> None:
    _clock, settings, store, manager = _registered_manager(tmp_path)
    begin = manager.begin_authentication(
        session_id="session-a", identity="identity:a", purpose="elevation"
    )
    with pytest.raises(WebAuthnError) as no_uv:
        manager.finish_authentication(
            ceremony_id=begin["ceremony_id"],
            session_id="session-a",
            identity="identity:a",
            credential=_credential(settings, uv=False),
        )
    assert no_uv.value.code == "user_verification_required"
    record = store.credentials("identity:a")[0]
    store.revoke_credential(record.record_id, "identity:a")
    begin = (
        manager.begin_authentication(
            session_id="session-a", identity="identity:a", purpose="elevation"
        )
        if store.credential_count()
        else None
    )
    assert begin is None


def test_wrong_webauthn_challenge_is_denied_and_consumed(tmp_path) -> None:
    _clock, settings, _store, manager = _registered_manager(tmp_path)
    begin = manager.begin_authentication(
        session_id="session-a", identity="identity:a", purpose="elevation"
    )
    credential = _credential(settings)
    credential["challenge"] = b"wrong"
    with pytest.raises(WebAuthnError) as denied:
        manager.finish_authentication(
            ceremony_id=begin["ceremony_id"],
            session_id="session-a",
            identity="identity:a",
            credential=credential,
        )
    assert denied.value.code == "authentication_denied"
    with pytest.raises(WebAuthnError) as replay:
        manager.finish_authentication(
            ceremony_id=begin["ceremony_id"],
            session_id="session-a",
            identity="identity:a",
            credential=_credential(settings),
        )
    assert replay.value.code == "ceremony_replayed"


def test_additional_passkey_and_revocation_grants_require_fresh_assertions(
    tmp_path,
) -> None:
    _clock, settings, store, manager = _registered_manager(tmp_path)
    add = manager.begin_authentication(
        session_id="session-a",
        identity="identity:a",
        purpose="register_passkey",
    )
    grant = manager.finish_authentication(
        ceremony_id=add["ceremony_id"],
        session_id="session-a",
        identity="identity:a",
        credential=_credential(settings),
    ).fresh_grant
    registration = manager.begin_registration(
        session_id="session-a",
        identity="identity:a",
        label="Laptop",
        fresh_grant=grant,
    )
    second = _credential(settings)
    second["credential_id"] = b"credential-two"
    manager.finish_registration(
        ceremony_id=registration["ceremony_id"],
        session_id="session-a",
        identity="identity:a",
        credential=second,
    )
    assert store.credential_count() == 2
    record = store.credentials("identity:a")[0]
    revoke = manager.begin_authentication(
        session_id="session-a",
        identity="identity:a",
        purpose="revoke_passkey",
        subject=record.record_id,
    )
    revoke_grant = manager.finish_authentication(
        ceremony_id=revoke["ceremony_id"],
        session_id="session-a",
        identity="identity:a",
        credential=_credential(settings),
    ).fresh_grant
    subject = store.consume_fresh_grant(
        revoke_grant,
        session_id="session-a",
        identity="identity:a",
        purpose="revoke_passkey",
    )
    assert subject == record.record_id
    with pytest.raises(AuthStateError):
        store.consume_fresh_grant(
            revoke_grant,
            session_id="session-a",
            identity="identity:a",
            purpose="revoke_passkey",
        )


@dataclass
class Calls:
    values: list[str]


def _action_registry(calls: Calls) -> SkillRegistry:
    registry = SkillRegistry(
        PolicyValidator(allowed_actions=frozenset({ActionClass.ACTION}))
    )

    def parser(values):
        strict_arguments(values)
        return NoArguments()

    def register(name, level, *, local=False):
        registry.register(
            SkillSpec(
                name,
                name,
                ActionClass.ACTION,
                parser,
                allow_arguments,
                lambda _arguments, selected=name: (
                    calls.values.append(selected)
                    or StructuredSkillResult("action", {"name": selected})
                ),
                2,
                version="2.1.0",
                explicit_intent_required=True,
                confirmation_required=True,
                authentication=level,
                local_console_allowed=local,
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            )
        )

    register("elevated_action", AuthenticationLevel.ELEVATED, local=True)
    register("fresh_action", AuthenticationLevel.FRESH)
    return registry


def _wait_job(store, job_id, session="session-a", identity="identity:a"):
    deadline = time.time() + 2
    while time.time() < deadline:
        job = store.job(job_id, session_id=session, identity=identity)
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_pending_plan_is_immutable_owned_single_use_and_fresh_bound(tmp_path) -> None:
    calls = Calls([])
    store = ActionStateStore(tmp_path / "actions.sqlite3", ActionSettings())
    coordinator = ActionCoordinator(_action_registry(calls), store)
    plan = coordinator.freeze(
        skill="fresh_action",
        arguments={},
        summary="Fresh action",
        session_id="session-a",
        identity="identity:a",
        request_id="request-a",
        source="browser",
    )
    with pytest.raises(ActionStateError):
        store.require(plan.plan_id, session_id="session-b", identity="identity:a")
    elevated = AuthenticationContext(
        AuthenticationLevel.ELEVATED,
        "session-a",
        "identity:a",
        time.time() + 60,
        "webauthn",
    )
    with pytest.raises(ActionCoordinatorError) as insufficient:
        coordinator.execute(
            plan.plan_id,
            session_id="session-a",
            identity="identity:a",
            authentication=elevated,
        )
    assert insufficient.value.code == "fresh_authentication_required"
    wrong_digest = AuthenticationContext(
        AuthenticationLevel.FRESH,
        "session-a",
        "identity:a",
        time.time() + 60,
        "webauthn",
        "different",
    )
    with pytest.raises(ActionCoordinatorError):
        coordinator.execute(
            plan.plan_id,
            session_id="session-a",
            identity="identity:a",
            authentication=wrong_digest,
        )
    fresh = AuthenticationContext(
        AuthenticationLevel.FRESH,
        "session-a",
        "identity:a",
        time.time() + 60,
        "webauthn",
        plan.digest,
    )
    job = coordinator.execute(
        plan.plan_id,
        session_id="session-a",
        identity="identity:a",
        authentication=fresh,
    )[0]
    assert _wait_job(store, job["job_id"])["state"] == "completed"
    with pytest.raises(ActionCoordinatorError) as replay:
        coordinator.execute(
            plan.plan_id,
            session_id="session-a",
            identity="identity:a",
            authentication=fresh,
        )
    assert replay.value.code == "pending_action_replayed"
    assert calls.values == ["fresh_action"]
    audit = store.audit_entries()[0]
    assert audit["identity_ref"] != "identity:a"
    assert audit["session_ref"] != "session-a"
    assert audit["authentication"] == "fresh"
    assert "public" not in str(audit).casefold()


def test_frozen_multi_action_plan_cannot_append_or_change_steps(tmp_path) -> None:
    calls = Calls([])
    store = ActionStateStore(tmp_path / "actions.sqlite3", ActionSettings())
    coordinator = ActionCoordinator(_action_registry(calls), store)
    plan = coordinator.freeze_plan(
        steps=(("elevated_action", {}), ("fresh_action", {})),
        summary="Two exact actions",
        session_id="session-a",
        identity="identity:a",
        request_id="request-a",
        source="browser",
    )
    assert [step.skill for step in plan.steps] == ["elevated_action", "fresh_action"]
    fresh = AuthenticationContext(
        AuthenticationLevel.FRESH,
        "session-a",
        "identity:a",
        time.time() + 60,
        "webauthn",
        plan.digest,
    )
    job = coordinator.execute(
        plan.plan_id,
        session_id="session-a",
        identity="identity:a",
        authentication=fresh,
    )[0]
    assert _wait_job(store, job["job_id"])["state"] == "completed"
    assert calls.values == ["elevated_action", "fresh_action"]


def test_pending_action_expiry_cancel_and_job_cancellation(tmp_path) -> None:
    clock = Clock()
    calls = Calls([])
    registry = _action_registry(calls)

    def parser(values):
        strict_arguments(values)
        return NoArguments()

    def slow(_arguments):
        from butters.skills.registry import current_cancel_event

        event = current_cancel_event()
        assert event is not None
        event.wait(2)
        if event.is_set():
            raise SkillError("cancelled", "cancelled")
        return StructuredSkillResult("action", {})

    registry.register(
        SkillSpec(
            "slow_action",
            "slow",
            ActionClass.ACTION,
            parser,
            allow_arguments,
            slow,
            3,
            version="2.1.0",
            explicit_intent_required=True,
            confirmation_required=True,
            authentication=AuthenticationLevel.ELEVATED,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
    )
    store = ActionStateStore(
        tmp_path / "actions.sqlite3",
        ActionSettings(),
        pending_seconds=30,
        clock=clock,
    )
    coordinator = ActionCoordinator(registry, store)
    expired = coordinator.freeze(
        skill="elevated_action",
        arguments={},
        summary="expires",
        session_id="session-a",
        identity="identity:a",
        request_id="request-a",
        source="browser",
    )
    clock.now += 31
    with pytest.raises(ActionStateError) as timeout:
        store.require(expired.plan_id, session_id="session-a", identity="identity:a")
    assert timeout.value.code == "pending_action_expired"
    cancelled = coordinator.freeze(
        skill="elevated_action",
        arguments={},
        summary="cancelled",
        session_id="session-a",
        identity="identity:a",
        request_id="request-b",
        source="browser",
    )
    coordinator.cancel_pending(
        cancelled.plan_id, session_id="session-a", identity="identity:a"
    )
    with pytest.raises(ActionStateError):
        store.require(cancelled.plan_id, session_id="session-a", identity="identity:a")
    running = coordinator.freeze(
        skill="slow_action",
        arguments={},
        summary="slow",
        session_id="session-a",
        identity="identity:a",
        request_id="request-c",
        source="browser",
    )
    authentication = AuthenticationContext(
        AuthenticationLevel.ELEVATED,
        "session-a",
        "identity:a",
        time.time() + 60,
        "webauthn",
    )
    job = coordinator.execute(
        running.plan_id,
        session_id="session-a",
        identity="identity:a",
        authentication=authentication,
    )[0]
    coordinator.cancel_job(job["job_id"], session_id="session-a", identity="identity:a")
    assert _wait_job(store, job["job_id"])["state"] == "cancelled"
