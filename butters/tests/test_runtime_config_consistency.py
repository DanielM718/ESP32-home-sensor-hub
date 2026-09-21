"""One effective configuration, one peer, one credential state.

Three defects of the same shape: two pieces of code answering the same
question independently and drifting apart.

* the uvicorn listener and the application object each resolved the config
  file, so a second instance pointed at another file still bound the running
  service's port;
* the proxy-header invariant lived as a bare keyword in one launcher, so a new
  launch path could silently reintroduce uvicorn's default and let a forwarded
  header rewrite the TCP peer Butters authorises on;
* the security posture answered "is a credential configured" from the process
  environment while the AI control plane answered it from the credential
  store, so Admin contradicted itself about the same key.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from beta1_harness import (
    ADMIN_IDENTITY,
    PRODUCTION_ORIGIN,
    build_app,
    build_settings,
)
from butters.ai.credentials import OpenAICredentialStore
from butters.assistant_config import (
    ConfigError,
    default_assistant_config_path,
    effective_assistant_config_path,
    load_assistant_settings,
)
from butters.web import __main__ as entrypoint

BUTTERS_DIR = Path(__file__).resolve().parents[1]
PACKAGED_CONFIG = default_assistant_config_path()


# ==================== 1. one effective configuration =======================


def _config_with_port(tmp_path: Path, port: int) -> Path:
    """A copy of the packaged configuration differing only in web.port."""

    target = tmp_path / "alternate.toml"
    body = PACKAGED_CONFIG.read_text(encoding="utf-8")
    patched, count = re.subn(
        r"(?m)^port = \d+$", f"port = {port}", body, count=1
    )
    assert count == 1, "expected exactly one web.port line to rewrite"
    target.write_text(patched, encoding="utf-8")
    return target


def test_the_packaged_default_is_used_when_nothing_overrides_it(monkeypatch) -> None:
    monkeypatch.delenv("BUTTERS_CONFIG", raising=False)
    assert effective_assistant_config_path() == PACKAGED_CONFIG


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_override_is_not_an_override(monkeypatch, blank: str) -> None:
    monkeypatch.setenv("BUTTERS_CONFIG", blank)
    assert effective_assistant_config_path() == PACKAGED_CONFIG


def test_butters_config_selects_the_file_the_loader_reads(tmp_path, monkeypatch) -> None:
    alternate = _config_with_port(tmp_path, 8095)
    monkeypatch.setenv("BUTTERS_CONFIG", str(alternate))
    assert effective_assistant_config_path() == alternate
    assert load_assistant_settings().web.port == 8095


def test_an_explicit_path_still_wins_over_the_environment(tmp_path, monkeypatch) -> None:
    """Tests and tooling that name one exact file keep getting that file."""

    monkeypatch.setenv("BUTTERS_CONFIG", str(_config_with_port(tmp_path, 8095)))
    named = _config_with_port(tmp_path, 8099)
    assert load_assistant_settings(named).web.port == 8099


def test_a_configured_file_that_is_missing_fails_closed(tmp_path, monkeypatch) -> None:
    """Never a silent fall back to the packaged defaults."""

    monkeypatch.setenv("BUTTERS_CONFIG", str(tmp_path / "absent.toml"))
    with pytest.raises(ConfigError):
        load_assistant_settings()


def test_a_configured_file_that_is_malformed_fails_closed(tmp_path, monkeypatch) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("[web\nport = ", encoding="utf-8")
    monkeypatch.setenv("BUTTERS_CONFIG", str(broken))
    with pytest.raises(ConfigError):
        load_assistant_settings()


def test_the_listener_follows_the_configured_file_not_the_packaged_one(
    tmp_path, monkeypatch
) -> None:
    """The defect, as a test.

    The packaged configuration says 8090. With BUTTERS_CONFIG naming a file
    that says 8095, the uvicorn listener must be built for 8095 — previously
    it took 8090 from the packaged file and collided with the running service.
    """

    packaged_port = load_assistant_settings(PACKAGED_CONFIG).web.port
    alternate = _config_with_port(tmp_path, 8095)
    assert packaged_port != 8095

    monkeypatch.setenv("BUTTERS_CONFIG", str(alternate))
    with patch.object(entrypoint.uvicorn, "run") as run:
        entrypoint.main()

    run.assert_called_once()
    positional, options = run.call_args
    assert positional == (entrypoint.APPLICATION,)
    assert options["port"] == 8095, "the listener ignored BUTTERS_CONFIG"
    assert options["port"] != packaged_port
    # Nothing was bound: uvicorn.run never ran.


def test_the_application_and_the_listener_read_the_same_file(tmp_path, monkeypatch) -> None:
    alternate = _config_with_port(tmp_path, 8095)
    monkeypatch.setenv("BUTTERS_CONFIG", str(alternate))

    with patch.object(entrypoint.uvicorn, "run") as run:
        entrypoint.main()
    listener_port = run.call_args[1]["port"]

    # create_app() resolves its own settings through the same loader.
    application_port = load_assistant_settings().web.port
    assert listener_port == application_port == 8095


def test_create_app_no_longer_parses_the_environment_itself() -> None:
    source = (BUTTERS_DIR / "src/butters/web/app.py").read_text(encoding="utf-8")
    assert 'os.environ["BUTTERS_CONFIG"]' not in source
    assert 'os.getenv("BUTTERS_CONFIG")' not in source
    # One resolver, named once.
    assert source.count("load_assistant_settings()") == 1


# ===================== 2. the proxy-header invariant =======================


def test_the_launcher_never_enables_proxy_header_rewriting() -> None:
    options = entrypoint.server_options(build_settings(Path("/tmp")))
    assert options["proxy_headers"] is False


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"FORWARDED_ALLOW_IPS": "*"},
        {"UVICORN_PROXY_HEADERS": "1"},
        {"PROXY_HEADERS": "true"},
    ],
)
def test_no_environment_variable_can_turn_proxy_headers_back_on(
    monkeypatch, environment: dict[str, str]
) -> None:
    """The invariant is stated in code, so the environment cannot move it."""

    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert entrypoint.server_options(build_settings(Path("/tmp")))["proxy_headers"] is False


def test_every_supported_launch_path_goes_through_this_module() -> None:
    """A new launcher that called uvicorn directly would lose the invariant.

    That is exactly how it was lost once: an ad-hoc `python -m uvicorn`
    inherited uvicorn's proxy_headers default and a forwarded header started
    rewriting the peer that AuthPolicy checks.
    """

    unit = (BUTTERS_DIR / "systemd/butters-web.service").read_text(encoding="utf-8")
    exec_start = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_start.endswith("-m butters.web"), exec_start
    assert "uvicorn" not in exec_start

    script = (BUTTERS_DIR / "scripts/butters-web").read_text(encoding="utf-8")
    assert "-m butters.web" in script
    assert "-m uvicorn" not in script and "uvicorn " not in script

    # And no other committed launcher invokes uvicorn's CLI.
    for path in (*BUTTERS_DIR.glob("scripts/*"), *BUTTERS_DIR.glob("systemd/*")):
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8", errors="ignore")
        assert "-m uvicorn" not in body, path
        assert "uvicorn --" not in body, path


def test_the_invariant_is_documented_where_it_is_set() -> None:
    source = (BUTTERS_DIR / "src/butters/web/__main__.py").read_text(encoding="utf-8")
    block = source[: source.index('"proxy_headers": False')]
    assert "X-Forwarded-For" in block
    assert "default is True" in block


# ================== 3. one authoritative credential state ==================


def _store(tmp_path: Path, environment: dict[str, str]) -> OpenAICredentialStore:
    return OpenAICredentialStore(tmp_path, environment=environment)


def test_posture_and_control_plane_agree_that_nothing_is_configured(tmp_path) -> None:
    state = _store(tmp_path, {}).state()
    assert state.as_dict()["configured"] is False
    assert state.as_posture()["configured"] is False
    assert state.as_posture()["source"] == "none"


def test_posture_and_control_plane_agree_on_a_stored_credential(tmp_path) -> None:
    store = _store(tmp_path, {})
    store.store("sk-stored-credential-value", validation=None)
    state = store.state()
    assert state.as_dict()["configured"] is True
    assert state.as_posture()["configured"] is True
    assert state.as_dict()["source"] == state.as_posture()["source"] == "butters_store"


def test_posture_and_control_plane_agree_on_an_environment_credential(tmp_path) -> None:
    state = _store(tmp_path, {"OPENAI_API_KEY": "sk-from-the-unit-environment"}).state()
    assert state.as_dict()["configured"] is True
    assert state.as_posture()["configured"] is True
    assert state.as_dict()["source"] == state.as_posture()["source"] == "unit_environment"


def test_a_stored_credential_outranks_an_empty_environment_variable(tmp_path) -> None:
    """Production's exact shape: the key is in the store, the env var is blank."""

    store = _store(tmp_path, {"OPENAI_API_KEY": "   "})
    store.store("sk-stored-credential-value", validation=None)
    state = store.state()
    assert state.as_posture()["configured"] is True
    assert state.as_posture()["source"] == "butters_store"


def test_the_posture_projection_withholds_the_fingerprint(tmp_path) -> None:
    store = _store(tmp_path, {})
    store.store("sk-stored-credential-value", validation=None)
    state = store.state()
    posture = state.as_posture()
    assert set(posture) == {"configured", "provider", "source", "last_verification"}
    assert state.as_dict()["fingerprint"] is not None
    assert "fingerprint" not in posture


def test_no_projection_can_carry_the_secret(tmp_path) -> None:
    secret = "sk-stored-credential-value"
    store = _store(tmp_path, {})
    store.store(secret, validation=None)
    state = store.state()
    for projection in (state.as_dict(), state.as_posture()):
        serialized = json.dumps(projection)
        assert secret not in serialized
        assert "sk-" not in serialized
        assert "Authorization" not in serialized


def test_reading_the_status_never_touches_the_credential_files(tmp_path) -> None:
    """A posture read is a read. It does not revalidate or rewrite anything."""

    store = _store(tmp_path, {})
    store.store("sk-stored-credential-value", validation=None)
    directory = tmp_path / "credentials"
    before = {
        path.name: (path.stat().st_mtime_ns, path.stat().st_size, path.read_bytes())
        for path in sorted(directory.iterdir())
    }
    for _ in range(3):
        store.state().as_posture()
        store.state().as_dict()
    after = {
        path.name: (path.stat().st_mtime_ns, path.stat().st_size, path.read_bytes())
        for path in sorted(directory.iterdir())
    }
    assert before == after


def _security(app, identity: str = ADMIN_IDENTITY):
    async def scenario():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4242))
        async with httpx.AsyncClient(transport=transport, base_url=PRODUCTION_ORIGIN) as http:
            await http.get("/api/session", headers={"Tailscale-User-Login": identity})
            response = await http.get(
                "/api/admin/security", headers={"Tailscale-User-Login": identity}
            )
            body = response.content
            payload = response.json()
        await app.state.shutdown_workers()
        return payload, body

    return asyncio.run(scenario())


def test_the_two_admin_endpoints_agree_over_http(tmp_path, monkeypatch) -> None:
    """The contradiction, end to end: both surfaces, one stored credential."""

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app, service, _settings = build_app(
        tmp_path, development_mode=False, allowed_origins=(PRODUCTION_ORIGIN,)
    )
    service.ai.credentials.store("sk-stored-credential-value", validation=None)

    control_plane = service.ai.credential_state()
    payload, body = _security(app)
    posture = payload["credentials"]["openai"]

    assert control_plane["configured"] is True
    assert posture["configured"] is True, "posture still disagrees with the store"
    assert control_plane["source"] == posture["source"] == "butters_store"
    # And the secret is nowhere in the posture response.
    assert b"sk-stored" not in body and b"credential-value" not in body
    assert b"fingerprint" not in body


def test_the_two_admin_endpoints_agree_when_nothing_is_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app, service, _settings = build_app(
        tmp_path, development_mode=False, allowed_origins=(PRODUCTION_ORIGIN,)
    )
    control_plane = service.ai.credential_state()
    payload, _body = _security(app)
    assert control_plane["configured"] is False
    assert payload["credentials"]["openai"]["configured"] is False
    assert payload["credentials"]["openai"]["source"] == "none"


def test_the_posture_no_longer_asks_the_process_environment() -> None:
    source = (BUTTERS_DIR / "src/butters/web/service.py").read_text(encoding="utf-8")
    block = source[source.index("def credential_status"):]
    block = block[: block.index("\n    def ")]
    # The docstring explains the old behaviour, so compare the code only.
    code = block[block.index('"""', block.index('"""') + 3) + 3 :]
    assert 'os.getenv("OPENAI_API_KEY")' not in code
    assert "self.ai.credential_posture()" in code
