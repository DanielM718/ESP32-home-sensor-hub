"""Assigning `nas_power`: the one administrator path, and its limits.

The defect these tests close was not a wrong check. Every backend check was
right, and the Portal was correctly hiding the shutdown control because the
owner genuinely did not hold `nas_power`. The defect was that *nothing could
grant it*: the only writer of a portal role ran at enrollment, from the roles an
invitation carried, and `PortalService.create_invite` hard-coded
`{jellyfin_access}`. A fully built, production-enabled role was unreachable.

So the properties worth proving are about the new path's edges rather than its
happy case: that it cannot enroll anybody, cannot reinstate a revoked identity,
cannot be driven without a fresh assertion bound to the named person, cannot
invent a role, and cannot be reached from the browser at all -- while the
partner keeps exactly the access they had and Wake stays where it was.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
from pathlib import Path

import httpx
from butters.auth.store import JELLYFIN_ACCESS, NAS_POWER, PORTAL_ROLES
from butters.web.portal import PortalService
from test_jellyfin_portal import (
    ADMIN,
    PARTNER,
    _application,
    _credential,
    _enroll,
    _mutation,
    _sign_in,
)

OWNER = "owner@example.com"
OWNER_CREDENTIAL = b"owner-credential"
ADMIN_CREDENTIAL = b"admin-credential"
ROLES_PATH = "/api/admin/portal/roles"


def _admin_passkey(service, credential_id=ADMIN_CREDENTIAL) -> None:
    """Give the administrator a passkey, without any portal role.

    The administrator authenticates as themselves to authorize a role change.
    They deliberately hold no portal role while doing it: the authority to grant
    `nas_power` must not require holding it.
    """

    service.auth_state.add_credential(
        credential_id=credential_id,
        public_key=b"public",
        user_id=b"admin-user",
        identity=f"identity:{ADMIN}",
        label="Administrator passkey",
        sign_count=0,
        device_type="multi_device",
        backed_up=True,
    )


async def _fresh_grant(http, headers, subject, credential_id=ADMIN_CREDENTIAL):
    """Collect a FRESH assertion bound to one portal identity."""

    begin = await http.post(
        "/api/auth/authenticate/options",
        headers=headers,
        json={"purpose": "portal_role_update", "subject": subject},
    )
    assert begin.status_code == 200, begin.text
    verified = await http.post(
        "/api/auth/authenticate/verify",
        headers=headers,
        json={
            "ceremony_id": begin.json()["ceremony_id"],
            "credential": _credential(credential_id),
        },
    )
    assert verified.status_code == 200, verified.text
    grant = verified.json()["fresh_grant"]
    assert isinstance(grant, str) and grant
    return grant


def _roles_of(service, identity) -> list[str]:
    """Read the durable record, not a response projection."""

    record = service.auth_state.portal_identity(f"identity:{identity}")
    return [] if record is None else sorted(record.roles)


# ---------------------------------------------------------------------------
# 1. The owner gains the role, and the Portal then offers shutdown.
# ---------------------------------------------------------------------------


async def _owner_granted_nas_power_sees_shutdown(tmp_path) -> None:
    app, service, nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({JELLYFIN_ACCESS}),
        )
        _admin_passkey(service)
        assert _roles_of(service, OWNER) == [JELLYFIN_ACCESS]

        # Before the grant the Portal withholds the control, because the
        # backend says so -- not because the stylesheet hides it.
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as owner_http:
            owner = await _mutation(owner_http, OWNER)
            await _sign_in(owner_http, owner, credential_id=OWNER_CREDENTIAL)
            before = await owner_http.get("/api/portal/nas", headers=owner)
            assert before.status_code == 200, before.text
            assert before.json()["can_shutdown"] is False

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            headers = await _mutation(admin_http, ADMIN)
            grant = await _fresh_grant(admin_http, headers, f"identity:{OWNER}")
            response = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "fresh_grant": grant,
                    "confirm": True,
                },
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "roles_updated"
            assert body["previous_roles"] == [JELLYFIN_ACCESS]
            assert body["roles"] == sorted([JELLYFIN_ACCESS, NAS_POWER])

        # The durable record is what changed.
        assert _roles_of(service, OWNER) == sorted([JELLYFIN_ACCESS, NAS_POWER])

        # A role is read from the current grant at sign-in, so the owner's next
        # portal session carries it and the control appears.
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as owner_http:
            owner = await _mutation(owner_http, OWNER)
            signed_in = await _sign_in(
                owner_http, owner, credential_id=OWNER_CREDENTIAL
            )
            assert sorted(signed_in.json()["value"]["roles"]) == sorted(
                [JELLYFIN_ACCESS, NAS_POWER]
            )
            after = await owner_http.get("/api/portal/nas", headers=owner)
            assert after.json()["can_shutdown"] is True

        # Granting authority is not exercising it.
        assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


# ---------------------------------------------------------------------------
# 2 & 3. Holding the role is not the same as being allowed to shut down.
# ---------------------------------------------------------------------------


async def _shutdown_still_requires_its_own_fresh_ceremony(tmp_path) -> None:
    app, service, nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({JELLYFIN_ACCESS, NAS_POWER}),
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as owner_http:
            owner = await _mutation(owner_http, OWNER)
            await _sign_in(owner_http, owner, credential_id=OWNER_CREDENTIAL)

            # Preparing the plan is as far as the role alone reaches, and the
            # answer names the requirement rather than performing anything.
            planned = await owner_http.post(
                "/api/portal/shutdown/plan",
                headers=owner,
                json={"confirm": True},
            )
            assert planned.status_code == 200, planned.text
            plan = planned.json()
            assert plan["status"] == "shutdown_auth_required"
            assert plan["authentication_required"] == "fresh"
            pending = plan["pending_action"]["pending_action_id"]

            # The ceremony is bound to this frozen plan's digest.
            begin = await owner_http.post(
                "/api/portal/shutdown/authenticate/options",
                headers=owner,
                json={"pending_action_id": pending},
            )
            assert begin.status_code == 200, begin.text

            # An assertion that fails user verification authorizes nothing.
            refused = await owner_http.post(
                "/api/portal/shutdown/authenticate/verify",
                headers=owner,
                json={
                    "ceremony_id": begin.json()["value"]["ceremony_id"],
                    "credential": {
                        "id": base64.urlsafe_b64encode(OWNER_CREDENTIAL)
                        .rstrip(b"=")
                        .decode(),
                        "uv": False,
                    },
                },
            )
            assert refused.status_code in {401, 403}

        # FakeNas.shutdown raises if reached; nothing reached it.
        assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


# ---------------------------------------------------------------------------
# 4. The partner is untouched.
# ---------------------------------------------------------------------------


async def _partner_keeps_jellyfin_only(tmp_path) -> None:
    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(service)
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({JELLYFIN_ACCESS, NAS_POWER}),
        )
        # Granting the owner power says nothing about the partner.
        assert _roles_of(service, PARTNER) == [JELLYFIN_ACCESS]

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as partner_http:
            partner = await _mutation(partner_http, PARTNER)
            await _sign_in(partner_http, partner)

            status = await partner_http.get("/api/portal/nas", headers=partner)
            assert status.status_code == 200, status.text
            # The control is withheld, and the reason is the backend's.
            assert status.json()["can_shutdown"] is False

            denied = await partner_http.post(
                "/api/portal/shutdown/plan",
                headers=partner,
                json={"confirm": True},
            )
            assert denied.status_code == 401
            assert denied.json()["error"] == "portal_authentication_required"
    finally:
        await app.state.shutdown_workers()
        del service


# ---------------------------------------------------------------------------
# 5. The browser is not the authority.
# ---------------------------------------------------------------------------


async def _client_claims_cannot_create_authority(tmp_path) -> None:
    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(service)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as partner_http:
            partner = await _mutation(partner_http, PARTNER)
            await _sign_in(partner_http, partner)

            # Asking for the role in the sign-in body does not confer it, and
            # the response reports the granted role rather than the requested.
            reopened = await partner_http.post(
                "/api/portal/authenticate/options",
                headers=partner,
                json={"roles": [NAS_POWER], "can_shutdown": True},
            )
            assert reopened.status_code in {200, 400}

            # The partner cannot reach the administrator role endpoint at all,
            # whatever they send, and no role appears from trying.
            for payload in (
                {"identity": f"identity:{PARTNER}", "roles": [NAS_POWER]},
                {
                    "identity": f"identity:{PARTNER}",
                    "roles": [NAS_POWER],
                    "confirm": True,
                    "fresh_grant": "forged",
                },
            ):
                attempt = await partner_http.post(
                    ROLES_PATH, headers=partner, json=payload
                )
                assert attempt.status_code in {401, 403}, attempt.text
            assert _roles_of(service, PARTNER) == [JELLYFIN_ACCESS]

            # And a client-side claim never changes what the server reports.
            status = await partner_http.get("/api/portal/nas", headers=partner)
            assert status.json()["can_shutdown"] is False
    finally:
        await app.state.shutdown_workers()
        del service


# ---------------------------------------------------------------------------
# 6. Tampered and unproven requests are refused.
# ---------------------------------------------------------------------------


async def _role_change_requires_a_bound_fresh_assertion(tmp_path) -> None:
    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({JELLYFIN_ACCESS}),
        )
        _enroll(service)
        _admin_passkey(service)

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            headers = await _mutation(admin_http, ADMIN)

            # No grant at all.
            unproven = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "confirm": True,
                },
            )
            assert unproven.status_code in {401, 403}
            assert unproven.json()["error"] == "fresh_required"

            # A grant collected for the partner, replayed against the owner.
            # The subject binding is what refuses it.
            grant = await _fresh_grant(admin_http, headers, f"identity:{PARTNER}")
            replayed = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "fresh_grant": grant,
                    "confirm": True,
                },
            )
            assert replayed.status_code in {401, 403}
            assert replayed.json()["error"] == "fresh_binding_denied"

        assert _roles_of(service, OWNER) == [JELLYFIN_ACCESS]
        assert _roles_of(service, PARTNER) == [JELLYFIN_ACCESS]
    finally:
        await app.state.shutdown_workers()
        del service


async def _confirmation_and_role_names_are_checked(tmp_path) -> None:
    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({JELLYFIN_ACCESS}),
        )
        _admin_passkey(service)

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            headers = await _mutation(admin_http, ADMIN)

            # An unconfirmed request is refused even with a valid assertion,
            # and the assertion is spent rather than left replayable.
            grant = await _fresh_grant(admin_http, headers, f"identity:{OWNER}")
            unconfirmed = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "fresh_grant": grant,
                    "confirm": False,
                },
            )
            assert unconfirmed.status_code in {400, 403}
            assert unconfirmed.json()["error"] == "confirmation_required"

            reused = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "fresh_grant": grant,
                    "confirm": True,
                },
            )
            assert reused.status_code in {401, 403, 429}
            assert _roles_of(service, OWNER) == [JELLYFIN_ACCESS]
    finally:
        await app.state.shutdown_workers()
        del service


async def _invented_roles_and_unenrolled_identities_are_refused(tmp_path) -> None:
    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _admin_passkey(service)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            headers = await _mutation(admin_http, ADMIN)

            # `administrator` is not a portal role and cannot be requested as
            # one. This is the escalation the role vocabulary exists to stop.
            grant = await _fresh_grant(admin_http, headers, f"identity:{OWNER}")
            invented = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, "administrator"],
                    "fresh_grant": grant,
                    "confirm": True,
                },
            )
            assert invented.status_code in {400, 403}
            assert invented.json()["error"] == "role_denied"

            # A name nobody has enrolled is refused, not created: this endpoint
            # must never become a way to hold a role without a passkey.
            grant = await _fresh_grant(admin_http, headers, "identity:nobody@example.com")
            absent = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": "identity:nobody@example.com",
                    "roles": [JELLYFIN_ACCESS],
                    "fresh_grant": grant,
                    "confirm": True,
                },
            )
            assert absent.status_code in {403, 404, 429}
            assert service.auth_state.portal_identity("identity:nobody@example.com") is None
    finally:
        await app.state.shutdown_workers()
        del service


async def _a_revoked_identity_is_not_reinstated_by_a_role_edit(tmp_path) -> None:
    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({JELLYFIN_ACCESS}),
        )
        _admin_passkey(service)
        service.auth_state.revoke_portal_identity(f"identity:{OWNER}")

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            headers = await _mutation(admin_http, ADMIN)
            grant = await _fresh_grant(admin_http, headers, f"identity:{OWNER}")
            attempt = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "fresh_grant": grant,
                    "confirm": True,
                },
            )
            assert attempt.status_code in {403, 404}
            assert attempt.json()["error"] == "identity_denied"

        record = service.auth_state.portal_identity(f"identity:{OWNER}")
        assert record is not None and record.revoked is True
        assert service.auth_state.portal_roles(f"identity:{OWNER}") == frozenset()

        # Revocation deliberately leaves the credential alone, so the passkey
        # still exists. That is exactly why this endpoint must not reinstate:
        # otherwise a role edit would silently restore access that an
        # administrator ended. Reinstatement is not a supported operation here.
        assert any(
            item.identity == f"identity:{OWNER}" and not item.revoked
            for item in service.auth_state.credentials(f"identity:{OWNER}")
        )
    finally:
        await app.state.shutdown_workers()
        del service


# ---------------------------------------------------------------------------
# 7. Wake is untouched.
# ---------------------------------------------------------------------------


async def _wake_authority_is_unchanged(tmp_path) -> None:
    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(service)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as partner_http:
            partner = await _mutation(partner_http, PARTNER)
            await _sign_in(partner_http, partner)

            # Wake still belongs to `jellyfin_access`, with no passkey ceremony
            # and no `nas_power`. Shutdown's requirements did not spread to it.
            woken = await partner_http.post(
                "/api/portal/wake", headers=partner, json={}
            )
            assert woken.status_code == 200, woken.text
            assert woken.json()["status"] == "wake_packet_sent"

            # And it is still POST-only.
            assert (
                await partner_http.get("/api/portal/wake", headers=partner)
            ).status_code == 405
    finally:
        await app.state.shutdown_workers()
        del service


# ---------------------------------------------------------------------------
# 8. Structural facts that keep the vocabulary closed.
# ---------------------------------------------------------------------------


def test_the_invite_path_still_grants_jellyfin_access_only() -> None:
    """Enrollment must not become a way to arrive holding NAS power."""

    body = inspect.getsource(PortalService.create_invite)
    assert "JELLYFIN_ACCESS" in body
    assert "NAS_POWER" not in body


def test_portal_roles_remain_exactly_two() -> None:
    """A widened vocabulary would silently widen this endpoint."""

    assert PORTAL_ROLES == frozenset({JELLYFIN_ACCESS, NAS_POWER})
    assert "administrator" not in PORTAL_ROLES


def test_the_role_purpose_is_a_first_class_ceremony() -> None:
    from butters.auth.manager import PasskeyManager

    assert "portal_role_update" in PasskeyManager.PURPOSES
    # It must not be reachable as the portal's own sign-in purpose.
    assert PasskeyManager.PORTAL_PURPOSE not in PasskeyManager.PURPOSES


def test_administrator_authority_never_reads_a_portal_role() -> None:
    """The role table and administrator authorization stay disjoint."""

    from butters.web.security import AuthPolicy

    body = inspect.getsource(AuthPolicy.admin_identity)
    for forbidden in ("portal_roles", "nas_power", "jellyfin_access", "auth_state"):
        assert forbidden not in body


async def _nas_power_alone_implies_nothing_else(tmp_path) -> None:
    """The two roles are independent in both directions."""

    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({NAS_POWER}),
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as owner_http:
            owner = await _mutation(owner_http, OWNER)
            signed_in = await _sign_in(
                owner_http, owner, credential_id=OWNER_CREDENTIAL
            )
            assert signed_in.json()["value"]["roles"] == [NAS_POWER]

            # NAS power does not carry Jellyfin access...
            assert (
                await owner_http.get("/api/portal/nas", headers=owner)
            ).status_code == 401
            assert (
                await owner_http.get("/api/portal/destination", headers=owner)
            ).status_code == 401

            # ...nor administrator status. `OWNER` is not in admin_identities,
            # and holding a portal role does not add them to it.
            for path in (
                "/api/admin/overview",
                "/api/admin/portal/identities",
                "/api/admin/tools/nas",
            ):
                assert (
                    await owner_http.get(path, headers=owner)
                ).status_code in {401, 403}, path

            # The role endpoint is not reachable by the person it concerns.
            attempt = await owner_http.post(
                ROLES_PATH,
                headers=owner,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "confirm": True,
                    "fresh_grant": "forged",
                },
            )
            assert attempt.status_code in {401, 403}
            assert _roles_of(service, OWNER) == [NAS_POWER]
    finally:
        await app.state.shutdown_workers()
        del service


async def _granting_a_role_preserves_the_existing_passkey(tmp_path) -> None:
    """A role change must not disturb credentials.

    The owner holds one active passkey and the deployment intends to keep it at
    one, so a repair that quietly revoked or duplicated it would be a real
    regression even with the roles correct.
    """

    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({JELLYFIN_ACCESS}),
        )
        _admin_passkey(service)
        before = [
            item.safe_dict() for item in service.auth_state.credentials(f"identity:{OWNER}")
        ]
        assert len(before) == 1

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            headers = await _mutation(admin_http, ADMIN)
            grant = await _fresh_grant(admin_http, headers, f"identity:{OWNER}")
            done = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "fresh_grant": grant,
                    "confirm": True,
                },
            )
            assert done.status_code == 200, done.text

        after = [
            item.safe_dict() for item in service.auth_state.credentials(f"identity:{OWNER}")
        ]
        assert after == before
        # The label the identity was enrolled under is kept, not overwritten
        # with something derived from the role change.
        record = service.auth_state.portal_identity(f"identity:{OWNER}")
        assert record is not None and record.label == "Partner"
    finally:
        await app.state.shutdown_workers()
        del service


async def _the_role_path_cannot_mint_a_shutdown(tmp_path) -> None:
    """A role grant must not produce anything the shutdown flow accepts.

    The shutdown ceremony consumes a FRESH *context* bound to a frozen plan's
    digest. The role endpoint consumes a FRESH *grant* bound to an identity.
    These must not be interchangeable, or granting a role would become a way to
    pre-authorize a power action.
    """

    app, service, nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity=OWNER,
            credential_id=OWNER_CREDENTIAL,
            roles=frozenset({JELLYFIN_ACCESS}),
        )
        _admin_passkey(service)

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            headers = await _mutation(admin_http, ADMIN)
            grant = await _fresh_grant(admin_http, headers, f"identity:{OWNER}")
            done = await admin_http.post(
                ROLES_PATH,
                headers=headers,
                json={
                    "identity": f"identity:{OWNER}",
                    "roles": [JELLYFIN_ACCESS, NAS_POWER],
                    "fresh_grant": grant,
                    "confirm": True,
                },
            )
            assert done.status_code == 200, done.text
            # No pending action and no job came out of a role change.
            assert "jobs" not in done.json()
            assert "pending_action" not in done.json()

        # The grant is spent, and nothing in the response can stand in for the
        # shutdown ceremony: the plan must still be frozen and asserted against.
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as owner_http:
            owner = await _mutation(owner_http, OWNER)
            await _sign_in(owner_http, owner, credential_id=OWNER_CREDENTIAL)
            planned = await owner_http.post(
                "/api/portal/shutdown/plan", headers=owner, json={"confirm": True}
            )
            assert planned.status_code == 200, planned.text
            pending = planned.json()["pending_action"]["pending_action_id"]

            # Replaying the administrator's role grant as the shutdown proof
            # authorizes nothing.
            forged = await owner_http.post(
                "/api/portal/shutdown/authenticate/verify",
                headers=owner,
                json={"ceremony_id": grant, "credential": _credential(OWNER_CREDENTIAL)},
            )
            # 409 is the ceremony store refusing an id it never issued.
            assert forged.status_code in {400, 401, 403, 409}

            # The plan was frozen but never ran: no job exists for it, and
            # FakeNas.shutdown raises if anything reaches it. The only thing
            # that can move the plan is an assertion bound to its own digest.
            assert pending
            assert service.action_state.jobs(identity=f"identity:{OWNER}") == ()
        assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


def test_the_hidden_attribute_still_wins() -> None:
    """The stylesheet fix that stopped unauthorized controls rendering.

    `can_shutdown` false means the Portal marks the control `hidden`, and that
    only actually hides it because `base.css` overrides the author `display`
    that components.css sets on buttons. This is the same defect class as the
    one under repair -- authority in the backend, visibility in the browser --
    so it is asserted alongside it.
    """

    base = (
        Path(__file__).resolve().parents[1]
        / "src/butters/web/static/assets/base.css"
    ).read_text()
    collapsed = " ".join(base.split())
    assert "[hidden] { display: none !important; }" in collapsed


def test_the_portal_derives_shutdown_visibility_from_the_server() -> None:
    """The Portal script must not decide `can_shutdown` for itself."""

    script = (
        Path(__file__).resolve().parents[1]
        / "src/butters/web/static/assets/portal.js"
    ).read_text()
    # It may *read* the server's answer, but must not compute one from roles.
    assert "can_shutdown" in script
    for forbidden in ('roles.includes("nas_power")', "roles.includes('nas_power')"):
        assert forbidden not in script

    # And the server's answer is derived from the role plus the capability gate.
    body = inspect.getsource(PortalService.nas_state)
    assert "NAS_POWER in roles" in body
    assert 'capability.get("shutdown_configured") is True' in body


# --------------------------------- drivers ----------------------------------


def test_owner_granted_nas_power_sees_shutdown(tmp_path) -> None:
    asyncio.run(_owner_granted_nas_power_sees_shutdown(tmp_path))


def test_shutdown_still_requires_its_own_fresh_ceremony(tmp_path) -> None:
    asyncio.run(_shutdown_still_requires_its_own_fresh_ceremony(tmp_path))


def test_partner_keeps_jellyfin_only(tmp_path) -> None:
    asyncio.run(_partner_keeps_jellyfin_only(tmp_path))


def test_client_claims_cannot_create_authority(tmp_path) -> None:
    asyncio.run(_client_claims_cannot_create_authority(tmp_path))


def test_role_change_requires_a_bound_fresh_assertion(tmp_path) -> None:
    asyncio.run(_role_change_requires_a_bound_fresh_assertion(tmp_path))


def test_confirmation_and_role_names_are_checked(tmp_path) -> None:
    asyncio.run(_confirmation_and_role_names_are_checked(tmp_path))


def test_invented_roles_and_unenrolled_identities_are_refused(tmp_path) -> None:
    asyncio.run(_invented_roles_and_unenrolled_identities_are_refused(tmp_path))


def test_a_revoked_identity_is_not_reinstated_by_a_role_edit(tmp_path) -> None:
    asyncio.run(_a_revoked_identity_is_not_reinstated_by_a_role_edit(tmp_path))


def test_wake_authority_is_unchanged(tmp_path) -> None:
    asyncio.run(_wake_authority_is_unchanged(tmp_path))


def test_nas_power_alone_implies_nothing_else(tmp_path) -> None:
    asyncio.run(_nas_power_alone_implies_nothing_else(tmp_path))


def test_granting_a_role_preserves_the_existing_passkey(tmp_path) -> None:
    asyncio.run(_granting_a_role_preserves_the_existing_passkey(tmp_path))


def test_the_role_path_cannot_mint_a_shutdown(tmp_path) -> None:
    asyncio.run(_the_role_path_cannot_mint_a_shutdown(tmp_path))
