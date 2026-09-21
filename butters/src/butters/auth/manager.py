"""Standards-based WebAuthn/passkey ceremonies with server-bound state."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from butters.assistant_config import AuthenticationSettings
from butters.auth.store import (
    PORTAL_ROLES,
    AuthStateError,
    AuthStateStore,
    CredentialRecord,
)
from butters.skills.model import AuthenticationContext, AuthenticationLevel


class WebAuthnError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RegistrationVerification:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    device_type: str | None
    backed_up: bool | None


@dataclass(frozen=True, slots=True)
class AuthenticationVerification:
    sign_count: int
    device_type: str | None
    backed_up: bool | None
    user_verified: bool


@dataclass(frozen=True, slots=True)
class AuthenticationOutcome:
    context: AuthenticationContext | None
    purpose: str
    pending_action_id: str | None = None
    fresh_grant: str | None = None
    # Only ever populated for the portal purpose; an administrator ceremony
    # never carries roles, and a portal ceremony never produces an elevation.
    portal_roles: frozenset[str] = frozenset()


class WebAuthnBackend(Protocol):
    def registration_options(
        self,
        *,
        settings: AuthenticationSettings,
        challenge: bytes,
        user_id: bytes,
        identity: str,
        exclude_credentials: tuple[bytes, ...],
    ) -> dict[str, object]: ...

    def verify_registration(
        self,
        credential: dict[str, object],
        *,
        settings: AuthenticationSettings,
        challenge: bytes,
    ) -> RegistrationVerification: ...

    def authentication_options(
        self,
        *,
        settings: AuthenticationSettings,
        challenge: bytes,
        allow_credentials: tuple[bytes, ...],
    ) -> dict[str, object]: ...

    def verify_authentication(
        self,
        credential: dict[str, object],
        *,
        settings: AuthenticationSettings,
        challenge: bytes,
        record: CredentialRecord,
    ) -> AuthenticationVerification: ...


class PyWebAuthnBackend:
    """Thin adapter around maintained py_webauthn; no cryptography lives here."""

    @staticmethod
    def registration_options(
        *,
        settings: AuthenticationSettings,
        challenge: bytes,
        user_id: bytes,
        identity: str,
        exclude_credentials: tuple[bytes, ...],
    ) -> dict[str, object]:
        try:
            from webauthn import generate_registration_options, options_to_json
            from webauthn.helpers.structs import (
                AttestationConveyancePreference,
                AuthenticatorSelectionCriteria,
                PublicKeyCredentialDescriptor,
                ResidentKeyRequirement,
                UserVerificationRequirement,
            )
        except ImportError as exc:
            raise WebAuthnError(
                "webauthn_unavailable", "passkey support is not installed"
            ) from exc
        options = generate_registration_options(
            rp_id=settings.rp_id,
            rp_name=settings.rp_name,
            user_id=user_id,
            user_name=identity,
            user_display_name=identity,
            challenge=challenge,
            timeout=int(settings.challenge_seconds * 1000),
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=item) for item in exclude_credentials
            ],
        )
        return json.loads(options_to_json(options))

    @staticmethod
    def verify_registration(
        credential: dict[str, object],
        *,
        settings: AuthenticationSettings,
        challenge: bytes,
    ) -> RegistrationVerification:
        try:
            from webauthn import verify_registration_response

            result = verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=settings.rp_id,
                expected_origin=settings.origin,
                require_user_verification=True,
            )
        except Exception as exc:
            raise WebAuthnError(
                "registration_denied", "passkey registration was rejected"
            ) from exc
        return RegistrationVerification(
            bytes(result.credential_id),
            bytes(result.credential_public_key),
            int(result.sign_count),
            _enum_value(result.credential_device_type),
            bool(result.credential_backed_up),
        )

    @staticmethod
    def authentication_options(
        *,
        settings: AuthenticationSettings,
        challenge: bytes,
        allow_credentials: tuple[bytes, ...],
    ) -> dict[str, object]:
        try:
            from webauthn import generate_authentication_options, options_to_json
            from webauthn.helpers.structs import (
                PublicKeyCredentialDescriptor,
                UserVerificationRequirement,
            )
        except ImportError as exc:
            raise WebAuthnError(
                "webauthn_unavailable", "passkey support is not installed"
            ) from exc
        options = generate_authentication_options(
            rp_id=settings.rp_id,
            challenge=challenge,
            timeout=int(settings.challenge_seconds * 1000),
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=item) for item in allow_credentials
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return json.loads(options_to_json(options))

    @staticmethod
    def verify_authentication(
        credential: dict[str, object],
        *,
        settings: AuthenticationSettings,
        challenge: bytes,
        record: CredentialRecord,
    ) -> AuthenticationVerification:
        try:
            from webauthn import verify_authentication_response

            result = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=settings.rp_id,
                expected_origin=settings.origin,
                credential_public_key=record.public_key,
                credential_current_sign_count=record.sign_count,
                require_user_verification=True,
            )
        except Exception as exc:
            raise WebAuthnError(
                "authentication_denied", "passkey authentication was rejected"
            ) from exc
        return AuthenticationVerification(
            int(result.new_sign_count),
            _enum_value(result.credential_device_type),
            bool(result.credential_backed_up),
            bool(getattr(result, "user_verified", True)),
        )


class PasskeyManager:
    PURPOSES = frozenset(
        {
            "elevation",
            "pending_action",
            "register_passkey",
            "revoke_passkey",
            # Mutating the stored OpenAI credential is a sensitive
            # administrator action, so it carries its own fresh-grant purpose
            # rather than reusing elevation. The grant is additionally bound to
            # the subject "set" or "remove", so an assertion collected for one
            # cannot authorize the other.
            "openai_credential",
            # The organization Admin key is a second, more dangerous
            # credential, so it carries its own purpose. A grant collected to
            # change the inference key cannot be replayed to change this one.
            "openai_usage_admin_credential",
            # Assigning a portal role is its own sensitive operation. The
            # `nas_power` role carries authority over a destructive physical
            # action, so moving it needs stronger proof than an authenticated
            # administrator session. The name says `update` rather than `grant`
            # because one endpoint both adds and removes roles, and the grant is
            # bound to the target identity as its subject, so an assertion
            # collected to change one person's roles cannot be replayed against
            # another's. It never yields an elevation or a pending action.
            "portal_role_update",
        }
    )
    # The portal's own sign-in purpose. It is deliberately not in PURPOSES: the
    # administrator endpoints validate against PURPOSES, so a portal ceremony
    # cannot be started through them, and this ceremony can never yield an
    # administrator elevation or authorize a pending administrator action.
    PORTAL_PURPOSE = "portal_session"

    def __init__(
        self,
        store: AuthStateStore,
        settings: AuthenticationSettings,
        *,
        backend: WebAuthnBackend | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.backend = backend or PyWebAuthnBackend()

    def begin_registration(
        self,
        *,
        session_id: str,
        identity: str,
        label: str,
        bootstrap_token: str | None = None,
        fresh_grant: str | None = None,
    ) -> dict[str, object]:
        if not self.settings.enabled:
            raise WebAuthnError("webauthn_disabled", "passkey support is disabled")
        if self.store.credential_count(active_only=False) == 0:
            if not bootstrap_token:
                raise WebAuthnError(
                    "bootstrap_required", "local bootstrap authorization is required"
                )
            self._store_call(self.store.consume_bootstrap, bootstrap_token)
            authorization_method = "local_bootstrap"
        else:
            if not fresh_grant:
                raise WebAuthnError(
                    "fresh_required", "fresh passkey authentication is required"
                )
            self._store_call(
                self.store.consume_fresh_grant,
                fresh_grant,
                session_id=session_id,
                identity=identity,
                purpose="register_passkey",
            )
            authorization_method = "fresh_webauthn"
        user_id = secrets.token_bytes(32)
        ceremony = self.store.create_ceremony(
            kind="registration",
            session_id=session_id,
            identity=identity,
            metadata={
                "label": label,
                "user_id": user_id.hex(),
                "authorization_method": authorization_method,
            },
        )
        options = self.backend.registration_options(
            settings=self.settings,
            challenge=ceremony.challenge,
            user_id=user_id,
            identity=identity,
            exclude_credentials=tuple(
                item.credential_id
                for item in self.store.credentials(identity)
                if not item.revoked
            ),
        )
        return {"ceremony_id": ceremony.ceremony_id, "publicKey": options}

    def finish_registration(
        self,
        *,
        ceremony_id: str,
        session_id: str,
        identity: str,
        credential: dict[str, object],
    ) -> dict[str, object]:
        ceremony = self._store_call(
            self.store.consume_ceremony,
            ceremony_id,
            kind="registration",
            session_id=session_id,
            identity=identity,
        )
        verified = self.backend.verify_registration(
            credential, settings=self.settings, challenge=ceremony.challenge
        )
        record = self._store_call(
            self.store.add_credential,
            credential_id=verified.credential_id,
            public_key=verified.public_key,
            user_id=bytes.fromhex(str(ceremony.metadata["user_id"])),
            identity=identity,
            label=str(ceremony.metadata["label"]),
            sign_count=verified.sign_count,
            device_type=verified.device_type,
            backed_up=verified.backed_up,
        )
        roles = frozenset(
            str(item) for item in ceremony.metadata.get("portal_roles", [])
        )
        if roles:
            self._store_call(
                self.store.grant_portal_roles,
                identity,
                str(ceremony.metadata["label"]),
                roles & PORTAL_ROLES,
                maximum=self.settings.max_credentials,
            )
        return {
            **record.safe_dict(),
            "authorization_method": ceremony.metadata["authorization_method"],
            "portal_roles": sorted(roles),
        }

    def begin_authentication(
        self,
        *,
        session_id: str,
        identity: str,
        purpose: str,
        action_digest: str | None = None,
        pending_action_id: str | None = None,
        subject: str | None = None,
        required_level: AuthenticationLevel = AuthenticationLevel.ELEVATED,
    ) -> dict[str, object]:
        if purpose not in self.PURPOSES:
            raise WebAuthnError("purpose_denied", "authentication purpose is invalid")
        credentials = tuple(
            item for item in self.store.credentials(identity) if not item.revoked
        )
        if not credentials:
            raise WebAuthnError("no_passkeys", "no active passkey is registered")
        if purpose == "pending_action" and (not action_digest or not pending_action_id):
            raise WebAuthnError(
                "action_binding_required", "pending action binding is required"
            )
        ceremony = self.store.create_ceremony(
            kind="authentication",
            session_id=session_id,
            identity=identity,
            action_digest=action_digest,
            metadata={
                "purpose": purpose,
                "pending_action_id": pending_action_id,
                "subject": subject,
                "required_level": required_level.value,
            },
        )
        options = self.backend.authentication_options(
            settings=self.settings,
            challenge=ceremony.challenge,
            allow_credentials=tuple(item.credential_id for item in credentials),
        )
        return {"ceremony_id": ceremony.ceremony_id, "publicKey": options}

    def finish_authentication(
        self,
        *,
        ceremony_id: str,
        session_id: str,
        identity: str,
        credential: dict[str, object],
    ) -> AuthenticationOutcome:
        ceremony = self._store_call(
            self.store.consume_ceremony,
            ceremony_id,
            kind="authentication",
            session_id=session_id,
            identity=identity,
        )
        purpose = str(ceremony.metadata.get("purpose"))
        # A portal ceremony must never be completed through the administrator
        # endpoint: it would otherwise fall through to the fresh-grant branch.
        if purpose not in self.PURPOSES:
            raise WebAuthnError("purpose_denied", "authentication purpose is invalid")
        self._verified_record(ceremony, session_id, identity, credential)
        required = AuthenticationLevel(
            str(ceremony.metadata.get("required_level", "elevated"))
        )
        if purpose == "elevation":
            return AuthenticationOutcome(
                self.store.elevate(session_id, identity), purpose
            )
        if purpose == "pending_action":
            if required is AuthenticationLevel.FRESH:
                context = AuthenticationContext(
                    AuthenticationLevel.FRESH,
                    session_id,
                    identity,
                    self.store.clock() + 30,
                    "webauthn",
                    ceremony.action_digest,
                )
            else:
                context = self.store.elevate(session_id, identity)
            return AuthenticationOutcome(
                context,
                purpose,
                str(ceremony.metadata.get("pending_action_id")),
            )
        grant = self.store.create_fresh_grant(
            session_id,
            identity,
            purpose,
            str(ceremony.metadata.get("subject") or "") or None,
        )
        return AuthenticationOutcome(None, purpose, fresh_grant=grant)


    # ----- portal (non-administrator) ceremonies ----------------------------

    def begin_portal_registration(
        self,
        *,
        session_id: str,
        identity: str,
        label: str,
        invite_token: str,
    ) -> dict[str, object]:
        """Register a portal credential against a single-use admin invite.

        There is no unauthenticated path into this method: without a valid,
        unexpired invite minted for exactly this identity, no ceremony starts.
        """

        if not self.settings.enabled:
            raise WebAuthnError("webauthn_disabled", "passkey support is disabled")
        if not invite_token:
            raise WebAuthnError("invite_required", "an enrollment invitation is required")
        roles = self._store_call(
            self.store.consume_portal_invite, invite_token, identity
        )
        user_id = secrets.token_bytes(32)
        ceremony = self.store.create_ceremony(
            kind="registration",
            session_id=session_id,
            identity=identity,
            metadata={
                "label": label,
                "user_id": user_id.hex(),
                "authorization_method": "portal_invite",
                "portal_roles": sorted(roles),
            },
        )
        options = self.backend.registration_options(
            settings=self.settings,
            challenge=ceremony.challenge,
            user_id=user_id,
            identity=identity,
            exclude_credentials=tuple(
                item.credential_id
                for item in self.store.credentials(identity)
                if not item.revoked
            ),
        )
        return {"ceremony_id": ceremony.ceremony_id, "publicKey": options}

    def begin_portal_authentication(
        self, *, session_id: str, identity: str
    ) -> dict[str, object]:
        credentials = tuple(
            item for item in self.store.credentials(identity) if not item.revoked
        )
        if not credentials:
            raise WebAuthnError("no_passkeys", "no active passkey is registered")
        if not self.store.portal_roles(identity):
            raise WebAuthnError("role_denied", "identity holds no portal role")
        ceremony = self.store.create_ceremony(
            kind="authentication",
            session_id=session_id,
            identity=identity,
            metadata={"purpose": self.PORTAL_PURPOSE},
        )
        options = self.backend.authentication_options(
            settings=self.settings,
            challenge=ceremony.challenge,
            allow_credentials=tuple(item.credential_id for item in credentials),
        )
        return {"ceremony_id": ceremony.ceremony_id, "publicKey": options}

    def finish_portal_authentication(
        self,
        *,
        ceremony_id: str,
        session_id: str,
        identity: str,
        credential: dict[str, object],
        ttl_seconds: float,
    ) -> AuthenticationOutcome:
        ceremony = self._store_call(
            self.store.consume_ceremony,
            ceremony_id,
            kind="authentication",
            session_id=session_id,
            identity=identity,
        )
        if str(ceremony.metadata.get("purpose")) != self.PORTAL_PURPOSE:
            raise WebAuthnError("purpose_denied", "authentication purpose is invalid")
        record = self._verified_record(ceremony, session_id, identity, credential)
        assert record is not None
        roles = self._store_call(
            self.store.open_portal_session, session_id, identity, ttl_seconds
        )
        # No AuthenticationContext is produced here at all: a portal sign-in
        # carries no elevation and cannot authorize a frozen action plan.
        return AuthenticationOutcome(None, self.PORTAL_PURPOSE, portal_roles=roles)

    def _verified_record(
        self,
        ceremony: Any,
        session_id: str,
        identity: str,
        credential: dict[str, object],
    ) -> CredentialRecord:
        encoded_id = credential.get("id")
        if not isinstance(encoded_id, str):
            raise WebAuthnError("malformed_credential", "credential ID is required")
        record = self._store_call(self.store.credential_by_encoded_id, encoded_id)
        if record.identity != identity:
            raise WebAuthnError(
                "credential_identity_denied", "credential belongs to another identity"
            )
        verified = self.backend.verify_authentication(
            credential,
            settings=self.settings,
            challenge=ceremony.challenge,
            record=record,
        )
        if not verified.user_verified:
            raise WebAuthnError(
                "user_verification_required", "user verification is required"
            )
        self.store.update_credential_use(
            record.record_id,
            sign_count=verified.sign_count,
            device_type=verified.device_type,
            backed_up=verified.backed_up,
        )
        return record

    def portal_status(self, session_id: str, identity: str) -> dict[str, object]:
        roles = self.store.portal_session_roles(session_id, identity)
        granted = self.store.portal_roles(identity)
        return {
            "authenticated": bool(roles),
            "roles": sorted(roles),
            "enrolled": bool(granted),
            "passkey_count": len(
                [item for item in self.store.credentials(identity) if not item.revoked]
            ),
        }

    def status(self, session_id: str, identity: str) -> dict[str, object]:
        context = self.store.elevation(session_id, identity)
        return {
            "level": context.level.value if context else AuthenticationLevel.NONE.value,
            "elevated": context is not None,
            "expires_at": context.expires_at if context else None,
            "remaining_seconds": max(0, round(context.expires_at - self.store.clock()))
            if context
            else 0,
            "passkey_count": len(
                [item for item in self.store.credentials(identity) if not item.revoked]
            ),
        }

    @staticmethod
    def _store_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except AuthStateError as exc:
            raise WebAuthnError(exc.code, str(exc)) from exc


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return None if raw is None else str(raw)
