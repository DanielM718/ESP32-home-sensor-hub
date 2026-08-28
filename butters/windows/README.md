# Fixed Windows desktop prerequisites

These files support the single desktop configured in `assistant.toml` and the
root broker. They are not a general Windows execution surface.

## Desktop control helper

Run `install-desktop-control.ps1` from an elevated Windows PowerShell with
`desktop-control.ps1` beside it. The installer:

- backs up any previous helper, task definitions, and the Parsec service mode;
- installs `C:\ProgramData\Butters\desktop-control.ps1`;
- creates only `\Butters\LockDesktop` and `\Butters\SleepDesktop`;
- changes only the fixed `Parsec` service from its observed Manual mode to
  Automatic and starts it.

The helper accepts a `ValidateSet` of fixed operations. The Linux broker still
selects each operation from its own enum and never accepts an operation argument
from a caller.

To roll back, use the timestamped directory printed by the installer:

1. Restore `desktop-control.ps1.previous` if it exists; otherwise remove the
   installed helper.
2. Restore task XML files with `Register-ScheduledTask -Xml`, or unregister the
   two tasks if no prior XML exists.
3. Restore the Parsec mode recorded in `parsec-service-before.json` (the August
   2026 baseline was Manual) with `Set-Service Parsec -StartupType Manual`.

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
