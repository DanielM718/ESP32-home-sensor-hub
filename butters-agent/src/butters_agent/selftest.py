"""Explicit local installation test for an interactive Windows session."""

import json
import os
import time
from pathlib import Path

from .engine import Engine
from .platform.win32 import Platform
from .protocol import ProtocolError


def main():
    platform = Platform()
    registry = Path(r"C:\ProgramData\Butters\DesktopAgent\apps.toml")
    engine = Engine(platform, registry)
    report = {
        "kind": "operator_interactive_installation_test",
        "started_at": time.time(),
        "pid": os.getpid(),
        "session": platform.session(),
        "applications": {},
    }
    for app in sorted(engine.apps):
        before = engine.invoke("desktop.app.status", {"app": app})
        first = engine.invoke("desktop.app.launch", {"app": app})
        second = engine.invoke("desktop.app.launch", {"app": app})
        report["applications"][app] = {
            "before": before,
            "first": first,
            "second": second,
        }
    try:
        engine.invoke("desktop.app.launch", {"app": r"C:\unregistered.exe"})
        report["arbitrary_path_rejected"] = False
    except ProtocolError as exc:
        report["arbitrary_path_rejected"] = str(exc) == "invalid_parameter"
    report["completed_at"] = time.time()
    report["physical_visual_confirmation"] = "not performed"
    output = (
        Path(os.environ["LOCALAPPDATA"]) / "ButtersAgent" / "installation-selftest.json"
    )
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
