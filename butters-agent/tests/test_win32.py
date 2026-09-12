from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

from butters_agent.platform import win32


def test_powershell_uses_encoded_argument_without_a_shell(monkeypatch):
    captured = {}
    script = 'Write-Output "\'; Stop-Computer; #"'

    def run(arguments, **options):
        captured["arguments"] = arguments
        captured["options"] = options
        return SimpleNamespace(returncode=0, stdout=b"true")

    monkeypatch.setattr(win32.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(win32.subprocess, "run", run)

    assert win32.powershell(script) is True
    arguments = captured["arguments"]
    assert arguments[:-1] == [
        win32.PS,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
    ]
    assert script not in arguments
    assert base64.b64decode(arguments[-1]).decode("utf-16-le") == script
    assert captured["options"].get("shell", False) is False


def test_registry_path_is_base64_encoded_before_powershell(monkeypatch):
    scripts = []
    registry = Path("registry'); Stop-Computer; #.toml")

    def powershell(script):
        scripts.append(script)
        return True

    monkeypatch.setattr(win32, "powershell", powershell)
    win32.Platform.validate_registry(object(), registry)

    assert len(scripts) == 1
    assert str(registry) not in scripts[0]
    encoded = base64.b64encode(str(registry).encode("utf-8")).decode("ascii")
    assert encoded in scripts[0]


def test_launch_uses_single_registry_executable_with_shell_disabled(monkeypatch):
    captured = {}
    executable = r"C:\Program Files\Safe App\safe.exe"

    class Kernel:
        @staticmethod
        def ProcessIdToSessionId(process_id, session_id):
            return False

    class Platform:
        session_id = SimpleNamespace(value=1)
        kernel = Kernel()

        @staticmethod
        def session():
            return {"gui_launch": True}

    def popen(arguments, **options):
        captured["arguments"] = arguments
        captured["options"] = options
        return SimpleNamespace(pid=42)

    monkeypatch.setattr(win32.subprocess, "DETACHED_PROCESS", 0, raising=False)
    monkeypatch.setattr(
        win32.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0,
        raising=False,
    )
    monkeypatch.setattr(win32.subprocess, "Popen", popen)

    win32.Platform.launch(Platform(), {"path": executable})

    assert captured["arguments"] == [executable]
    assert captured["options"]["shell"] is False
    assert "powershell" not in " ".join(captured["arguments"]).lower()
