"""Separate passkey-authenticated NAS / Jellyfin access portal.

This surface is deliberately small. An authorized holder of the
``jellyfin_access`` role may do exactly four things:

* read NAS / Tailscale / Jellyfin observations,
* send one Wake-on-LAN packet to the one configured NAS,
* poll wake progress,
* be redirected to one of two operator-configured Jellyfin URLs once Jellyfin
  is actually ready.

The independent ``nas_power`` role may additionally prepare exactly one fixed,
zero-argument NAS Agent shutdown plan and complete a FRESH passkey ceremony
bound to its digest. It grants neither Jellyfin access nor administrator status.
There is no Desktop control, administrator tool, caller-selected skill, JSON
executor, broker control, or caller-selected target/host/method in this module.
Administrator authorization remains solely an AuthPolicy decision based on the
tailnet identity and never consults portal roles.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from butters.assistant_config import NasEndpointSettings, PortalSettings
from butters.auth.store import JELLYFIN_ACCESS, NAS_POWER, AuthStateError
from butters.skills.model import AuthenticationContext, AuthenticationLevel
from butters.web.locality import LocalityClassifier, jellyfin_destination
from butters.web.sessions import BrowserSession


class PortalError(PermissionError):
    def __init__(self, code: str, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PortalIdentityView:
    identity: str
    roles: tuple[str, ...]


class PortalService:
    """Role-checked NAS access for non-administrator identities."""

    def __init__(
        self,
        runtime,
        settings: PortalSettings,
        endpoints: NasEndpointSettings,
        classifier: LocalityClassifier,
    ) -> None:
        self.runtime = runtime
        self.settings = settings
        self.endpoints = endpoints
        self.classifier = classifier

    # ----- authorization -----------------------------------------------------

    def require_role(self, session: BrowserSession, role: str = JELLYFIN_ACCESS):
        if not self.settings.enabled:
            raise PortalError("portal_disabled", "the access portal is disabled", 404)
        roles = self.runtime.auth_state.portal_session_roles(
            session.session_id, session.peer_key
        )
        if role not in roles:
            raise PortalError(
                "portal_authentication_required",
                "portal authentication is required",
                401,
            )
        return PortalIdentityView(session.peer_key, tuple(sorted(roles)))

    def status(self, session: BrowserSession) -> dict[str, object]:
        """Unauthenticated-safe status: identity and enrollment state only."""

        if not self.settings.enabled:
            raise PortalError("portal_disabled", "the access portal is disabled", 404)
        return {
            "portal": "ready",
            **self.runtime.passkeys.portal_status(
                session.session_id, session.peer_key
            ),
            "identity": session.peer_key,
        }

    # ----- passkey ceremonies ------------------------------------------------

    def begin_registration(
        self, session: BrowserSession, *, label: str, invite_token: str
    ) -> dict[str, object]:
        """Enrollment is invite-only; an unauthenticated visitor gets nothing."""

        if not self.settings.enabled:
            raise PortalError("portal_disabled", "the access portal is disabled", 404)
        return self.runtime.passkeys.begin_portal_registration(
            session_id=session.session_id,
            identity=session.peer_key,
            label=label,
            invite_token=invite_token,
        )

    def finish_registration(
        self, session: BrowserSession, *, ceremony_id: str, credential: dict
    ) -> dict[str, object]:
        if not self.settings.enabled:
            raise PortalError("portal_disabled", "the access portal is disabled", 404)
        result = self.runtime.passkeys.finish_registration(
            ceremony_id=ceremony_id,
            session_id=session.session_id,
            identity=session.peer_key,
            credential=credential,
        )
        if result.get("authorization_method") != "portal_invite":
            # An administrator registration ceremony must never be completed
            # here, where no administrator identity check has run.
            raise PortalError("ceremony_denied", "registration ceremony is invalid")
        self.runtime.action_state.audit(
            identity=session.peer_key,
            session_id=session.session_id,
            skill="portal.passkey.register",
            authentication=AuthenticationLevel.NONE,
            method="portal_invite",
            arguments={"roles": result.get("portal_roles")},
            outcome="completed",
            job_id=None,
        )
        return result

    def begin_authentication(self, session: BrowserSession) -> dict[str, object]:
        if not self.settings.enabled:
            raise PortalError("portal_disabled", "the access portal is disabled", 404)
        return self.runtime.passkeys.begin_portal_authentication(
            session_id=session.session_id, identity=session.peer_key
        )

    def finish_authentication(
        self, session: BrowserSession, *, ceremony_id: str, credential: dict
    ) -> dict[str, object]:
        if not self.settings.enabled:
            raise PortalError("portal_disabled", "the access portal is disabled", 404)
        outcome = self.runtime.passkeys.finish_portal_authentication(
            ceremony_id=ceremony_id,
            session_id=session.session_id,
            identity=session.peer_key,
            credential=credential,
            ttl_seconds=self.settings.session_ttl_seconds,
        )
        return {
            "verified": True,
            "roles": sorted(outcome.portal_roles),
            # A portal sign-in yields no elevation and no administrator rights.
            "administrator": False,
        }

    def sign_out(self, session: BrowserSession) -> dict[str, object]:
        self.runtime.auth_state.close_portal_session(session.session_id)
        return {"status": "signed_out"}

    # ----- NAS state and wake -------------------------------------------------

    def nas_state(
        self, session: BrowserSession, *, refresh: bool = False
    ) -> dict[str, object]:
        """CURRENT OBSERVED STATE plus the separate LAST OPERATION record."""

        self.require_role(session)
        status = self.runtime.nas_admin_status(refresh=refresh)
        last = status.get("last_operation")
        aggregate = str(status.get("aggregate", "UNKNOWN"))
        elapsed = None
        if isinstance(last, dict) and last.get("operation") == "wake_nas":
            elapsed = max(0.0, time.time() - float(last["at"]))
        ready = aggregate == "READY"
        roles = self.runtime.auth_state.portal_session_roles(
            session.session_id, session.peer_key
        )
        capability = status.get("capability")
        return {
            "observations": status.get("observations", {}),
            "aggregate": aggregate,
            "observed_at": status.get("observed_at"),
            "headline": _headline(aggregate),
            "detail": _detail(aggregate),
            "last_operation": _portal_last_operation(last),
            "wake_elapsed_seconds": elapsed,
            "jellyfin_ready": ready,
            # Bounded polling. The client is told when to stop rather than
            # being trusted to stop, and a timeout never re-sends WOL.
            "max_poll_seconds": self.settings.max_poll_seconds,
            "poll_expired": (
                elapsed is not None
                and elapsed > self.settings.max_poll_seconds
                and not ready
            ),
            # Wake Again is offered only when it is actually appropriate:
            # when nothing has been requested yet, or when the bounded poll
            # window has expired. While a wake is in flight the button is
            # withheld, so the UI cannot be nudged into a packet storm.
            "can_wake": _can_wake(
                aggregate, elapsed, self.settings.max_poll_seconds
            ),
            # The UI renders its fixed shutdown ceremony only when both this
            # independent role and the two server-side capability gates agree.
            "can_shutdown": (
                NAS_POWER in roles
                and isinstance(capability, dict)
                and capability.get("shutdown_configured") is True
            ),
            "power_state": status.get("power_state", "unknown"),
            "lifecycle": status.get("lifecycle", "UNKNOWN"),
        }

    def wake(self, session: BrowserSession) -> dict[str, object]:
        """Send exactly one packet. Never claim the NAS booted.

        The portal's caller is not an administrator, so this does not go through
        the administrator action path. It names the same registered `wake_nas`
        action, with the same empty argument object, executed by the coordinator
        under a server-minted role authorization -- the role is the authority,
        and it authorizes this one action and nothing else.
        """

        identity = self.require_role(session)
        if self.runtime.assistant.nas_adapter is None:
            raise PortalError("capability_unavailable", "NAS wake is not configured")
        plan = self.runtime.actions.freeze(
            skill="wake_nas",
            arguments={},
            summary="Wake the configured NAS",
            session_id=session.session_id,
            identity=session.peer_key,
            request_id="portal-" + _request_suffix(),
            source="jellyfin_portal",
        )
        # Minted here and never written anywhere. Persisting it in
        # `browser_elevations` would make the portal's role look like an
        # administrator elevation to every other reader of that table, so this
        # context exists only for the length of this one coordinator call and
        # is bound to this session, this identity, and a 60-second expiry.
        context = AuthenticationContext(
            AuthenticationLevel.ELEVATED,
            session.session_id,
            session.peer_key,
            time.time() + 60,
            "portal_role",
        )
        jobs = self.runtime.actions.execute(
            plan.plan_id,
            session_id=session.session_id,
            identity=session.peer_key,
            authentication=context,
        )
        record = self.runtime._record_operation(
            "nas", "wake_nas", "wake_packet_sent", f"portal:{identity.identity}"
        )
        return {
            # Deliberate wording: this reports what this host did, not what the
            # NAS did. Boot is decided only by the status observations.
            "status": "wake_packet_sent",
            "message": "Wake packet sent",
            "jobs": list(jobs),
            "last_operation": _portal_last_operation(record),
        }

    # ----- dormant NAS power flow -------------------------------------------

    def prepare_shutdown(
        self, session: BrowserSession, *, confirmed: bool
    ) -> dict[str, object]:
        """Freeze the only portal power plan; this never executes it."""

        self.require_role(session, NAS_POWER)
        if confirmed is not True:
            raise PortalError(
                "confirmation_required", "NAS shutdown requires explicit confirmation"
            )
        status = self.runtime.nas_admin_status()
        capability = status.get("capability")
        if not isinstance(capability, dict) or capability.get("shutdown_configured") is not True:
            raise PortalError(
                "capability_unavailable", "NAS Agent shutdown is disabled or unconfigured"
            )
        plan = self.runtime.actions.freeze(
            skill="nas.system.shutdown",
            arguments={},
            summary="Shut down the configured NAS",
            session_id=session.session_id,
            identity=session.peer_key,
            request_id="portal-power-" + _request_suffix(),
            source="nas_power_portal",
            pending_confirmation=True,
        )
        return {
            "status": "shutdown_auth_required",
            "authentication_required": AuthenticationLevel.FRESH.value,
            "pending_action": plan.safe_dict(),
        }

    def begin_shutdown_authentication(
        self, session: BrowserSession, *, pending_action_id: str
    ) -> dict[str, object]:
        self.require_role(session, NAS_POWER)
        plan = self._shutdown_plan(session, pending_action_id)
        return self.runtime.passkeys.begin_authentication(
            session_id=session.session_id,
            identity=session.peer_key,
            purpose="pending_action",
            action_digest=plan.digest,
            pending_action_id=plan.plan_id,
            subject="nas",
            required_level=AuthenticationLevel.FRESH,
        )

    def finish_shutdown_authentication(
        self,
        session: BrowserSession,
        *,
        ceremony_id: str,
        credential: dict[str, object],
    ) -> dict[str, object]:
        self.require_role(session, NAS_POWER)
        outcome = self.runtime.passkeys.finish_authentication(
            ceremony_id=ceremony_id,
            session_id=session.session_id,
            identity=session.peer_key,
            credential=credential,
        )
        if outcome.pending_action_id is None or outcome.context is None:
            raise PortalError("fresh_authentication_required", "fresh authentication is required")
        self._shutdown_plan(session, outcome.pending_action_id)
        jobs = self.runtime.actions.execute(
            outcome.pending_action_id,
            session_id=session.session_id,
            identity=session.peer_key,
            authentication=outcome.context,
        )
        self.runtime._record_operation(
            "nas", "nas.system.shutdown", "shutdown_queued", "portal:nas_power"
        )
        return {"verified": True, "status": "shutdown_queued", "jobs": list(jobs)}

    def _shutdown_plan(self, session: BrowserSession, plan_id: str):
        try:
            plan = self.runtime.action_state.require(
                plan_id,
                session_id=session.session_id,
                identity=session.peer_key,
                allowed_states=frozenset({"pending_confirmation", "pending_auth"}),
            )
        except Exception as exc:
            raise PortalError("pending_action_denied", "pending action is unavailable") from exc
        if (
            len(plan.steps) != 1
            or plan.steps[0].skill != "nas.system.shutdown"
            or plan.steps[0].arguments != {}
            or plan.authentication is not AuthenticationLevel.FRESH
        ):
            raise PortalError("pending_action_denied", "pending action is unavailable")
        return plan

    # ----- redirect -----------------------------------------------------------

    def destination(
        self, session: BrowserSession, headers: object, client_host: str | None
    ) -> dict[str, object]:
        """Resolve the redirect server-side, or refuse to redirect at all.

        Two rules hold unconditionally: nothing is returned unless Jellyfin's
        own readiness probe currently passes, and the URL is always one of the
        two configured destinations. No request value participates.
        """

        self.require_role(session)
        status = self.runtime.nas_admin_status()
        if str(status.get("aggregate")) != "READY":
            return {
                "ready": False,
                "aggregate": status.get("aggregate"),
                "destination": None,
            }
        decision = self.classifier.classify(headers, client_host)
        url = jellyfin_destination(decision, self.endpoints)
        if not url:
            return {
                "ready": False,
                "aggregate": "READY",
                "destination": None,
                "reason": "no_destination_configured",
            }
        return {
            "ready": True,
            "aggregate": "READY",
            "destination": url,
            "locality": decision.locality.value,
            "locality_source": decision.source,
        }

    # ----- administrator-side enrollment --------------------------------------

    def create_invite(self, identity: str, label: str) -> dict[str, object]:
        token, expires = _store_call(
            self.runtime.auth_state.create_portal_invite,
            identity,
            label,
            frozenset({JELLYFIN_ACCESS}),
            self.settings.invite_ttl_seconds,
        )
        return {
            "identity": identity,
            "label": label,
            "roles": [JELLYFIN_ACCESS],
            "invite_token": token,
            "expires_at": expires,
        }

    def revoke_identity(self, identity: str) -> dict[str, object]:
        _store_call(self.runtime.auth_state.revoke_portal_identity, identity)
        return {"status": "revoked", "identity": identity}

    def identities(self) -> dict[str, object]:
        return {
            "identities": [
                item.safe_dict()
                for item in self.runtime.auth_state.portal_identities()
            ],
            "pending_invites": list(self.runtime.auth_state.portal_invites()),
        }


def _can_wake(aggregate: str, elapsed: float | None, maximum: float) -> bool:
    if aggregate == "READY":
        return False
    if elapsed is None:
        return aggregate in {"OFFLINE", "UNKNOWN"}
    return elapsed > maximum


_HEADLINES = {
    "OFFLINE": ("NAS OFFLINE", "Jellyfin Unavailable"),
    "WAKING": ("Wake packet sent", "Waiting for NAS…"),
    "NAS_REACHABLE": ("NAS reachable", "Waiting for services…"),
    "TAILSCALE_REACHABLE": ("Tailscale reachable", "Waiting for services…"),
    "JELLYFIN_STARTING": ("NAS reachable", "Jellyfin starting…"),
    "READY": ("NAS Online", "Jellyfin Ready"),
    "UNKNOWN": ("NAS state unknown", "Jellyfin state unknown"),
}


def _headline(aggregate: str) -> str:
    return _HEADLINES.get(aggregate, _HEADLINES["UNKNOWN"])[0]


def _detail(aggregate: str) -> str:
    return _HEADLINES.get(aggregate, _HEADLINES["UNKNOWN"])[1]


def _portal_last_operation(record: object) -> dict[str, object] | None:
    """Expose only the operation, outcome, and age -- never internal detail."""

    if not isinstance(record, dict):
        return None
    at = float(record.get("at", 0.0))
    return {
        "operation": record.get("operation"),
        "outcome": record.get("outcome"),
        "at": at,
        "age_seconds": max(0.0, time.time() - at),
    }


def _request_suffix() -> str:
    return secrets.token_urlsafe(12)


def _store_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except AuthStateError as exc:
        raise PortalError(exc.code, str(exc)) from exc
