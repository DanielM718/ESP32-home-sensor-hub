"""Deterministic backend for tests. Never selected implicitly on Windows."""

import hashlib
from pathlib import Path

from .win32 import resolve_application_state


class Platform:
    def __init__(self):
        self.active = True
        self.running = set()
        # Visible top-level window classes per image path, so the fake resolves
        # "running" through the same rule the Windows backend uses. A resident
        # shell image with no allow-listed window must not read as running.
        self.windows = {}
        self.launches = 0
        self.missing = set()
        self.failure = False
        # KR260 JTAG recovery: file contents by path, ACL failures, and the
        # scripted wrapper outcome (returncode, stdout) or exception.
        self.files = {}
        self.unprotected = set()
        self.debuggers = set()
        self.board_pings = False
        self.wrapper_outcome = (1, "")
        self.wrapper_calls = []

    def session(self):
        return {
            "state": "ACTIVE" if self.active else "NONE",
            "session_id": 1,
            "interactive_session": self.active,
            "gui_launch": self.active,
        }

    def app_status(self, entry):
        path = entry["path"]
        process_ids = {1} if path in self.running else set()
        visible = [(1, name) for name in self.windows.get(path, ())]
        running, visible_window = resolve_application_state(
            process_ids, visible, entry.get("window_classes", ())
        )
        return {
            "installed": path not in self.missing,
            "running": running,
            "session_id": 1,
            "visible_window": visible_window,
        }

    def app_statuses(self, entries):
        return {name: self.app_status(entry) for name, entry in entries.items()}

    def launch(self, entry):
        if self.failure:
            raise OSError("test failure")
        self.launches += 1
        self.running.add(entry["path"])
        classes = entry.get("window_classes", ())
        self.windows.setdefault(entry["path"], []).append(
            classes[0] if classes else "FakeAppWindow"
        )

    def validate_registry(self, path):
        if str(path) in self.unprotected:
            raise ValueError("unsafe_registry_permissions")
        if str(path) in self.files:
            return
        if not isinstance(path, Path):
            raise OSError("missing")
        if path.stat().st_mode & 0o022:
            raise ValueError("unsafe_registry_permissions")

    def file_sha256(self, path):
        if str(path) not in self.files:
            raise OSError("missing")
        return hashlib.sha256(self.files[str(path)]).hexdigest()

    def debuggers_running(self, images):
        return bool(self.debuggers & images)

    def host_answers_ping(self, address):
        return self.board_pings

    def run_wrapper(self, path, arguments, timeout):
        self.wrapper_calls.append((str(path), list(arguments), timeout))
        if isinstance(self.wrapper_outcome, BaseException):
            raise self.wrapper_outcome
        return self.wrapper_outcome
