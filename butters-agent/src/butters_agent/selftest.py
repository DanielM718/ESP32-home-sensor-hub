"""Explicit operator installation test, not a network/API authorization path.

Run only through a temporary InteractiveToken task. Uses the installed registry
and execution engine; refuses unavailable sessions. Does not unlock Windows,
change credentials or authorize the production Action API.
"""

import json
import os
from pathlib import Path
import time

from .engine import Engine
from .protocol import ProtocolError
from .platform.win32 import Platform


def main():
    platform = Platform()
    engine = Engine(platform, Path(r"C:\ProgramData\Butters\DesktopAgent\apps.toml"))
    report = {"kind": "operator_interactive_installation_test", "started_at": time.time(),
              "pid": os.getpid(), "session": platform.session(), "applications": {}}
    for app in ("git_bash", "parsec", "vs_code"):
        before = engine.invoke("desktop.app.status", {"app": app})
        first = engine.invoke("desktop.app.launch", {"app": app})
        second = engine.invoke("desktop.app.launch", {"app": app})
        report["applications"][app] = {"before": before, "first": first, "second": second}
    try:
        engine.invoke("desktop.app.launch", {"app": r"C:\unregistered.exe"})
        report["arbitrary_path_rejected"] = False
    except ProtocolError:
        report["arbitrary_path_rejected"] = True
    report["completed_at"] = time.time()
    report["physical_visual_confirmation"] = "not performed"
    output = Path(os.environ["LOCALAPPDATA"]) / "ButtersAgent" / "installation-selftest.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
