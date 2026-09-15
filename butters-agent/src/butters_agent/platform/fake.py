"""Deterministic backend for tests. Never selected implicitly on Windows."""

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
        if path.stat().st_mode & 0o022:
            raise ValueError("unsafe_registry_permissions")
