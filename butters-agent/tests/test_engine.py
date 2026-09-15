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


# ---------------------------------------------------------------------------
# File Explorer: an allow-listed application whose image is the Windows shell.
# ---------------------------------------------------------------------------

EXPLORER = r"C:\Windows\explorer.exe"
FOLDER_CLASSES = ["CabinetWClass", "ExploreWClass"]
# Windows shell surfaces that exist for the whole session and must never be
# mistaken for an open File Explorer window.
SHELL_CLASSES = ["Progman", "WorkerW", "Shell_TrayWnd"]


@pytest.fixture
def explorer_engine():
    instance = Engine(Platform())
    instance.apps = {
        "file_explorer": {
            "path": EXPLORER,
            "images": [EXPLORER],
            "require_visible": True,
            "window_classes": list(FOLDER_CLASSES),
        }
    }
    return instance


def test_resident_shell_process_alone_is_not_file_explorer_running(explorer_engine):
    """The whole point: explorer.exe is always resident, so it proves nothing.

    Reporting File Explorer as running because the shell process exists would be
    permanently true, which would make status and already-running dishonest.
    """

    platform = explorer_engine.platform
    platform.running.add(EXPLORER)
    platform.windows[EXPLORER] = list(SHELL_CLASSES)

    status = explorer_engine.invoke("desktop.app.status", {"app": "file_explorer"})
    assert status["running"] is False
    assert status["visible_window"] is False
    assert status["installed"] is True


def test_open_folder_window_makes_file_explorer_running(explorer_engine):
    platform = explorer_engine.platform
    platform.running.add(EXPLORER)
    platform.windows[EXPLORER] = [*SHELL_CLASSES, "CabinetWClass"]

    status = explorer_engine.invoke("desktop.app.status", {"app": "file_explorer"})
    assert status["running"] is True
    assert status["visible_window"] is True


def test_legacy_explorer_window_class_also_counts(explorer_engine):
    platform = explorer_engine.platform
    platform.running.add(EXPLORER)
    platform.windows[EXPLORER] = ["ExploreWClass"]

    assert explorer_engine.invoke(
        "desktop.app.status", {"app": "file_explorer"}
    )["running"] is True


def test_file_explorer_launch_opens_a_window_then_reports_already_running(
    explorer_engine,
):
    platform = explorer_engine.platform
    # The shell is already resident before any launch, as it always is.
    platform.running.add(EXPLORER)
    platform.windows[EXPLORER] = list(SHELL_CLASSES)

    first = explorer_engine.invoke("desktop.app.launch", {"app": "file_explorer"})
    assert first["success"] is True
    assert first["state"] == "running"
    assert first["visible_window"] is True
    assert platform.launches == 1

    second = explorer_engine.invoke("desktop.app.launch", {"app": "file_explorer"})
    assert second["success"] is True
    assert second["state"] == "already_running"
    # Truthful idempotence: no second launch was performed.
    assert platform.launches == 1


def test_ordinary_application_semantics_are_unchanged(engine):
    """Git Bash, Parsec and VS Code declare no window classes and must not move."""

    for name in ("git_bash", "parsec"):
        assert "window_classes" not in engine.apps[name]
        before = engine.invoke("desktop.app.status", {"app": name})
        assert before["running"] is False
        launched = engine.invoke("desktop.app.launch", {"app": name})
        assert launched["state"] == "running"
        again = engine.invoke("desktop.app.status", {"app": name})
        assert again["running"] is True


def test_unknown_application_still_fails_closed(explorer_engine):
    """Adding a status strategy must not widen what may be named."""

    assert (
        explorer_engine.invoke("desktop.app.status", {"app": "notepad"})["error"]
        == "unknown_app"
    )
    assert (
        explorer_engine.invoke("desktop.app.launch", {"app": "regedit"})["error"]
        == "unknown_app"
    )
    assert explorer_engine.platform.launches == 0


def test_registry_accepts_window_classes_and_rejects_abuse(tmp_path):
    registry = tmp_path / "apps.toml"
    write_registry(
        registry,
        """schema_version = 1
[apps.file_explorer]
path = 'C:\\Windows\\explorer.exe'
images = ['C:\\Windows\\explorer.exe']
require_visible = true
window_classes = ['CabinetWClass', 'ExploreWClass']
""",
    )
    loaded = Engine(Platform(), registry)
    assert loaded.apps["file_explorer"]["window_classes"] == [
        "CabinetWClass",
        "ExploreWClass",
    ]
    assert loaded.invalid == {}

    # A window class can express nothing executable: anything path-like,
    # argument-like, or otherwise non-symbolic is refused entry.
    for rejected in (
        "'C:\\\\Windows\\\\system32\\\\cmd.exe'",
        "'Cabinet WClass'",
        "'../Cabinet'",
        "'Cabinet;calc'",
        "''",
        "42",
    ):
        hostile = tmp_path / f"apps-{abs(hash(rejected))}.toml"
        write_registry(
            hostile,
            "schema_version = 1\n"
            "[apps.file_explorer]\n"
            "path = 'C:\\Windows\\explorer.exe'\n"
            "images = ['C:\\Windows\\explorer.exe']\n"
            f"window_classes = [{rejected}]\n",
        )
        assert Engine(Platform(), hostile).invalid == {
            "file_explorer": "invalid_registry_entry"
        }, rejected


def test_registry_still_rejects_argv_and_non_executable_targets(tmp_path):
    """The allowlist gains a status hint, not a way to name a command."""

    registry = tmp_path / "apps.toml"
    write_registry(
        registry,
        """schema_version = 1
[apps.file_explorer]
path = 'C:\\Windows\\explorer.exe'
images = ['C:\\Windows\\explorer.exe']
args = ['C:\\Users']
""",
    )
    assert Engine(Platform(), registry).invalid == {
        "file_explorer": "invalid_registry_entry"
    }
