"""Registry-only actions. Transport cannot supply executable paths or arguments."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import PureWindowsPath

import tomllib

from .protocol import NAME, OPTIONAL, SCHEMAS, ProtocolError, parameters

# A Win32 window class name: letters, digits, dot, underscore, hyphen only.
WINDOW_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")


class Engine:
    def __init__(self, platform, registry=None, kr260=None):
        self.platform = platform
        # Optional KR260 JTAG recovery (butters_agent.kr260); absent by default.
        self.kr260 = kr260
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
                        or set(entry)
                        - {"path", "images", "require_visible", "window_classes"}
                        or not {"path", "images"}.issubset(entry)
                        or type(entry.get("require_visible", False)) is not bool
                        # Optional fixed window-class allowlist. It narrows what
                        # "running" means for an app whose image is a resident
                        # shell component, and can express nothing executable.
                        or not isinstance(entry.get("window_classes", []), list)
                        or any(
                            not isinstance(item, str)
                            or not WINDOW_CLASS.fullmatch(item)
                            for item in entry.get("window_classes", [])
                        )
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

    def actions(self):
        """The hello's action list: optional actions only when enabled locally."""

        enabled = self.kr260.actions() if self.kr260 is not None else frozenset()
        return sorted((set(SCHEMAS) - OPTIONAL) | (set(enabled) & OPTIONAL))

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
        if action in OPTIONAL:
            if action not in self.actions():
                raise ProtocolError("unsupported_action")
            # Runs to completion once started: a reset cannot be taken back.
            return self.kr260.invoke(action)
        if action in {"desktop.agent.status", "desktop.session.status"}:
            return {"success": True, "session": self.platform.session()}
        if action == "desktop.app.list":
            names = sorted(set(self.apps) | set(self.invalid))
            observed = self.platform.app_statuses(self.apps)
            return {
                "success": True,
                "apps": [self._status(name, observed=observed) for name in names],
                "observed_at": time.time(),
            }

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

    def _status(self, name, *, observed=None):
        if name in self.invalid:
            return {
                "app": name,
                "installed": False,
                "running": False,
                "reason": self.invalid[name],
            }
        if name not in self.apps:
            raise ProtocolError("unknown_app")
        status = (
            observed[name]
            if observed is not None
            else self.platform.app_status(self.apps[name])
        )
        return {"app": name, **status}
