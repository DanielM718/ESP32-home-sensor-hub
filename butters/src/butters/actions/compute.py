"""Registered headless desktop actions, shared by HTTP and future voice callers.

Only operator-owned TOML supplies commands; requests supply validated names.
SSH credentials and host-key policy belong to an operator-owned SSH config.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import tomllib

from butters.diagnostics.sanitizer import sanitize_text

NAMES = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
ACTIONS = (
    "desktop.status",
    "desktop.ping",
    "desktop.ssh_test",
    "desktop.run_registered",
    "desktop.compile",
    "desktop.test",
    "desktop.build_and_test",
)
OUTPUT_LIMIT = 65536


class ComputeError(ValueError):
    """Invalid or unavailable registered action; never echo request commands."""


class DesktopAgent(Protocol):
    """Future authenticated agent in the logged-in Windows user's session."""

    def status(self) -> dict[str, object]: ...

    def run_registered(self, application: str) -> dict[str, object]: ...

    def snapshot(self) -> dict[str, object]: ...

    def invoke(self, action: str, parameters: dict) -> dict[str, object]: ...


class UnavailableDesktopAgent:
    def status(self) -> dict[str, object]:
        return {
            "available": False,
            "reason": "Windows interactive Desktop Agent is not installed",
        }

    def run_registered(self, application: str) -> dict[str, object]:
        raise ComputeError("GUI actions require the future interactive Desktop Agent")

    def snapshot(self) -> dict[str, object]:
        return self.status()

    def invoke(self, action: str, parameters: dict) -> dict[str, object]:
        raise ComputeError("Interactive Desktop Agent is unavailable")


def _name(value: object) -> str:
    if not isinstance(value, str) or not NAMES.fullmatch(value):
        raise ComputeError("Invalid registered name")
    return value


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class DesktopActions:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.agent = UnavailableDesktopAgent()
        self.config: dict = {}
        self.configuration_error = ""
        self._ssh_verified_at = 0.0
        try:
            if path.stat().st_mode & 0o022:
                raise ComputeError(
                    "Compute configuration must not be group/world writable"
                )
            self.config = tomllib.loads(path.read_text())
            self._validate_config()
        except (OSError, ValueError, TypeError, KeyError):
            self.config = {}
            self.configuration_error = (
                "Desktop compute configuration is missing or invalid"
            )

    def _validate_config(self) -> None:
        if set(self.config) - {"desktop", "commands", "projects"}:
            raise ComputeError("Unknown configuration section")
        desktop = self.config["desktop"]
        if (
            not isinstance(desktop, dict)
            or not isinstance(self.config.get("commands", {}), dict)
            or not isinstance(self.config.get("projects", {}), dict)
        ):
            raise ComputeError("Configuration sections must be tables")
        if set(desktop) != {"hostname", "ssh_config", "timeout_seconds"}:
            raise ComputeError("Invalid desktop configuration")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", desktop["hostname"]):
            raise ComputeError("Invalid hostname")
        if not Path(desktop["ssh_config"]).is_absolute():
            raise ComputeError("SSH config must be absolute")
        if (
            type(desktop["timeout_seconds"]) is not int
            or not 1 <= desktop["timeout_seconds"] <= 120
        ):
            raise ComputeError("Invalid timeout")
        for name, command in self.config.get("commands", {}).items():
            _name(name)
            self._command(command)
        for name, project in self.config.get("projects", {}).items():
            _name(name)
            if not isinstance(project, dict):
                raise ComputeError("Project must be a table")
            if (
                set(project) - {"host", "directory", "build", "test"}
                or project.get("host") != "desktop"
            ):
                raise ComputeError("Invalid project target")
            directory = project["directory"]
            if (
                not isinstance(directory, str)
                or not directory.startswith("/")
                or len(directory) > 1024
                or any(ord(c) < 32 for c in directory)
            ):
                raise ComputeError(
                    "Project directory must be an absolute Git Bash path"
                )
            for operation in ("build", "test"):
                if operation in project:
                    self._command(project[operation])

    @staticmethod
    def _command(value: object) -> None:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 2048
            or "\0" in value
        ):
            raise ComputeError("Invalid registered command")

    def catalog(self) -> dict[str, object]:
        return {
            "actions": list(ACTIONS),
            "hostname": self.config.get("desktop", {}).get(
                "hostname", "DESKTOP-G4CFVL1"
            ),
            "configuration_error": self.configuration_error or None,
            "commands": sorted(self.config.get("commands", {})),
            "projects": [
                {
                    "name": name,
                    "host": "desktop",
                    "directory": project["directory"],
                    "build_available": "build" in project,
                    "test_available": "test" in project,
                }
                for name, project in sorted(self.config.get("projects", {}).items())
            ],
            "desktop_agent": self.agent.status(),
        }

    def execute(self, action: str, parameters: dict | None = None) -> dict[str, object]:
        parameters = {} if parameters is None else parameters
        if (
            not isinstance(action, str)
            or action not in ACTIONS
            or type(parameters) is not dict
        ):
            raise ComputeError("Unknown action or invalid parameters")
        expected = (
            {"command"}
            if action == "desktop.run_registered"
            else {"project"}
            if action in ACTIONS[4:]
            else set()
        )
        if set(parameters) != expected:
            raise ComputeError("Action parameters do not match the registered schema")
        for value in parameters.values():
            _name(value)
        command = None
        if action == "desktop.run_registered":
            command = self.config.get("commands", {}).get(parameters["command"])
            if command is None:
                raise ComputeError("Command is not registered")
        elif action in ACTIONS[4:]:
            project = self.config.get("projects", {}).get(parameters["project"])
            if project is None:
                raise ComputeError("Project is not registered")
            operations = (
                ("build", "test")
                if action == "desktop.build_and_test"
                else ("build",)
                if action == "desktop.compile"
                else ("test",)
            )
            if any(operation not in project for operation in operations):
                raise ComputeError("Requested project operation is unavailable")
            # Separate subshells prevent build scripts changing the test directory.
            command = " && ".join(
                "(cd -- "
                + shlex.quote(project["directory"])
                + " && ( "
                + project[operation]
                + "\n))"
                for operation in operations
            )
        started, tick = _utc(), time.monotonic()
        result = {
            "action": action,
            "parameters": parameters,
            "target_host": "desktop",
            "hostname": self.catalog()["hostname"],
            "started_at": started,
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "output_truncated": False,
            "remote_completion_unknown": False,
        }
        if not self._lock.acquire(blocking=False):
            result["stderr"] = (
                "Another desktop action is running; retry when it completes"
            )
        else:
            try:
                if self.configuration_error:
                    result["stderr"] = self.configuration_error
                elif action in {"desktop.status", "desktop.ping"}:
                    result.update(self._status(action))
                else:
                    if action == "desktop.ssh_test":
                        self._ssh_verified_at = 0.0
                        command = "printf '%s\\n' BUTTERS_DESKTOP_SSH_OK"
                    result.update(self._ssh(command))
                    if action == "desktop.ssh_test" and result["success"]:
                        result["success"] = (
                            result["stdout"].strip() == "BUTTERS_DESKTOP_SSH_OK"
                        )
                        if not result["success"]:
                            result["stderr"] += "\nSSH sentinel mismatch"
                        else:
                            self._ssh_verified_at = time.monotonic()
            finally:
                self._lock.release()
        result.update(
            completed_at=_utc(), duration_seconds=round(time.monotonic() - tick, 3)
        )
        if action == "desktop.status":
            agent = self.agent.status()
            authenticated = bool(result.get("ssh_reachable") and self._ssh_verified_at
                                 and time.monotonic() - self._ssh_verified_at < 60)
            result["desktop_agent"] = agent
            result["host_reachable"] = result.get("online")
            result["ssh_available"] = result.get("ssh_reachable")
            result["agent_connected"] = agent.get("agent_connected", False)
            result["interactive_session"] = agent.get("interactive_session", False)
            result["capabilities"] = {**agent.get("capabilities", {}),
                                      "headless_compute": True if authenticated else None}
            result["axes"] = {
                "power": "RESPONDING" if result.get("online") else "UNKNOWN",
                "network": "REACHABLE" if result.get("online") else "UNREACHABLE",
                "os": "AUTHENTICATED" if authenticated else "SSH_RESPONDING" if result.get("ssh_reachable") else "UNKNOWN",
                "session": agent.get("session", {}).get("state", "UNKNOWN"),
                "agent": "CONNECTED" if agent.get("agent_connected") else "OFFLINE",
            }
        return result

    def _status(self, action: str) -> dict[str, object]:
        host = self.config["desktop"]["hostname"]
        ping = _capture(["/usr/bin/ping", "-c", "1", "-W", "1", host], 3)
        # A subprocess bounds DNS resolution as well as TCP/banner reads.
        # socket timeouts alone do not bound getaddrinfo on a broken resolver.
        probe = _capture(
            [
                sys.executable,
                "-c",
                (
                    "import socket,sys; s=socket.create_connection((sys.argv[1],22),2); "
                    "s.settimeout(2); b=s.makefile('rb').readline(255); "
                    "s.close(); sys.exit(0 if b.startswith(b'SSH-') else 1)"
                ),
                host,
            ],
            5,
        )
        ssh = bool(probe["success"])
        online = bool(ping["success"] or ssh)
        return {
            "success": True if action == "desktop.status" else online,
            "exit_code": 0 if action == "desktop.status" or online else 1,
            "online": online,
            "icmp_reachable": ping["success"],
            "ssh_reachable": ssh,
            "ssh_authenticated": None,
            "status_note": "No network response (offline or name/network unavailable)"
            if not online
            else "SSH authentication is checked by SSH Test",
        }

    def _ssh(self, command: str) -> dict[str, object]:
        desktop = self.config["desktop"]
        if not Path(desktop["ssh_config"]).is_file():
            return {
                "success": False,
                "exit_code": None,
                "stderr": "Desktop SSH identity has not been provisioned",
            }
        return _capture(
            [
                "/usr/bin/ssh",
                "-F",
                desktop["ssh_config"],
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=3",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ForwardAgent=no",
                "-o",
                "ClearAllForwardings=yes",
                "-T",
                "desktop",
                command,
            ],
            desktop["timeout_seconds"],
            remote=True,
        )


def _capture(
    argv: list[str], timeout: int, *, remote: bool = False
) -> dict[str, object]:
    """Drain both pipes with bounded memory/time; never log raw output."""
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    timed_out = truncated = False
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except OSError:
        return {
            "success": False,
            "exit_code": None,
            "stderr": "Could not start desktop transport",
        }
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        for name in buffers:
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        try:
            while selector.get_map() or process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                for key, _ in selector.select(0.05):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.data]
                    remaining = OUTPUT_LIMIT - len(buffer)
                    buffer.extend(data[:remaining])
                    if len(data) > remaining:
                        truncated = True
                if truncated:
                    break
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
    values = {
        name: sanitize_text(
            data.decode("utf-8", errors="replace"), max_bytes=OUTPUT_LIMIT
        ).text
        for name, data in buffers.items()
    }
    if timed_out or truncated:
        values["stderr"] += "\nAction stopped: " + (
            "timeout" if timed_out else "output limit exceeded"
        )
    return {
        **values,
        "success": process.returncode == 0 and not (timed_out or truncated),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "output_truncated": truncated,
        "remote_completion_unknown": remote
        and (timed_out or truncated or process.returncode == 255),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/butters/desktop-compute.toml")
    )
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--project")
    parser.add_argument("--command", help="Registered command name, never shell text")
    args = parser.parse_args()
    params = {
        key: value
        for key, value in {"project": args.project, "command": args.command}.items()
        if value is not None
    }
    try:
        result = DesktopActions(args.config).execute(args.action, params)
    except ComputeError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
