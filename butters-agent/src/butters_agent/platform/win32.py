"""Windows boundary. Fixed local queries only; wire data never becomes script text."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess

PS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                  r"System32\WindowsPowerShell\v1.0\powershell.exe")


def powershell(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run([PS, "-NoLogo", "-NoProfile", "-NonInteractive",
                             "-EncodedCommand", encoded], capture_output=True,
                            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise OSError("windows_query_failed")
    text = result.stdout.decode("utf-8-sig", errors="replace").strip()
    return json.loads(text) if text else None


class Blob(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def dpapi(data: bytes, *, protect: bool) -> bytes:
    """User scope only. Called in Daniel's own logon context, never machine scope."""
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately no LOCAL_MACHINE flag.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError("credential_unavailable")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.WinDLL("kernel32").LocalFree(output.data)


class Platform:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.user.IsWindowVisible.argtypes = [w.HWND]
        self.user.EnumWindows.argtypes = [ctypes.c_void_p, w.LPARAM]
        self.session_id = w.DWORD()
        if not self.kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(self.session_id)):
            raise OSError("session_query_failed")

    def session(self):
        console = self.kernel.WTSGetActiveConsoleSessionId()
        self.user.OpenInputDesktop.restype = w.HANDLE
        desktop = self.user.OpenInputDesktop(0, False, 0x0001)
        unlocked = False
        if desktop:
            try:
                name = ctypes.create_unicode_buffer(256)
                length = w.DWORD()
                self.user.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int,
                    w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]
                if self.user.GetUserObjectInformationW(desktop, 2, name,
                        ctypes.sizeof(name), ctypes.byref(length)):
                    unlocked = name.value.lower() == "default"
            finally:
                self.user.CloseDesktop.argtypes = [w.HANDLE]
                self.user.CloseDesktop(desktop)
        interactive = self.session_id.value != 0 and console == self.session_id.value
        # A second active/disconnected interactive session is ambiguous: fail closed.
        class SessionInfo(ctypes.Structure):
            _fields_ = [("id", w.DWORD), ("station", w.LPWSTR), ("state", ctypes.c_int)]
        terminal = ctypes.WinDLL("wtsapi32")
        sessions = ctypes.POINTER(SessionInfo)()
        count = w.DWORD()
        multiple = True  # A failed enumeration must not grant GUI capability.
        if terminal.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(sessions), ctypes.byref(count)):
            try:
                multiple = sum(1 for item in sessions[:count.value]
                               if item.id != 0 and item.state in (0, 4)) > 1
            finally:
                terminal.WTSFreeMemory(sessions)
        return {"state": "MULTIPLE" if multiple else "ACTIVE" if interactive and unlocked else
                "LOCKED" if interactive else "NONE",
                "session_id": self.session_id.value, "interactive_session": interactive,
                "gui_launch": interactive and unlocked and not multiple, "observed_at": __import__("time").time()}

    def app_status(self, entry):
        # WMI supplies owner as well as session; an unrelated user's process is not ours.
        processes = powershell(r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$sid=(Get-Process -Id $PID).SessionId
$result=@(Get-CimInstance Win32_Process -Filter "SessionId=$sid" | ForEach-Object {
  if ($_.ExecutablePath) {
    $owner=Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction SilentlyContinue
    if ($owner.User -eq $env:USERNAME) {
      @{pid=[int]$_.ProcessId;path=$_.ExecutablePath;session_id=[int]$_.SessionId}
    }
  }
})
ConvertTo-Json -InputObject $result -Compress
''') or []
        images = {os.path.normcase(p) for p in entry["images"]}
        matching = [p for p in processes if os.path.normcase(p["path"]) in images
                    and p["session_id"] == self.session_id.value]
        pids = {p["pid"] for p in matching}
        windows = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        @callback_type
        def visit(hwnd, _):
            pid = w.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and self.user.IsWindowVisible(hwnd):
                windows.append(pid.value)
            return True
        self.user.EnumWindows(visit, 0)
        return {"installed": Path(entry["path"]).is_file(), "running": bool(matching),
                "pids": sorted(pids), "session_id": self.session_id.value,
                "visible_window": bool(windows)}

    def launch(self, entry):
        if not self.session()["gui_launch"]:
            raise OSError("session_inactive")
        process = subprocess.Popen([entry["path"]], cwd=str(Path(entry["path"]).parent),
            shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        sid = w.DWORD()
        if (self.kernel.ProcessIdToSessionId(process.pid, ctypes.byref(sid))
                and sid.value != self.session_id.value):
            raise OSError("wrong_session")

    def validate_registry(self, path):
        # Path is a fixed local installation input, never supplied over the transport.
        encoded = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
        safe = powershell(r'''
$ErrorActionPreference='Stop'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(''' + "'" + encoded + "'" + r'''))
$safe=$true
foreach($target in @($p,(Split-Path $p))) {
  $acl=Get-Acl -LiteralPath $target
  $owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
  if($owner -notin @('S-1-5-18','S-1-5-32-544')) {$safe=$false}
  foreach($rule in $acl.Access) {
    $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if($rule.AccessControlType -eq 'Allow' -and
       (([int]$rule.FileSystemRights -band 0xD0156) -ne 0) -and
       $sid -notin @('S-1-5-18','S-1-5-32-544')) {$safe=$false}
  }
}
ConvertTo-Json -InputObject $safe -Compress
''')
        if safe is not True:
            raise ValueError("unsafe_registry_permissions")
