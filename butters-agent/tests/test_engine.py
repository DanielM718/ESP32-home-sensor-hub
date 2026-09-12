from __future__ import annotations

import threading

import pytest
from butters_agent.engine import Engine
from butters_agent.platform.fake import Platform
from butters_agent.protocol import ProtocolError


@pytest.fixture
def engine():
    instance = Engine(Platform())
    instance.apps = {
        "git_bash": {
            "path": r"C:\Program Files\Git\git-bash.exe",
            "images": [r"C:\Program Files\Git\usr\bin\mintty.exe"],
        },
        "parsec": {
            "path": r"C:\Program Files\Parsec\parsecd.exe",
            "images": [r"C:\Program Files\Parsec\parsecd.exe"],
        },
    }
    return instance


def write_registry(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)


def test_registry_loads_only_symbolic_allowlisted_applications(tmp_path):
    registry = tmp_path / "apps.toml"
    write_registry(
        registry,
        """schema_version = 1
[apps.git_bash]
path = 'C:\\Program Files\\Git\\git-bash.exe'
images = ['C:\\Program Files\\Git\\usr\\bin\\mintty.exe']
require_visible = true
""",
    )

    loaded = Engine(Platform(), registry)

    assert set(loaded.apps) == {"git_bash"}
    assert loaded.apps["git_bash"]["require_visible"] is True


def test_invalid_registry_entry_is_quarantined(tmp_path):
    registry = tmp_path / "apps.toml"
    write_registry(
        registry,
        """schema_version = 1
[apps.safe_name]
path = 'relative.exe'
images = ['C:\\Program Files\\App\\app.exe']
""",
    )

    loaded = Engine(Platform(), registry)

    assert loaded.apps == {}
    assert loaded.invalid == {"safe_name": "invalid_registry_entry"}
    listed = loaded.invoke("desktop.app.list", {})
    assert listed["apps"][0]["reason"] == "invalid_registry_entry"


def test_invalid_registry_name_fails_closed(tmp_path):
    registry = tmp_path / "apps.toml"
    write_registry(
        registry,
        """schema_version = 1
[apps."bad;name"]
path = 'C:\\Program Files\\App\\app.exe'
images = ['C:\\Program Files\\App\\app.exe']
""",
    )

    with pytest.raises(ValueError, match="invalid_registry"):
        Engine(Platform(), registry)


def test_unregistered_application_is_rejected(engine):
    result = engine.invoke("desktop.app.status", {"app": "unknown"})
    assert result["success"] is False
    assert result["error"] == "unknown_app"
    assert engine.platform.launches == 0


def test_fake_platform_launch_and_already_running_behavior(engine):
    first = engine.invoke("desktop.app.launch", {"app": "git_bash"})
    second = engine.invoke("desktop.app.launch", {"app": "git_bash"})

    assert first["success"] is True
    assert first["state"] == "running"
    assert second["success"] is True
    assert second["state"] == "already_running"
    assert engine.platform.launches == 1


def test_session_missing_application_and_launch_failure(engine):
    engine.platform.active = False
    assert (
        engine.invoke(
            "desktop.app.launch",
            {"app": "parsec"},
        )["error"]
        == "session_inactive"
    )

    engine.platform.active = True
    engine.platform.missing.add(engine.apps["parsec"]["path"])
    assert (
        engine.invoke(
            "desktop.app.launch",
            {"app": "parsec"},
        )["error"]
        == "app_not_installed"
    )

    engine.platform.missing.clear()
    engine.platform.failure = True
    assert (
        engine.invoke(
            "desktop.app.launch",
            {"app": "parsec"},
        )["error"]
        == "launch_failed"
    )


def test_cancel_before_launch_has_no_side_effect(engine):
    cancel = threading.Event()
    cancel.set()

    result = engine.invoke("desktop.app.launch", {"app": "parsec"}, cancel)

    assert result["error"] == "cancelled"
    assert engine.platform.launches == 0


def test_transport_cannot_supply_an_executable_path(engine):
    with pytest.raises(ProtocolError, match="invalid_parameter"):
        engine.invoke("desktop.app.launch", {"app": r"C:\unregistered.exe"})
    assert engine.platform.launches == 0
