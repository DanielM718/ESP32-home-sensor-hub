"""Registry-only actions. Transport cannot supply executable paths or arguments."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import PureWindowsPath

import tomllib

from .protocol import NAME, ProtocolError, parameters


class Engine:
    def __init__(self, platform, registry=None):
        self.platform = platform
        self.apps = {}
        self.invalid = {}
        self.lock = threading.Lock()
        if registry is not None:
            platform.validate_registry(registry)
            data = tomllib.loads(registry.read_text(encoding="utf-8-sig"))
            if data.get("schema_version") != 1 or set(data) - {
                "schema_version",
                "apps",
            }:
                raise ValueError("invalid_registry")
            for name, entry in data.get("apps", {}).items():
                if not NAME.fullmatch(name):
                    raise ValueError("invalid_registry")
                try:
                    if (
                        type(entry) is not dict
                        or set(entry) - {"path", "images", "require_visible"}
                        or not {"path", "images"}.issubset(entry)
                        or type(entry.get("require_visible", False)) is not bool
                        or not isinstance(entry["path"], str)
                        or not PureWindowsPath(entry["path"]).is_absolute()
                        or PureWindowsPath(entry["path"]).suffix.lower() != ".exe"
                        or not isinstance(entry["images"], list)
                        or not entry["images"]
                        or any(
                            not isinstance(path, str)
                            or not PureWindowsPath(path).is_absolute()
                            or PureWindowsPath(path).suffix.lower() != ".exe"
                            for path in entry["images"]
                        )
                    ):
                        raise ValueError()
                    self.apps[name] = entry
                except (TypeError, ValueError):
                    self.invalid[name] = "invalid_registry_entry"

    def invoke(self, action, values, cancel=None):
        started = time.time()
        parameters(action, values)
        result = {
            "action": action,
            "target_host": "desktop",
            "started_at": started,
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
        }
        try:
            if cancel is not None and cancel.is_set():
                raise ProtocolError("cancelled")
            result.update(self._invoke(action, values, cancel))
        except ProtocolError as exc:
            result.update(error=str(exc), success=False)
        except subprocess.TimeoutExpired:
            result.update(error="timeout", success=False)
        except (OSError, ValueError):
            result.update(error="platform_error", success=False)
        result["completed_at"] = time.time()
        result["duration_seconds"] = result["completed_at"] - started
        result["exit_code"] = 0 if result["success"] else 1
        return result

    def _invoke(self, action, values, cancel):
        if action in {"desktop.agent.status", "desktop.session.status"}:
            return {"success": True, "session": self.platform.session()}
        if action == "desktop.app.list":
            names = sorted(set(self.apps) | set(self.invalid))
            return {"success": True, "apps": [self._status(name) for name in names]}

        name = values["app"]
        status = self._status(name)
        if action == "desktop.app.status":
            return {"success": True, **status}
        if not status.get("installed"):
            raise ProtocolError(status.get("reason", "app_not_installed"))
        if not self.platform.session()["gui_launch"]:
            raise ProtocolError("session_inactive")
        if not self.lock.acquire(blocking=False):
            raise ProtocolError("busy")
        try:
            status = self._status(name)
            if status["running"]:
                visible = status["visible_window"]
                return {
                    "success": not self.apps[name].get("require_visible") or visible,
                    "state": "already_running",
                    **status,
                    "reason": None
                    if visible
                    else "existing_process_has_no_visible_window",
                }
            if cancel is not None and cancel.is_set():
                raise ProtocolError("cancelled")
            try:
                self.platform.launch(self.apps[name])
            except OSError:
                raise ProtocolError("launch_failed") from None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = self._status(name)
                visible = status["visible_window"]
                if status["running"] and (
                    not self.apps[name].get("require_visible") or visible
                ):
                    return {"success": True, "state": "running", **status}
                if cancel is not None and cancel.wait(0.2):
                    return {
                        "success": False,
                        "error": "cancelled",
                        "side_effect_committed": True,
                    }
                if cancel is None:
                    time.sleep(0.2)
            raise ProtocolError("launch_failed")
        finally:
            self.lock.release()

    def _status(self, name):
        if name in self.invalid:
            return {
                "app": name,
                "installed": False,
                "running": False,
                "reason": self.invalid[name],
            }
        if name not in self.apps:
            raise ProtocolError("unknown_app")
        return {"app": name, **self.platform.app_status(self.apps[name])}
