from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from butters.actions.compute import ComputeError, DesktopActions, _capture
from test_beta1_web import _app


def configured(tmp_path, *, build="printf build", test="printf test"):
    # The directory deliberately exercises spaces, quotes and shell metacharacters.
    directory = tmp_path / "project 'quoted' $(not_a_command)"
    directory.mkdir(exist_ok=True)
    path = tmp_path / "compute.toml"
    path.write_text(
        '[desktop]\nhostname="DESKTOP-G4CFVL1"\n'
        f"ssh_config={json.dumps(str(tmp_path / 'ssh-config'))}\ntimeout_seconds=2\n"
        '[commands]\ngit_version="git --version"\n'
        '[projects.fixture]\nhost="desktop"\n'
        f"directory={json.dumps(str(directory))}\n"
        f"build={json.dumps(build)}\ntest={json.dumps(test)}\n"
    )
    path.chmod(0o600)
    actions = DesktopActions(path)
    assert not actions.configuration_error
    return actions


@pytest.mark.parametrize(
    "action,params",
    [
        ("desktop.shell", {"command": "whoami"}),
        ("desktop.ssh_test", {"command": "whoami"}),
        ("desktop.run_registered", {"command": "git_version; whoami"}),
        ("desktop.run_registered", {"command": "unregistered"}),
        ("desktop.compile", {"project": "../fixture"}),
        ("desktop.compile", {"project": ["fixture"]}),
        ("desktop.compile", {"project": "missing"}),
        ("desktop.test", {"project": "fixture", "directory": "/tmp"}),
    ],
)
def test_rejects_unregistered_or_injected_requests(
    tmp_path, monkeypatch, action, params
):
    actions = configured(tmp_path)
    monkeypatch.setattr(actions, "_ssh", lambda _: pytest.fail("must not execute"))
    with pytest.raises(ComputeError):
        actions.execute(action, params)


def test_build_test_quoting_and_stop_on_failure(tmp_path, monkeypatch):
    actions = configured(
        tmp_path, build="printf 'build\\n'; cd /", test="printf 'test\\n'; pwd"
    )
    monkeypatch.setattr(
        actions, "_ssh", lambda command: _capture(["/bin/bash", "-c", command], 2)
    )
    result = actions.execute("desktop.build_and_test", {"project": "fixture"})
    assert result["success"] and result["exit_code"] == 0
    assert result["stdout"].startswith("build\ntest\n")
    assert "project 'quoted' $(not_a_command)" in result["stdout"]
    assert result["completed_at"] >= result["started_at"]
    assert result["duration_seconds"] >= 0
    actions = configured(
        tmp_path, build="printf failure >&2; exit 17", test="printf MUST_NOT_RUN"
    )
    monkeypatch.setattr(
        actions, "_ssh", lambda command: _capture(["/bin/bash", "-c", command], 2)
    )
    result = actions.execute("desktop.build_and_test", {"project": "fixture"})
    assert not result["success"] and result["exit_code"] == 17
    assert result["stderr"] == "failure" and "MUST_NOT_RUN" not in result["stdout"]


def test_capture_timeout_output_cap_and_redaction():
    result = _capture(
        [sys.executable, "-c", "import time; time.sleep(5)"], 1, remote=True
    )
    assert result["timed_out"] and not result["success"]
    assert result["remote_completion_unknown"]
    result = _capture([sys.executable, "-c", "print('x'*100000)"], 2)
    assert result["output_truncated"] and len(result["stdout"]) <= 65536
    result = _capture(
        [sys.executable, "-c", "print('API_KEY=synthetic-test-secret')"], 2
    )
    assert "synthetic-test-secret" not in result["stdout"]
    assert "REDACTED" in result["stdout"]


def test_unconfigured_identity_offline_and_busy(tmp_path, monkeypatch):
    actions = configured(tmp_path)
    assert "not been provisioned" in actions.execute("desktop.ssh_test")["stderr"]
    monkeypatch.setattr(
        "butters.actions.compute._capture", lambda *args, **kwargs: {"success": False}
    )
    result = actions.execute("desktop.status")
    assert result["success"] and not result["online"] and not result["ssh_reachable"]
    assert not actions.execute("desktop.ping")["success"]
    with actions._lock:
        assert "Another desktop action" in actions.execute("desktop.status")["stderr"]


@pytest.mark.parametrize(
    "contents", ["desktop=1", "[desktop]\nhostname=[]", "commands=1", "projects=[]"]
)
def test_bad_configuration_fails_closed(tmp_path, contents):
    path = tmp_path / "invalid.toml"
    path.write_text(contents)
    path.chmod(0o600)
    actions = DesktopActions(path)
    assert actions.configuration_error
    assert not actions.execute("desktop.status")["success"]


def test_api_authorization_csrf_shared_backend_and_ui(tmp_path, monkeypatch):
    async def scenario():
        app, service = _app(tmp_path)
        service.desktop_actions = configured(tmp_path)
        calls = []

        def remote(command):
            calls.append(command)
            return {
                "success": True,
                "exit_code": 0,
                "stdout": "BUTTERS_DESKTOP_SSH_OK\n",
            }

        monkeypatch.setattr(service.desktop_actions, "_ssh", remote)
        monkeypatch.setattr(
            service.desktop_actions,
            "_status",
            lambda _: {
                "online": False,
                "ssh_reachable": False,
                "success": True,
                "exit_code": 0,
            },
        )
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                assert (await client.get("/api/desktop/status")).status_code == 403
                headers = {"tailscale-user-login": "admin@example.com"}
                session = (await client.get("/api/session", headers=headers)).json()
                mutation = {
                    **headers,
                    "origin": "http://testserver",
                    "x-butters-csrf": session["csrf_token"],
                }
                payload = {"action": "desktop.ssh_test", "parameters": {}}
                assert (
                    await client.post(
                        "/api/desktop/actions", headers=headers, json=payload
                    )
                ).status_code == 403
                assert calls == []
                status = await client.get("/api/desktop/status", headers=headers)
                assert status.status_code == 200 and status.json()["online"] is False
                result = await client.post(
                    "/api/desktop/actions", headers=mutation, json=payload
                )
                assert result.status_code == 200 and result.json()["success"]
                assert calls == ["printf '%s\\n' BUTTERS_DESKTOP_SSH_OK"]
                invalid = await client.post(
                    "/api/desktop/actions",
                    headers=mutation,
                    json={**payload, "parameters": {"command": "whoami"}},
                )
                assert invalid.status_code == 400 and len(calls) == 1
                build = {
                    "action": "desktop.compile",
                    "parameters": {"project": "fixture"},
                }
                assert (
                    await client.post(
                        "/api/desktop/actions", headers=mutation, json=build
                    )
                ).status_code == 403
                assert len(calls) == 1
                # An existing, unexpired passkey elevation unlocks registered work.
                browser = service.sessions.get(client.cookies.get("butters_session"))
                service.auth_state.elevate(browser.session_id, browser.peer_key)
                built = await client.post(
                    "/api/desktop/actions", headers=mutation, json=build
                )
                assert built.status_code == 200 and built.json()["success"]
                assert len(calls) == 2 and "printf build" in calls[-1]
                catalog = (
                    await client.get("/api/desktop/catalog", headers=headers)
                ).json()
                assert catalog["projects"][0]["name"] == "fixture"
                html = (await client.get("/admin", headers=headers)).text
                assert 'id="desktop-ssh-test"' in html and 'id="tool-list"' in html
                assert (await client.get("/healthz")).status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())
