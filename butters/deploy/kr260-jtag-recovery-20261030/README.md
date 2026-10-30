# KR260 JTAG recovery: gated deployment kit (prepared 2026-10-30, not run)

Nothing here has been executed against production or Windows. Each mutating
step needs the owner's explicit approval and refuses without `--authorized` /
`-Authorized`.

## A. Butters (Pi): candidate `040ca84`

`./butters-candidate-deploy.sh precheck`. This is read-only and passed 26/26
on 2026-10-30.

Then, once approved:

```
./butters-candidate-deploy.sh backup              # /var/backups/butters-predeploy-<UTC>
./butters-candidate-deploy.sh deploy --authorized # switch deploy worktree, install-beta1 --start, restart broker, postcheck
```

| Changes | Does not change |
|---|---|
| `/opt/butters` → candidate tree; `/opt/butters.previous` → `ba675b4` (dbb5e02 is retired) | `/etc/butters/action-broker.toml` (sha `3df6ae06…`), `/etc/butters/assistant.local.toml` (sha `15687256…`) |
| restarts `butters-web` (the Desktop Agent WebSocket drops and reconnects) and `butters-action-broker` (FPGA telemetry lines) | `butters-agent-ingress` (TLS proxy, code unchanged), `butters-live`, the unit files (byte-identical) |
| deploy worktree `~/worktrees/kr260-management-recovery-deploy` → branch `deploy/kr260-jtag-recovery-candidate` | shipped `config/assistant.toml`, `requirements.txt` (identical) |

The gates after deploy were verified with the candidate's code against the
production files. `install-beta1`'s own guard reports "preserves all 50
declared approvals".

| Gate | Value after deploy |
|---|---|
| `kr260.status` (KR260 monitoring) | `true` |
| `kr260.reboot` (normal kexec restart) | `false` |
| `desktop.wake` | `false` |
| `actions.kr260_reboot` | disabled |
| `actions.kr260_jtag_recovery` | disabled, absent from the overlay |
| KR260 monitoring in the web app | on |

`postcheck` then expects:
- the deployed commit and tree digest;
- the rollback tree is `ba675b4`;
- the gates above;
- the agent reconnected and no tracebacks;
- the KR260 online and verified, FPGA `operating`, and the JTAG gate `[false, false]`.

Rollback: `./butters-candidate-deploy.sh rollback --authorized`. It swaps
`/opt/butters.previous` back, restarts `butters-web` and
`butters-action-broker`, and verifies `ba675b4` and its digest. **First make
sure the Desktop Agent's JTAG gate is off**: production `ba675b4` rejects a
hello that advertises the JTAG actions.

## B. Desktop Agent (Windows): `windows-bundle/`

Do this after A, and only while Codex is not using Windows.

**Native preflight (2026-10-30 03:20Z).** It ran from a temporary copy, and
Windows was left unchanged afterwards.
- `-Stage Check`: all 14 checks passed.
- `Install` and `Rollback` both refuse without `-Authorized`.
- The ACL plan, simulated on a scratch folder: refused before protection
  (`unsafe_registry_permissions`), accepted after. Its ACL was SYSTEM and
  Administrators full control and Daniel read and execute, none inherited,
  with owner Administrators.
- The backup copy dry run gave 11 identical `.py` files. 2093 GB are free on
  C:.

The preflight found and fixed two installer bugs (installer sha256 is now
`6dd5a97e…`):
- `Check`'s parameter `$name` shadowed the caller's loop variable, so the
  bundle hash checks read the wrong path. They failed closed.
- Windows PowerShell 5.1 drops `"` inside arguments to native programs. That
  broke the Python one-liner for the hello check.

Installed environment: Python 3.11.9, `butters-agent` 0.1.0, websockets
15.0.1, cryptography 50.0.1, setuptools 65.5.0. Its `RECORD` lists the stray
`profile.py`, so the reinstall removes it (nothing imports it). The task's last
result, `0x800710E0`, is the 5-minute trigger being refused because an
instance is already running (`IgnoreNew`). That is expected.

**Procedure**, once approved, in an elevated PowerShell on the desktop:
1. Stage into an Administrators-only folder, so nothing unprivileged can swap
   a file between the hash check and the elevated install:
   ```
   $S = 'C:\ProgramData\Butters\Deploy-kr260-f2f216d'
   New-Item -ItemType Directory $S
   icacls $S /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F /Q
   ```
   Then copy `windows-bundle/` from the Pi into `$S` with `scp`.
2. `(Get-FileHash $S\install-kr260-agent.ps1).Hash` must be
   `6DD5A97E1F1DC3F491256C04AFA215772A097C5B2E6CC3AE41117302D3F48E2F`. The
   installer checks every other file itself.
3. Run, in `$S`:

```
.\install-kr260-agent.ps1 -Stage Check                # read-only
.\install-kr260-agent.ps1 -Stage Install -Authorized  # needs approval
.\install-kr260-agent.ps1 -Stage Verify               # read-only
.\install-kr260-agent.ps1 -Stage Rollback -Authorized # restores the backup
```

| File | sha256 | Source |
|---|---|---|
| `butters_agent-0.1.0-py3-none-any.whl` | `5ef9de85…` | local branch `deploy/desktop-agent-kr260-jtag` `f2f216d` |
| `kr260/kr260-jtag-por.ps1` | `ee16ce33…` | `040ca84` |
| `kr260/kr260-jtag-por.tcl` | `a7709d8f…` | `040ca84` |
| `kr260-jtag.toml` | `7f4d8e93…` | `enabled = false`, cable `XFL11R5N0BBZA`, board `192.168.1.125`, both hashes |
| `install-kr260-agent.ps1` | `6dd5a97e…` | this kit |

Why a separate agent package: the agent installed on the desktop is
`origin/main`'s `butters_agent` (plus an unused, stray `profile.py`), not the
production line's. The candidate's agent also carries the batched
`desktop.app.list` snapshot from `7682879`/`d3f3be5` (PR #5), which has never
run on Windows. `f2f216d` is `origin/main` plus exactly PR #9's agent diff
(+994/−7), checked line for line. Its tests: 110 passed. Its default hello is
the same 5 actions.

`Install`:
1. Backs up `site-packages\butters_agent` and its `dist-info` to
   `C:\ProgramData\Butters\backups\butters_agent-pre-kr260-<UTC>`.
2. Creates `C:\ProgramData\Butters\kr260` and locks it before filling it:
   - inheritance removed;
   - SYSTEM and Administrators full control, Daniel read and execute;
   - owner Administrators.

   This matters because `C:\ProgramData\Butters` gives Users write access.
3. Copies both scripts into it.
4. Places `kr260-jtag.toml` in `DesktopAgent` (it inherits that folder's
   protected ACL).
5. Runs `pip install --no-index --no-deps --force-reinstall` on the wheel:
   offline, with no dependency change.
6. Restarts only the agent:
   - `Stop-ScheduledTask`;
   - stops any leftover agent interpreter by PID;
   - `Start-ScheduledTask`.

   The venv's `pythonw.exe` is a launcher, and the agent is its child
   process.

`Verify`:
- the hashes;
- the agent's own ACL rule on all three files;
- the new package loads from `site-packages`;
- the gate parses and is disabled;
- the default hello is unchanged;
- exactly one agent (launcher plus interpreter) is running;
- `kr260_jtag_loaded` with `success: false`, then `connected`, in `agent.jsonl`;
- Sunshine is running and no debugger is.

The JTAG actions are never advertised while the gate is off. Butters then
shows `jtag_not_offered`.

## Security context and remaining access questions

- The agent runs as `DESKTOP-G4CFVL1\Daniel`, Interactive, `RunLevel
  Limited`, in session 1. Daniel is a local administrator under UAC, so the
  agent holds the filtered token.
- The KR260 cable is an FT4232H (`XFL11R5N0BBZ`, "KR Carrier Card") on
  `FTDIBUS` 2.12.28.0. Every live XSDB run so far was elevated (SSH).
  Daniel's interactive Vivado Hardware Manager used the same cable on
  2026-10-27 (`vivado.jou`), so non-elevated access very likely works. It is
  confirmed only by the supervised check.
- hw_server has inbound firewall allow rules (Private, Public).
- A **second FTDI JTAG device was plugged in at 2026-10-30 02:38Z**: a
  "Digilent USB Device", FT2232H, serial `210292BB3753`, COM7. While it is
  connected and powered, `kr260-jtag-por.tcl` most likely sees two cables and
  refuses (`REFUSED_CHAIN`, nothing sent). That fails safe, but it blocks
  recovery. Any Vivado or hw_server session for it also makes the agent
  refuse (`debugger_active`).
- A hw_server scan also reaches the KR260 cable. That is now safe only because
  the deep-idle unit holds `state1/disable = 1`.

## Tomorrow: Windows agent installation (each step needs approval)

State at handoff (2026-10-30 ~03:30Z):
- production `040ca84`; all gates closed;
- KR260 boot `329affab` healthy;
- the agent connected since 03:03:20Z;
- nothing installed or staged on Windows.

1. **Codex is finished on Windows.**
   - No VM, hypervisor or installer processes.
   - No recent writes under `G:\Development\VM-Setup`.
   - Ask the owner.
2. **Revalidate.**
   - `./butters-candidate-deploy.sh postcheck` ends with "all checks passed" (the rollback-tree line
     expects `ba675b4`).
   - `cd windows-bundle && sha256sum -c SHA256SUMS`.
   - On Windows: agent task Running, Sunshine Running, no debugger, and no
     `C:\ProgramData\Butters\kr260` or `kr260-jtag.toml`.
3. **Admin-only staging folder** (elevated PowerShell on the desktop):
   ```
   $S = 'C:\ProgramData\Butters\Deploy-kr260-f2f216d'
   New-Item -ItemType Directory $S
   icacls $S /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F /Q
   ```
4. **Copy and verify the bundle.** From the Pi:
   `scp -r windows-bundle/* desktop:C:/ProgramData/Butters/Deploy-kr260-f2f216d/`.
   Then `(Get-FileHash $S\install-kr260-agent.ps1).Hash` must be
   `6DD5A97E1F1DC3F491256C04AFA215772A097C5B2E6CC3AE41117302D3F48E2F`.
5. `cd $S; .\install-kr260-agent.ps1 -Stage Check`: all 14 checks pass.
6. `.\install-kr260-agent.ps1 -Stage Install -Authorized`. It prints the backup
   folder (`C:\ProgramData\Butters\backups\butters_agent-pre-kr260-<UTC>`)
   first; it also writes `last-kr260-agent-backup.txt`.
7. `.\install-kr260-agent.ps1 -Stage Verify`: all checks pass.
8. Butters side:
   - a new `WebSocket /agent/v1/session [accepted]` in the `butters-web`
     journal;
   - agent `connected` in `agent.jsonl`;
   - the app actions still work.
9. Admin page: the agent is `connected` and JTAG shows `jtag_not_offered`
   (local gate `enabled = false`, Butters gate closed).
10. Rollback stays ready:
    - the backup folder exists;
    - `-Stage Rollback -Authorized` restores the old package and disables the
      gate, with no Windows restart;
    - the Butters rollback is `./butters-candidate-deploy.sh rollback --authorized`
      (agent JTAG gate off first).

Out of scope tomorrow without separate approval:
- enabling either JTAG gate;
- starting XSDB or hw_server, attaching JTAG, or any reset;
- unplugging the Digilent board.

The source of `f2f216d` is preserved as the local branch
`deploy/desktop-agent-kr260-jtag` and as `desktop-agent-kr260-jtag-f2f216d.bundle`
(prerequisite `origin/main` `1cd55af`). Rebuild the wheel with
`SOURCE_DATE_EPOCH=1793318400 pip wheel --no-deps --no-build-isolation` and
setuptools 84.2.1, then re-check its sha256 against `5ef9de85…`.
