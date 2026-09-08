#!/usr/bin/env python3
"""Read-only noninteractive acceptance checks for the desktop SSH alias.

For interactive acceptance, run `ssh desktop` from a terminal and verify
`printf '%s\\n' "$-"` includes i, then run uname/pwd/git/python and exit.
Windows ConPTY requires a live terminal; piped stdin can close before startup.
"""

import json
import subprocess


def check(name, command, code=0, contains=None, stderr_contains=None):
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "desktop", command],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == code, (name, result.returncode, result.stderr)
    if contains:
        assert contains in result.stdout, (name, result.stdout)
    if stderr_contains:
        assert stderr_contains in result.stderr, (name, result.stderr)
    print(
        json.dumps(
            {
                "check": name,
                "passed": True,
                "exit_code": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        )
    )


check("sentinel", "echo BUTTERS_DESKTOP_SSH_OK", contains="BUTTERS_DESKTOP_SSH_OK")
check("Git Bash", "uname -s", contains="MINGW64_NT")
check("working directory", "pwd", contains="/c/Users/Daniel")
check("Git", "git --version", contains="git version")
check("Python", "python --version", contains="Python 3.")
check("exit propagation", "exit 23", code=23)
check(
    "stderr propagation",
    "printf STDERR_SENTINEL >&2; exit 7",
    code=7,
    stderr_contains="STDERR_SENTINEL",
)
check(
    "literal quoting",
    "printf '%s\\n' 'literal $HOME; & | spaces' \"apostrophe: '\"",
    contains="literal $HOME; & | spaces\napostrophe: '",
)
check(
    "path containing spaces",
    "cd -- '/c/Program Files/Git' && pwd && './bin/bash.exe' -c 'printf SPACE_PATH_OK'",
    contains="/c/Program Files/Git\nSPACE_PATH_OK",
)
