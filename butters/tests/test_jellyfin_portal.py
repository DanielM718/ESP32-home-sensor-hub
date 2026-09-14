"""Jellyfin access portal: authentication, RBAC, wake, and redirect safety.

The properties under test are the ones that keep a partner's access narrow: the
role authorizes NAS status and wake and nothing else, it never implies
administrator, wake is unreachable by GET, and the redirect is always one of two
configured URLs chosen from a trusted ingress rather than from the request.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
from butters.assistant_config import (
    NasEndpointSettings,
    PortalSettings,
    load_assistant_settings,
)
from butters.auth.manager import AuthenticationVerification
from butters.auth.store import JELLYFIN_ACCESS
from butters.integrations.nas_status import (
    JellyfinState,
    NasAggregate,
    NasObservation,
    Reach,
)
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.locality import Locality, LocalityClassifier, jellyfin_destination
from butters.web.service import BetaAssistantService

PARTNER = "partner@example.com"
ADMIN = "admin@example.com"
LAN_URL = "http://192.168.1.50:8096"
TS_URL = "https://nas.tail0000.ts.net"

ENDPOINTS = NasEndpointSettings(
    lan_host="192.168.1.50",
    tailscale_host="nas.tail0000.ts.net",
    jellyfin_lan_url=LAN_URL,
    jellyfin_tailscale_url=TS_URL,
    cache_seconds=0.0,
).validated()


class NoCloud:
    available = False


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


class FakeWebAuthn:
    def registration_options(self, **_kwargs):
        return {
            "challenge": "dGVzdA",
            "rp": {"id": "sensor-pi.tail9644cc.ts.net"},
            "user": {"id": "dXNlcg", "name": "x", "displayName": "x"},
        }

    def verify_registration(self, credential, **_kwargs):
        from butters.auth.manager import RegistrationVerification

        return RegistrationVerification(
            credential["id"].encode(), b"public", 0, "multi_device", True
        )

    def authentication_options(self, **_kwargs):
        return {
            "challenge": "dGVzdA",
            "rpId": "sensor-pi.tail9644cc.ts.net",
            "allowCredentials": [],
            "userVerification": "required",
        }

    def verify_authentication(self, credential, **_kwargs):
        if credential.get("uv") is not True:
            return AuthenticationVerification(0, None, None, False)
        return AuthenticationVerification(0, "multi_device", True, True)


class FakeNas:
    def __init__(self) -> None:
        self.wakes = 0
        self.observation = NasObservation(
            Reach.UNREACHABLE,
            Reach.UNREACHABLE,
            Reach.UNREACHABLE,
            JellyfinState.UNAVAILABLE,
            NasAggregate.OFFLINE,
            0.0,
            0.01,
        )

    def wake(self, _cancel=None):
        self.wakes += 1
        return {"accepted": True, "outcome": "wake_packet_sent"}

    def shutdown(self, _cancel=None):
        raise AssertionError("the portal must never reach NAS shutdown")

    def observe(self, *, wake_requested_at=None, refresh=False):
        return self.observation.safe_dict()

    def become_ready(self) -> None:
        self.observation = NasObservation(
            Reach.REACHABLE,
            Reach.REACHABLE,
            Reach.REACHABLE,
            JellyfinState.READY,
            NasAggregate.READY,
            0.0,
            0.01,
        )


def _application(tmp_path, *, portal_settings: PortalSettings | None = None):
    base = load_assistant_settings()
    device = replace(base.actions.nas, enabled=True, configured=True)
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        actions=replace(base.actions, nas=device).validated(),
        nas_endpoints=ENDPOINTS,
        portal=(portal_settings or PortalSettings(enabled=True)).validated(),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=(ADMIN,),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    service = BetaAssistantService(
        settings, vocabulary, general_reasoner=NoCloud(), state_dir=tmp_path
    )
    service.passkeys.backend = FakeWebAuthn()
    nas = FakeNas()
    service.assistant.skills.get("wake_nas").implementation.__self__.nas = nas
    service.assistant.nas_adapter = nas
    return create_app(settings, vocabulary, service, stt_engine_factory=Engine), service, nas


def _enroll(service, identity=PARTNER, credential_id=b"partner-credential"):
    """Grant the role and register a credential, as enrollment would."""

    service.auth_state.grant_portal_roles(
        f"identity:{identity}", "Partner", frozenset({JELLYFIN_ACCESS}), maximum=16
    )
    service.auth_state.add_credential(
        credential_id=credential_id,
        public_key=b"public",
        user_id=b"user",
        identity=f"identity:{identity}",
        label="Partner phone",
        sign_count=0,
        device_type="multi_device",
        backed_up=True,
    )


def _credential(credential_id=b"partner-credential") -> dict[str, object]:
    return {
        "id": base64.urlsafe_b64encode(credential_id).rstrip(b"=").decode(),
        "uv": True,
    }


async def _mutation(http, identity):
    headers = {"tailscale-user-login": identity}
    session = (await http.get("/api/session", headers=headers)).json()
    return {
        **headers,
        "origin": "http://testserver",
        "x-butters-csrf": session["csrf_token"],
    }


async def _settles(predicate, *, timeout: float = 5.0) -> bool:
    """Wait for a coordinator-scheduled effect.

    ActionCoordinator.execute() returns as soon as the job is queued and the
    skill runs on a worker thread, so a counter assertion immediately after the
    POST is a race under load rather than a real expectation.
    """

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _sign_in(http, mutation, credential_id=b"partner-credential"):
    begin = await http.post(
        "/api/portal/authenticate/options", headers=mutation, json={}
    )
    assert begin.status_code == 200, begin.text
    return await http.post(
        "/api/portal/authenticate/verify",
        headers=mutation,
        json={
            "ceremony_id": begin.json()["value"]["ceremony_id"],
            "credential": _credential(credential_id),
        },
    )


# --------------------------- authentication / RBAC ---------------------------


async def _unauthenticated_access_is_denied(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            for method, path in (
                ("GET", "/api/portal/nas"),
                ("GET", "/api/portal/destination"),
                ("POST", "/api/portal/wake"),
            ):
                response = await (
                    http.get(path, headers=mutation)
                    if method == "GET"
                    else http.post(path, headers=mutation, json={})
                )
                assert response.status_code == 401, path
                assert response.json()["error"] == "portal_authentication_required"
            assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


async def _valid_role_is_accepted_and_grants_nothing_more(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            verified = await _sign_in(http, mutation)
            assert verified.status_code == 200
            body = verified.json()["value"]
            assert body["roles"] == [JELLYFIN_ACCESS]
            # A portal sign-in confers no administrator rights whatsoever.
            assert body["administrator"] is False

            state = await http.get("/api/portal/nas", headers=mutation)
            assert state.status_code == 200
            assert state.json()["aggregate"] == "OFFLINE"

            # Administrator surfaces stay closed to this identity.
            for path in (
                "/admin",
                "/api/admin/overview",
                "/api/admin/tools/desktop",
                "/api/admin/tools/nas",
                "/api/admin/portal/identities",
            ):
                denied = await http.get(path, headers=mutation)
                assert denied.status_code in {401, 403}, path

            # The shared capability endpoint is pre-existing and answers any
            # tailnet session with the non-administrator view; holding the
            # portal role changes nothing about it. What matters is that the
            # role cannot INVOKE anything, so ask the assistant to run one.
            catalog = await http.get("/api/capabilities", headers=mutation)
            assert catalog.status_code == 200
            assert catalog.json()["capabilities"]
            refused = await http.post(
                "/api/chat",
                headers=mutation,
                json={"text": "shut down my desktop"},
            )
            assert refused.status_code == 200
            assert refused.json()["route"] == "action_denied"
            assert "administrator_required" in refused.json()["reason_codes"]

            for path in (
                "/api/admin/tools/shutdown-nas",
                "/api/admin/tools/wake-nas",
                "/api/admin/tools/desktop/wake",
                "/api/admin/tools/desktop/shutdown",
                "/api/admin/tools/desktop/launch-app",
                "/api/admin/portal/invite",
            ):
                denied = await http.post(path, headers=mutation, json={})
                assert denied.status_code in {400, 401, 403}, path

            # The portal itself has no shutdown route at all.
            missing = await http.post(
                "/api/portal/shutdown", headers=mutation, json={}
            )
            assert missing.status_code == 404
            assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


async def _revocation_takes_effect_immediately(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)
            assert (await http.get("/api/portal/nas", headers=mutation)).status_code == 200

            # Revoking the role ends the live session on the next request,
            # without waiting for the portal session to expire.
            service.auth_state.revoke_portal_identity(f"identity:{PARTNER}")
            denied = await http.get("/api/portal/nas", headers=mutation)
            assert denied.status_code == 401
            refused = await http.post("/api/portal/wake", headers=mutation, json={})
            assert refused.status_code == 401
            assert nas.wakes == 0

            # And a fresh sign-in is refused too.
            begin = await http.post(
                "/api/portal/authenticate/options", headers=mutation, json={}
            )
            assert begin.status_code == 403
            assert begin.json()["error"] == "role_denied"
    finally:
        await app.state.shutdown_workers()
        del service


async def _registration_requires_an_invitation(tmp_path) -> None:
    app, service, _nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            for token in ("", "guessed-token"):
                refused = await http.post(
                    "/api/portal/register/options",
                    headers=mutation,
                    json={"label": "Phone", "invite_token": token},
                )
                assert refused.status_code in {400, 403}, token
            assert service.auth_state.portal_roles(f"identity:{PARTNER}") == frozenset()
    finally:
        await app.state.shutdown_workers()
        del service


async def _admin_invitation_enrolls_exactly_one_identity(tmp_path) -> None:
    app, service, _nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            admin = await _mutation(admin_http, ADMIN)
            created = await admin_http.post(
                "/api/admin/portal/invite",
                headers=admin,
                json={"identity": f"identity:{PARTNER}", "label": "Partner"},
            )
            assert created.status_code == 200
            token = created.json()["invite_token"]
            assert created.json()["roles"] == [JELLYFIN_ACCESS]

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as other_http:
            # The invitation is bound to one identity; nobody else may use it.
            other = await _mutation(other_http, "stranger@example.com")
            stolen = await other_http.post(
                "/api/portal/register/options",
                headers=other,
                json={"label": "Phone", "invite_token": token},
            )
            assert stolen.status_code in {400, 403}

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            begin = await http.post(
                "/api/portal/register/options",
                headers=mutation,
                json={"label": "Partner phone", "invite_token": token},
            )
            assert begin.status_code == 200
            finish = await http.post(
                "/api/portal/register/verify",
                headers=mutation,
                json={
                    "ceremony_id": begin.json()["value"]["ceremony_id"],
                    "credential": {"id": "partner-credential"},
                },
            )
            assert finish.status_code == 200
            assert finish.json()["value"]["portal_roles"] == [JELLYFIN_ACCESS]
            assert service.auth_state.portal_roles(
                f"identity:{PARTNER}"
            ) == frozenset({JELLYFIN_ACCESS})

            # Single use: the same token cannot enroll a second credential.
            replayed = await http.post(
                "/api/portal/register/options",
                headers=mutation,
                json={"label": "Second", "invite_token": token},
            )
            assert replayed.status_code in {400, 403}
    finally:
        await app.state.shutdown_workers()
        del service


# --------------------------------- wake --------------------------------------


async def _wake_is_post_only_and_csrf_protected(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)

            # A prefetch, crawler, link preview or <img> is a GET; there is no
            # GET route for wake at all.
            for method in ("GET", "HEAD", "PUT", "DELETE"):
                response = await http.request(
                    method, "/api/portal/wake", headers=mutation
                )
                assert response.status_code in {404, 405}, method
                assert nas.wakes == 0

            # A cross-origin form POST fails the Origin check.
            cross = await http.post(
                "/api/portal/wake",
                headers={**mutation, "origin": "https://evil.example"},
                json={},
            )
            assert cross.status_code == 403
            assert cross.json()["error"] == "origin_denied"

            # A same-origin POST without the CSRF header fails too.
            no_csrf = {k: v for k, v in mutation.items() if k != "x-butters-csrf"}
            refused = await http.post("/api/portal/wake", headers=no_csrf, json={})
            assert refused.status_code == 403
            assert refused.json()["error"] == "csrf_denied"
            assert nas.wakes == 0

            accepted = await http.post("/api/portal/wake", headers=mutation, json={})
            assert accepted.status_code == 200
            body = accepted.json()
            assert body["status"] == "wake_packet_sent"
            assert body["message"] == "Wake packet sent"
            # It never claims the NAS came up.
            assert "booted" not in repr(body).lower()
            assert await _settles(lambda: nas.wakes == 1)
    finally:
        await app.state.shutdown_workers()
        del service


async def _wake_takes_no_parameters_and_is_rate_limited(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)
            for payload in (
                {"mac": "00:e2:69:7d:40:cd"},
                {"ip": "192.168.1.50"},
                {"host": "nas"},
                {"broadcast": "192.168.1.255"},
                {"skill": "shutdown_nas"},
                {"redirect": "https://evil.example"},
            ):
                refused = await http.post(
                    "/api/portal/wake", headers=mutation, json=payload
                )
                assert refused.status_code == 400, payload
            assert nas.wakes == 0

            # Bounded: repeated wakes are throttled rather than amplified.
            outcomes = []
            for _ in range(6):
                response = await http.post(
                    "/api/portal/wake", headers=mutation, json={}
                )
                outcomes.append(response.status_code)
            assert 429 in outcomes
            assert await _settles(lambda: 0 < nas.wakes < 6)
    finally:
        await app.state.shutdown_workers()
        del service


async def _polling_progresses_and_bounds_itself(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)

            offline = (await http.get("/api/portal/nas", headers=mutation)).json()
            assert offline["headline"] == "NAS OFFLINE"
            assert offline["detail"] == "Jellyfin Unavailable"
            assert offline["jellyfin_ready"] is False
            assert offline["can_wake"] is True

            await http.post("/api/portal/wake", headers=mutation, json={})
            waiting = (await http.get("/api/portal/nas", headers=mutation)).json()
            assert waiting["last_operation"]["operation"] == "wake_nas"
            assert waiting["wake_elapsed_seconds"] is not None
            assert waiting["max_poll_seconds"] > 0
            # Wake Again is withheld while a wake is still in progress, so the
            # client cannot be nudged into re-sending packets.
            assert waiting["can_wake"] is False

            nas.become_ready()
            ready = (await http.get("/api/portal/nas", headers=mutation)).json()
            assert ready["aggregate"] == "READY"
            assert ready["headline"] == "NAS Online"
            assert ready["detail"] == "Jellyfin Ready"
            assert ready["jellyfin_ready"] is True
            # Current state won, even though the wake record is still present.
            assert ready["last_operation"]["operation"] == "wake_nas"

            # The whole sequence sent exactly one packet.
            assert await _settles(lambda: nas.wakes == 1)

            # And timing out is reported, never retried automatically.
            service._last_operations["nas"]["at"] -= 10_000
            nas.observation = FakeNas().observation
            expired = (await http.get("/api/portal/nas", headers=mutation)).json()
            assert expired["poll_expired"] is True
            assert expired["can_wake"] is True
            assert nas.wakes == 1
    finally:
        await app.state.shutdown_workers()
        del service


# -------------------------------- redirect -----------------------------------


async def _redirect_only_when_ready_and_never_from_request_input(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)

            not_ready = (
                await http.get("/api/portal/destination", headers=mutation)
            ).json()
            assert not_ready["ready"] is False
            assert not_ready["destination"] is None

            nas.become_ready()
            # Every override a caller might try is simply not read.
            hostile = (
                await http.get(
                    "/api/portal/destination"
                    "?redirect=https://evil.example&host=evil.example"
                    "&ip=10.0.0.1&local=true&remote=true&url=https://evil.example",
                    headers={
                        **mutation,
                        "x-forwarded-for": "192.168.1.10",
                        "x-real-ip": "192.168.1.10",
                    },
                )
            ).json()
            assert hostile["ready"] is True
            assert hostile["destination"] in {LAN_URL, TS_URL}
            assert "evil.example" not in hostile["destination"]
            # With no trustworthy classification available, it fails safe.
            assert hostile["destination"] == TS_URL
            assert hostile["locality"] == "unknown"
    finally:
        await app.state.shutdown_workers()
        del service


def test_unauthenticated_access_is_denied(tmp_path) -> None:
    asyncio.run(_unauthenticated_access_is_denied(tmp_path))


def test_valid_role_is_accepted_and_grants_nothing_more(tmp_path) -> None:
    asyncio.run(_valid_role_is_accepted_and_grants_nothing_more(tmp_path))


def test_revocation_takes_effect_immediately(tmp_path) -> None:
    asyncio.run(_revocation_takes_effect_immediately(tmp_path))


def test_registration_requires_an_invitation(tmp_path) -> None:
    asyncio.run(_registration_requires_an_invitation(tmp_path))


def test_admin_invitation_enrolls_exactly_one_identity(tmp_path) -> None:
    asyncio.run(_admin_invitation_enrolls_exactly_one_identity(tmp_path))


def test_wake_is_post_only_and_csrf_protected(tmp_path) -> None:
    asyncio.run(_wake_is_post_only_and_csrf_protected(tmp_path))


def test_wake_takes_no_parameters_and_is_rate_limited(tmp_path) -> None:
    asyncio.run(_wake_takes_no_parameters_and_is_rate_limited(tmp_path))


def test_polling_progresses_and_bounds_itself(tmp_path) -> None:
    asyncio.run(_polling_progresses_and_bounds_itself(tmp_path))


def test_redirect_only_when_ready_and_never_from_request_input(tmp_path) -> None:
    asyncio.run(_redirect_only_when_ready_and_never_from_request_input(tmp_path))


# ------------------------- locality classification ---------------------------


def _headers(values: dict[str, str]):
    class _Headers:
        def get(self, name, default=None):
            return values.get(name.lower(), default)

    return _Headers()


PORTAL = PortalSettings(
    enabled=True,
    lan_networks=("192.168.1.0/24",),
    locality_header="X-Butters-Locality",
    tailscale_status_command=("/usr/bin/tailscale", "status", "--json"),
).validated()


def test_untrusted_peer_is_never_classified_as_lan() -> None:
    classifier = LocalityClassifier(PORTAL, trusted_peers=frozenset({"127.0.0.1"}))
    decision = classifier.classify(
        _headers(
            {
                "x-butters-locality": "lan",
                "x-forwarded-for": "192.168.1.10",
                "x-real-ip": "192.168.1.10",
                "tailscale-user-login": PARTNER,
            }
        ),
        "203.0.113.9",
    )
    assert decision.locality is Locality.UNKNOWN
    assert decision.source == "untrusted_ingress"
    assert jellyfin_destination(decision, ENDPOINTS) == TS_URL


def test_trusted_ingress_may_state_locality() -> None:
    classifier = LocalityClassifier(PORTAL, trusted_peers=frozenset({"127.0.0.1"}))
    lan = classifier.classify(_headers({"x-butters-locality": "lan"}), "127.0.0.1")
    assert lan.locality is Locality.LAN
    assert jellyfin_destination(lan, ENDPOINTS) == LAN_URL
    tailnet = classifier.classify(
        _headers({"x-butters-locality": "tailnet"}), "127.0.0.1"
    )
    assert tailnet.locality is Locality.TAILNET
    assert jellyfin_destination(tailnet, ENDPOINTS) == TS_URL


def test_tailscaled_endpoint_decides_locality_without_client_input() -> None:
    def runner(argv, **_kwargs):
        payload = (
            '{"User":{"7":{"LoginName":"partner@example.com"}},'
            '"Peer":{"a":{"UserID":7,"Online":true,"CurAddr":"192.168.1.23:41641"}}}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload)

    classifier = LocalityClassifier(
        replace(PORTAL, locality_header=""),
        trusted_peers=frozenset({"127.0.0.1"}),
        runner=runner,
    )
    decision = classifier.classify(
        _headers({"tailscale-user-login": PARTNER}), "127.0.0.1"
    )
    assert decision.locality is Locality.LAN
    assert decision.source == "tailscaled_endpoint"
    assert jellyfin_destination(decision, ENDPOINTS) == LAN_URL


def test_remote_endpoint_is_tailnet_and_failures_fail_safe() -> None:
    def remote(argv, **_kwargs):
        payload = (
            '{"User":{"7":{"LoginName":"partner@example.com"}},'
            '"Peer":{"a":{"UserID":7,"Online":true,"CurAddr":"203.0.113.9:41641"}}}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload)

    def broken(argv, **_kwargs):
        raise OSError("tailscaled unavailable")

    settings = replace(PORTAL, locality_header="")
    peers = LocalityClassifier(
        settings, trusted_peers=frozenset({"127.0.0.1"}), runner=remote
    )
    decision = peers.classify(_headers({"tailscale-user-login": PARTNER}), "127.0.0.1")
    assert decision.locality is Locality.TAILNET
    assert jellyfin_destination(decision, ENDPOINTS) == TS_URL

    unavailable = LocalityClassifier(
        settings, trusted_peers=frozenset({"127.0.0.1"}), runner=broken
    )
    failed = unavailable.classify(
        _headers({"tailscale-user-login": PARTNER}), "127.0.0.1"
    )
    assert failed.locality is Locality.UNKNOWN
    assert jellyfin_destination(failed, ENDPOINTS) == TS_URL


def test_destination_is_always_one_of_two_configured_urls() -> None:
    for locality in Locality:
        decision = type(
            "D", (), {"locality": locality, "source": "test"}
        )()
        assert jellyfin_destination(decision, ENDPOINTS) in {LAN_URL, TS_URL}


# ------------------------------- security ------------------------------------


def test_portal_client_never_names_a_host_action_or_redirect() -> None:
    source = (Path(__file__).parents[1] / "src/butters/web/static/assets/portal.js").read_text()
    for forbidden in (
        "192.168.",
        "redirect=",
        "?host=",
        "local=true",
        "/api/admin/",
        "shutdown",
        "skill",
        "broker",
    ):
        assert forbidden not in source, forbidden
    # The one navigation it performs uses the server-supplied destination only.
    assert "window.location.assign(destination.destination)" in source
    assert source.count("window.location.assign") == 1


def test_portal_service_exposes_no_generic_execution() -> None:
    source = (Path(__file__).parents[1] / "src/butters/web/portal.py").read_text()
    # It freezes exactly one plan, and that plan names the fixed wake action.
    assert source.count("self.runtime.actions.freeze(") == 1
    assert 'skill="wake_nas"' in source
    assert source.count("self.runtime.actions.execute(") == 1
    assert "shutdown_nas" not in source
    # No desktop identifier of any kind is reachable from this module.
    for identifier in (
        "shutdown_desktop",
        "wake_desktop",
        "desktop.app",
        "desktop_agent",
        "start_admin_",
    ):
        assert identifier not in source, identifier
