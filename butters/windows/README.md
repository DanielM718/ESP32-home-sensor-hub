# Fixed Windows desktop prerequisites

These files support the single desktop configured in `assistant.toml` and the
root broker. They are not a general Windows execution surface.

## Desktop control helper

Run `install-desktop-control.ps1` from an elevated Windows PowerShell with
`desktop-control.ps1` beside it. The installer:

- backs up any previous helper, task definitions, and the Parsec service mode;
- installs `C:\ProgramData\Butters\desktop-control.ps1`;
- creates only `\Butters\LockDesktop` and `\Butters\SleepDesktop`;
- explicitly leaves the fixed `Parsec` service in Manual startup mode and does
  not start it as an installer side effect.

The helper accepts a `ValidateSet` of fixed operations. The Linux broker still
selects each operation from its own enum and never accepts an operation argument
from a caller.

To roll back, use the timestamped directory printed by the installer:

1. Restore `desktop-control.ps1.previous` if it exists; otherwise remove the
   installed helper.
2. Restore task XML files with `Register-ScheduledTask -Xml`, or unregister the
   two tasks if no prior XML exists.
3. Restore the Parsec mode recorded in `parsec-service-before.json`; for the
   reviewed Manual baseline use `Set-Service Parsec -StartupType Manual`.

`ParsecEnsure` and `ParsecRestart` may start the fixed service on demand. Neither
operation changes its startup type, creates a Run key, or creates a scheduled
startup task. Waking the desktop and changing monitor power are separate broker
operations.

## Human Git Bash SSH

The safe design leaves Windows OpenSSH's global `DefaultShell` unset, preserving
`cmd.exe` for the dedicated Butters automation key. A separate human public key
is added to `C:\ProgramData\ssh\administrators_authorized_keys` with a forced
command pointing to `C:\Users\Daniel\.ssh\human-ssh-launcher.ps1`.

The launcher starts Git Bash for an interactive human PTY, runs human
noninteractive commands through Git Bash, and preserves the SFTP subsystem. It
is intentionally unrestricted only for that human administrator key. It is not
used by the broker key.

Before editing `administrators_authorized_keys`, make a byte-for-byte backup and
validate both key fingerprints after the append. To roll back, restore that
backup, remove `human-ssh-launcher.ps1`, and restore the prior `.bashrc` backup.
