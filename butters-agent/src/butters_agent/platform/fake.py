"""Deterministic backend for tests. Never selected implicitly on Windows."""


class Platform:
    def __init__(self):
        self.active = True
        self.running = set()
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
        return {
            "installed": entry["path"] not in self.missing,
            "running": entry["path"] in self.running,
            "session_id": 1,
            "visible_window": entry["path"] in self.running,
        }

    def launch(self, entry):
        if self.failure:
            raise OSError("test failure")
        self.launches += 1
        self.running.add(entry["path"])

    def validate_registry(self, path):
        if path.stat().st_mode & 0o022:
            raise ValueError("unsafe_registry_permissions")
