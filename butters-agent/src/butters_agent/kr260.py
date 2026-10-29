"""KR260 JTAG recovery: two fixed actions that run one hash-pinned wrapper.

Nothing on the wire selects a path, cable or argument. The wrapper and its Tcl
script live in an admin-only directory and must match the SHA-256 values in the
agent-local configuration before every run. Both actions refuse while this
desktop can still ping the board: attaching a debugger to a running KR260 with
deep idle enabled hangs it (butters/KR260_MANAGEMENT.md), and a board that
answers needs no power-on reset. The actions are advertised only when the
local configuration enables them, so a default hello is unchanged.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import threading
import time
from pathlib import PureWindowsPath

import tomllib

from .protocol import ProtocolError

CHECK = "kr260.jtag_por.check"
RESET = "kr260.jtag_por.reset"
ACTIONS = frozenset({CHECK, RESET})

DIRECTORY = PureWindowsPath(r"C:\ProgramData\Butters\kr260")
WRAPPER = DIRECTORY / "kr260-jtag-por.ps1"
SCRIPT = DIRECTORY / "kr260-jtag-por.tcl"
# The wrapper refuses to start while any of these runs; checked here as well so
# a refusal never starts PowerShell.
DEBUGGERS = frozenset(
    {"hw_server.exe", "xsdb.exe", "rdi_xsdb.exe", "cs_server.exe", "vivado.exe"}
)
# The wrapper's own XSDB deadline. The attach-only check took 14.6 s on
# 2026-10-29. The whole action must finish inside the 30 s request window:
# up to PREFLIGHT_SECONDS of ping, ACL, hash and process-list checks, the run,
# then up to CLEANUP_SECONDS of debugger cleanup.
WRAPPER_TIMEOUT_SECONDS = 17
RUN_TIMEOUT_SECONDS = 20
PREFLIGHT_SECONDS = 8
CLEANUP_SECONDS = 2
# One reset attempt per half hour from this agent, whatever Butters asks.
RESET_INTERVAL_SECONDS = 1800.0

CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "enabled",
        "cable_serial",
        "board_address",
        "wrapper_sha256",
        "script_sha256",
    }
)
SERIAL = re.compile(r"[A-Z0-9]{12}A\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
# The wrapper's last line: "<UTC>Z RESULT <CODE> [at=<UTC>Z] ...".
_STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z"
_RESULT = re.compile(rf"^{_STAMP} RESULT ([A-Z_]+)\b(?:\s+at=({_STAMP}))?")
RESULT_CODES = frozenset(
    {
        "CHECK_OK",
        "RST_ISSUED",
        "RST_ERROR",
        "CONNECT_FAILED",
        "REFUSED_USAGE",
        "REFUSED_UNCONFIRMED",
        "REFUSED_CHAIN",
        "REFUSED_TARGET",
        "REFUSED_REPEAT",
        "REFUSED_DEBUGGER_ACTIVE",
        "REFUSED_MISSING_SCRIPT",
        "NO_RESULT",
    }
)
SUCCESS = {CHECK: "CHECK_OK", RESET: "RST_ISSUED"}
# Codes after which the POR line may have been pulsed.
_MAY_HAVE_RESET = frozenset({"RST_ISSUED", "RST_ERROR", "NO_RESULT"})


def load_config(text):
    data = tomllib.loads(text)
    if (
        set(data) != CONFIG_KEYS
        or data["schema_version"] != 1
        or type(data["enabled"]) is not bool
        or not isinstance(data["cable_serial"], str)
        or not SERIAL.fullmatch(data["cable_serial"])
        or not isinstance(data["board_address"], str)
        or any(
            not isinstance(data[key], str) or not SHA256.fullmatch(data[key])
            for key in ("wrapper_sha256", "script_sha256")
        )
    ):
        raise ValueError("invalid_kr260_config")
    address = ipaddress.IPv4Address(data["board_address"])
    if not address.is_private or str(address) != data["board_address"]:
        raise ValueError("invalid_kr260_config")
    return dict(data)


def parse_result(output):
    """The wrapper's last RESULT code and reset time; anything else is NO_RESULT."""

    code, at = "NO_RESULT", None
    for line in output.splitlines():
        match = _RESULT.match(line.strip())
        if match:
            code, at = match[1], match[2]
    if code not in RESULT_CODES:
        return "NO_RESULT", None
    return code, at if code == "RST_ISSUED" else None


class Kr260Jtag:
    def __init__(self, platform, config, monotonic=time.monotonic):
        self.platform = platform
        self.config = config
        self.monotonic = monotonic
        self.lock = threading.Lock()
        self.last_reset = None

    def actions(self):
        return ACTIONS if self.config["enabled"] else frozenset()

    def invoke(self, action):
        if action not in self.actions():
            raise ProtocolError("unsupported_action")
        if not self.lock.acquire(blocking=False):
            raise ProtocolError("busy")
        try:
            if (
                action == RESET
                and self.last_reset is not None
                and self.monotonic() - self.last_reset < RESET_INTERVAL_SECONDS
            ):
                raise ProtocolError("rate_limited")
            # Cheap refusal first, before the slower ACL and hash checks.
            self._require_dark()
            self._verify_files()
            if self.platform.debuggers_running(DEBUGGERS):
                raise ProtocolError("debugger_active")
            # Again immediately before the wrapper starts: the board may have
            # come back while the checks above ran. What remains is the
            # wrapper's own attach and chain scan (about 10 s) before rst -por.
            self._require_dark()
            arguments = [
                "-Mode",
                "check" if action == CHECK else "por",
                "-CableSerial",
                self.config["cable_serial"],
                "-TimeoutSeconds",
                str(WRAPPER_TIMEOUT_SECONDS),
            ]
            if action == RESET:
                arguments += ["-Confirm", "confirm-por"]
                # An attempt counts even if it fails or times out.
                self.last_reset = self.monotonic()
            try:
                returncode, output = self.platform.run_wrapper(
                    WRAPPER, arguments, RUN_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                # The platform has already killed the PowerShell process tree.
                returncode, output = None, ""
            cleanup = self._cleanup()
        finally:
            self.lock.release()
        code, at = parse_result(output)
        success = returncode == 0 and code == SUCCESS[action]
        result = {"success": success, "jtag_result": code, "debugger_cleanup": cleanup}
        if not success:
            result["error"] = "jtag_failed"
        if action == RESET:
            result["reset_may_have_run"] = code in _MAY_HAVE_RESET
            result["reset_issued_at"] = at
        return result

    def _require_dark(self):
        if self.platform.host_answers_ping(self.config["board_address"]):
            raise ProtocolError("board_reachable")

    def _cleanup(self):
        """No debugger may stay attached while the board boots.

        None ran before this run started, so any debugger process now is this
        run's. The wrapper normally stops them itself ("clean").
        """

        if not self.platform.debuggers_running(DEBUGGERS):
            return "clean"
        self.platform.stop_debuggers(DEBUGGERS)
        return "failed" if self.platform.debuggers_running(DEBUGGERS) else "forced"

    def _verify_files(self):
        for path, expected in (
            (WRAPPER, self.config["wrapper_sha256"]),
            (SCRIPT, self.config["script_sha256"]),
        ):
            try:
                # Owner and writers limited to SYSTEM and Administrators.
                self.platform.validate_registry(path)
                actual = self.platform.file_sha256(path)
            except (OSError, ValueError):
                raise ProtocolError("integrity_failed") from None
            if actual != expected:
                raise ProtocolError("integrity_failed")
