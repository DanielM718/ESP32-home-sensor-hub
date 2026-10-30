# KR260 JTAG recovery: Desktop Agent integration handoff (Claude → Windows Codex)

Written 2026-10-30. **Windows Codex owns the Desktop Agent runtime.** This file
is the contract it needs to add the two KR260 recovery actions in whatever way
fits its agent architecture. Claude owns the Butters side and the KR260 policy,
and does not install or change anything on Windows.

The full background is in `KR260_MANAGEMENT.md`, under "Butters → Desktop Agent
recovery".

## 0. State at handoff

| Item | State |
|---|---|
| Butters production | `040ca84`, since 2026-10-30 03:03Z. It already understands both actions; both gates are closed. |
| Production gates | `kr260.status=true`, `kr260.reboot=false`, `desktop.wake=false`. `actions.kr260_jtag_recovery` is absent from the overlay (disabled, unconfigured). |
| Installed Desktop Agent | `origin/main`'s `butters_agent` 0.1.0 (files dated 2026-09-15), plus an unused stray `profile.py`. It has no KR260 code and advertises 5 actions. |
| Reference agent build | local branch `deploy/desktop-agent-kr260-jtag` `f2f216d` = `origin/main` + exactly PR #9's agent diff (+994/−7). Not installed. |
| Kit | `/home/dmejiame/kr260-diagnostics/2026-10-30-deploy/` on the Pi. The text files are mirrored in `butters/deploy/kr260-jtag-recovery-20261030/`. |
| PR #9 | draft, `feature/kr260-jtag-recovery-agent`, stacked on PR #8. Never merge either as a branch (both contain `1e36895`). |

## 1. Wire contract (must stay byte-compatible)

Butters validates every frame against **its own copy** of
`butters_agent/protocol.py`, installed into `/opt/butters/src` by
`install-beta1`. Any protocol change therefore has to land in the repo's
`butters-agent/src/butters_agent/protocol.py` and be deployed to Butters
**before** an agent advertises it.

- **Actions:**
  - `kr260.jtag_por.check`: attach-only check.
  - `kr260.jtag_por.reset`: one `rst -por`.
  - Both take `parameters = {}`, and Butters rejects anything else.
- **`protocol.SCHEMAS`:** `"kr260.jtag_por.check": set()` and
  `"kr260.jtag_por.reset": set()`.
- **`protocol.MUTATIONS`:** includes `kr260.jtag_por.reset`.
- **`protocol.OPTIONAL`:** `frozenset({"kr260.jtag_por.check",
  "kr260.jtag_por.reset"})`.
- **Hello:** `actions` must be `sorted((SCHEMAS - OPTIONAL) | enabled_optional)`.
  - With the local gate off, the hello is exactly today's 5 actions.
  - `AgentHub._authenticate_hello` rejects **the whole agent** if it
    advertises an action Butters does not know. A gate-on agent against
    `ba675b4` or older is therefore disconnected entirely, which also takes
    down the app actions.
- **Frames:** signing, request IDs, idempotency and connection binding are
  unchanged. A reset is a mutation and carries Butters' idempotency key.
- **Butters timeout:** 30 s per request. The agent must answer within 28 s.

Result object, built by the agent from fixed values only; no wrapper text
crosses the wire:

| Field | Type / values |
|---|---|
| `success` | `true` only for exit 0 with `CHECK_OK` (check) or `RST_ISSUED` (reset) |
| `jtag_result` | one of `CHECK_OK RST_ISSUED RST_ERROR CONNECT_FAILED REFUSED_USAGE REFUSED_UNCONFIRMED REFUSED_CHAIN REFUSED_TARGET REFUSED_REPEAT REFUSED_DEBUGGER_ACTIVE REFUSED_MISSING_SCRIPT REFUSED_REACHABLE REFUSED_PROBE NO_RESULT` |
| `debugger_cleanup` | `clean`, `forced` or `failed` |
| `reset_may_have_run` | reset only: `true` for `RST_ISSUED`, `RST_ERROR`, `NO_RESULT`; otherwise `false` |
| `reset_issued_at` | reset only: the script's UTC stamp (`YYYY-MM-DDTHH:MM:SS.fffZ`) for `RST_ISSUED`, else `null` |
| `error` (on failure) | `jtag_failed`, or a refusal before PowerShell: `board_reachable`, `debugger_active`, `integrity_failed`, `rate_limited`, `busy`, `unsupported_action` |

Butters treats an unknown code, a success with the wrong code, a lost agent or
a timeout as **"may have reset"**, which starts its 30-minute interval. So
over-reporting is safe and under-reporting is not.

## 2. What Codex must preserve, replace, adapt or integrate

| Component | Action |
|---|---|
| Hello, frame signing, `desktop.*` actions, `apps.toml`, `agent.toml`, the `\Butters\DesktopAgent` task (Interactive, `LeastPrivilege`, `IgnoreNew`, 5-minute trigger), the venv | **Preserve.** Nothing in KR260 recovery changes them. |
| `butters_agent/protocol.py` | **Integrate** the 3 additions in §1 exactly. |
| `engine.py` | **Integrate:** `Engine(platform, registry=None, kr260=None)`; `actions()` as in §1; route `OPTIONAL` actions to `kr260.invoke(action)` and refuse them (`unsupported_action`) unless enabled. Once a reset has started it runs to completion; no cancellation. |
| `client.py` | **Adapt:** the hello uses `self.engine.actions()` instead of `sorted(SCHEMAS)`. |
| `__main__.py` | **Integrate** `load_kr260(platform, config_path)`. It reads `kr260-jtag.toml` next to `agent.toml`, applies the agent's own ACL rule to it, and logs `kr260_jtag_loaded` (`success` = enabled, plus the file's `sha256`) or `kr260_jtag_disabled` (`invalid_kr260_config`). An invalid file disables only this feature. |
| `kr260.py` (new, 216 lines) | **Integrate as is**, or reimplement to the same contract (§3–§6). |
| `platform/win32.py` | **Integrate** `file_sha256`, `debuggers_running`, `host_answers_ping`, `run_wrapper`, `stop_debuggers` (+94 lines, fixed binaries under `%SystemRoot%\System32`). They passed natively on 2026-10-29. |
| `platform/fake.py` | **Integrate** for tests (+50 lines). |
| The desktop-access-modes batched `desktop.app.list` (`7682879`, `d3f3be5`, in `040ca84`'s agent copy but not installed) | **Codex's decision.** It is unrelated to KR260; `f2f216d` deliberately leaves it out. |
| WinFleet, Sunshine, MTT, display and power modes, VMware | **Not touched** by KR260 recovery. |

The full diff is `git diff origin/main f2f216d -- butters-agent/`. Tests:
`butters-agent/tests/test_kr260.py` (110 agent tests pass on `f2f216d`).

## 3. Guarded scripts and integrity

| File (in `C:\ProgramData\Butters\kr260\`) | sha256 |
|---|---|
| `kr260-jtag-por.ps1` (wrapper) | `ee16ce3344b07b2787f09b403bd68ebcb82dc0a4454fc9ff2c0618ff19844836` |
| `kr260-jtag-por.tcl` (XSDB script) | `a7709d8fbdf1c65e5f6b6425f75e629838272c315a79fea3b260b96bb84d73da` |

Both are identical to `butters/config/kr260/` at `040ca84`. Before **every** run
the agent checks:
- owner SYSTEM or Administrators, with no other principal holding a write right
  (`0xD0156` mask), on each file and its folder;
- both SHA-256 values against `kr260-jtag.toml`.

Any failure is `integrity_failed`, and PowerShell is never started.

Fixed invocation, and nothing else:

```
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\ProgramData\Butters\kr260\kr260-jtag-por.ps1 `
  -Mode check|por -CableSerial XFL11R5N0BBZA -BoardAddress 192.168.1.125 -TimeoutSeconds 19 [-Confirm confirm-por]
```

The arguments come from the local config only. Nothing on the wire selects a
path, cable, address or argument.

The wrapper's last line is `<UTC>Z RESULT <CODE> [at=<UTC>Z] …`. Exit 0 means
only `CHECK_OK` or `RST_ISSUED`. The agent takes the **last** `RESULT` line,
and anything malformed is `NO_RESULT`.

## 4. ACLs: `C:\ProgramData\Butters\kr260`

`C:\ProgramData\Butters` grants `BUILTIN\Users` inherited Write. A folder
created there normally inherits it, and the agent then refuses every run
(`integrity_failed`). Lock the folder **before** copying anything into it:

```
icacls C:\ProgramData\Butters\kr260 /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F "DESKTOP-G4CFVL1\Daniel:(OI)(CI)RX"
icacls C:\ProgramData\Butters\kr260 /setowner *S-1-5-32-544 /T
```

This was simulated natively on a scratch folder (2026-10-30): the agent's rule
refused it before protection and accepted it after. `kr260-jtag.toml` lives in
`C:\ProgramData\Butters\DesktopAgent\` and inherits that folder's existing
protected ACL; set its owner to Administrators.

## 5. Agent-local gate: `kr260-jtag.toml` (ships disabled)

Exactly these keys; anything else disables the feature:

```
schema_version = 1
enabled = false
cable_serial = "XFL11R5N0BBZA"
board_address = "192.168.1.125"
wrapper_sha256 = "ee16ce3344b07b2787f09b403bd68ebcb82dc0a4454fc9ff2c0618ff19844836"
script_sha256 = "a7709d8fbdf1c65e5f6b6425f75e629838272c315a79fea3b260b96bb84d73da"
```

- `cable_serial` must match `^[A-Z0-9]{12}A$`.
- `board_address` must be a canonical private IPv4.
- The reviewed file has sha256 `7f4d8e93c31853719f411c03e0203ef8f2727f7d058d3a413888685e3dc519ce`.
- With `enabled = false`, the hello is unchanged and Butters shows
  `jtag_not_offered`.

## 6. Runtime safety the agent must keep

1. **Lock.** One run at a time; a second request gets `busy`. Check and reset
   share the lock.
2. **Agent interval.** At most one reset attempt per 1800 s, held in memory.
   An attempt counts once the wrapper starts, even if it fails or times out.
   Butters keeps its own persisted interval independently.
3. **Order of checks:**
   1. rate limit;
   2. ACL and hash of both files;
   3. no `hw_server`, `xsdb`, `rdi_xsdb`, `cs_server` or `vivado` process
      (`tasklist /FO CSV /NH`, failing closed);
   4. **one ICMP ping last**, immediately before PowerShell (`PING.EXE -n 2
      -w 1000`; only a `TTL=` reply counts as reachable, failing closed);
   5. the wrapper.
4. **The script's own probes.**
   - It makes one TCP connect to `board_address:22` immediately before
     `connect`, and again while attached, immediately before
     `CHECK_OK`/`rst -por`.
   - An accepted or refused connection gives `REFUSED_REACHABLE`, and any
     doubt gives `REFUSED_PROBE`; nothing is sent.
   - Remaining window: about 1 s. A refused connection takes about 2 s to
     report on Windows, so for a kernel without sshd the agent's ICMP ping is
     the cover.
5. **Identity.** The script requires exactly one cable, an `SCK-KR` with serial
   `XFL11R5N0BBZA`, the chain `xck26 04724093` then `arm_dap 5ba00477`, and
   target `PSU` active. Anything else is `REFUSED_CHAIN`/`REFUSED_TARGET`.
6. **One reset.** Only `rst -por`, once per process: never `rst -system`, no
   retry, no fallback, and no `stop`/`con`/`mwr`/`dow`/`fpga`/`exec`. It always
   disconnects (`try…finally`).
7. **Cleanup.**
   - The wrapper kills XSDB at its 19 s deadline and stops every debugger the
     moment XSDB exits.
   - The agent kills the PowerShell **tree** (`taskkill /F /T /PID`) at 21 s,
     then stops any leftover debugger by image name and reports
     `debugger_cleanup`.
   - Only processes this run started can exist, because the run refused if
     any were present beforehand.
8. **Time budget:** ≤5 s of checks, a 21 s run, ≤2 s of cleanup: 28 s in all,
   inside Butters' 30 s window.

## 7. Privilege: the open question

- **The agent's token.** The agent runs as `DESKTOP-G4CFVL1\Daniel`,
  Interactive, `RunLevel Limited`, in session 1, with a UAC-filtered token
  (Daniel is a local administrator). The process is a venv `pythonw.exe`
  launcher plus its child base interpreter.
- **Unverified: the cable under that token.** Every live XSDB run so far was
  elevated (SSH). Indications that it works:
  - FTDI `FTDIBUS` 2.12.28.0 is installed;
  - Daniel's own, probably non-elevated, Vivado Hardware Manager used the same
    cable on 2026-10-27;
  - hw_server has inbound firewall allow rules.

  Only a supervised run proves it.
- **The ACL check itself** works fine under the limited token: it only reads.

## 8. Second JTAG cable (Digilent)

A "Digilent USB Device" (FT2232H, serial `210292BB3753`, COM7) has been
plugged in since 2026-10-30 02:38Z.
- **The owner's decision** is to leave it connected for now. It must not be
  unplugged or worked around.
- **While connected,** both actions will most likely return `REFUSED_CHAIN`
  ("expected exactly one JTAG cable"), with nothing sent. That fail-closed
  result is intended.
- **Debuggers:** any Vivado or hw_server session on it makes the agent refuse
  (`debugger_active`).
- **Before live recovery tests,** the cable question must be resolved, either
  by disconnecting it during tests or by a reviewed change to the cable rule.
  Changing that rule changes the Tcl hash, needs a supervised check, and
  belongs to Claude, as KR260 policy.

## 9. Install, restart and rollback (no Windows restart)

The reference procedure is `windows-bundle/install-kr260-agent.ps1` (sha256
`6dd5a97e1f1dc3f491256c04afa215772a097c5b2e6cc3ae41117302d3f48e2f`).
- Its stages are `Check` (read-only), `Install -Authorized`, `Verify` and
  `Rollback -Authorized`.
- Its read-only `Check` stage passed natively on 2026-10-30, 14 of 14.
- Codex may use it as is or fold its steps into its own deployment.

What it does:
- **Install:**
  1. back up `site-packages\butters_agent` and its `dist-info`;
  2. lock and fill `kr260\`;
  3. place the disabled `kr260-jtag.toml`;
  4. `pip install --no-index --no-deps --force-reinstall` the wheel
     `butters_agent-0.1.0-py3-none-any.whl` (sha256
     `5ef9de85334cb12aa4efc5c0437ec61864a29af1f7fd02105864b69ec03ecbeb`,
     built from `f2f216d`); offline, with no dependency change;
  5. restart only the agent task.
- **Agent restart:** `Stop-ScheduledTask`, then stop any leftover
  `pythonw.exe -m butters_agent --config …\DesktopAgent\agent.toml` by PID
  (the launcher's child can survive), then `Start-ScheduledTask`. It is
  verified as exactly two processes, launcher plus interpreter.
- **Rollback:**
  1. rename `kr260-jtag.toml` away **first**;
  2. stop the agent;
  3. restore the backed-up package and `dist-info`;
  4. start the agent.

  `kr260\` is inert without the gate file. WinFleet, Sunshine and displays are
  unaffected.
- **Staging:** put the bundle in an Administrators-only folder (for example
  `C:\ProgramData\Butters\Deploy-kr260-f2f216d`, created with
  `/inheritance:r` granting SYSTEM and Administrators only). Check the
  installer's own hash before running it elevated.

## 10. Tests required before enabling either action

Each step is a separate owner approval, in this order:

1. **Install, gate off.**
   - `-Stage Verify` passes.
   - `agent.jsonl` shows `kr260_jtag_loaded` with `success: false`, then
     `connected`.
   - Butters shows the agent `connected` and JTAG `jtag_not_offered`.
   - The `desktop.*` actions still work.
2. **The installed wrapper under the agent's token, healthy board.**
   - First confirm the deep-idle unit holds `state1/disable = 1111`.
   - From a non-elevated PowerShell, run `kr260-jtag-por.ps1 -Mode check`.
   - Expect `PROBE before_attach reachable` and `REFUSED_REACHABLE
     before_attach`, exit 1.
   - XSDB starts but never connects, and no hw_server appears.
3. **Agent gate on, Butters gate still closed, healthy board.**
   - The hello now lists 7 actions, and Butters shows `gate_closed`.
   - Any request is refused before PowerShell (`board_reachable`).
   - Then switch the agent gate off again.
4. **Resolve the Digilent cable** (§8).
5. **Attach-only check on a genuinely dark board.**
   - This needs a separate decision: nobody wedges the board on purpose.
   - COM5 logging must be running.
   - Run the Admin "JTAG attach check" with a fresh passkey.
   - Expect `CHECK_OK` and `debugger_cleanup clean`.
   - This is the first proof of FTDI access under the limited token.
6. **One Admin reset on that dark board.**
   - Expect `requested`, then `confirmed` / `new_boot_id` within about 80 s,
     and `kr260-reset-verdict` reporting `por_confirmed`.
   - Afterwards check the deep-idle unit, the package holds and the FPGA.
7. Close both gates unless keeping them open is separately approved.

## 11. Deployment order and cross-agent dependencies

1. **Butters first.** This is done: `040ca84` knows the protocol.
   - PR #9's `b0963f5` (a Butters-only policy fix: the post-kexec quiet period
     survives a Butters restart) is not deployed yet. It has no protocol
     change.
   - Claude deploys it separately, with owner approval.
2. **Agent second, gate off** (Codex): §9, then §10 step 1.
3. **Gates last:** each gate and each live step is approved separately (§10).
4. **Rollback order:** agent gate off, then agent package, then (if needed)
   Butters via `butters-candidate-deploy.sh rollback --authorized`.

| Dependency | Owner | Notes |
|---|---|---|
| `butters_agent/protocol.py` (wire contract) | shared | change it only as a coordinated pair (Butters deploys first) |
| Desktop Agent config (`agent.toml`, `apps.toml`) | Codex | unchanged by KR260 |
| Agent task / service startup | Codex | the KR260 install restarts only the agent task |
| Sunshine, MTT, displays, power modes, WinFleet, VMware | Codex | no dependency |
| Windows SSH | Codex (Windows) | used only by Claude for read-only inspection; never in the recovery path |
| Broker API | Claude | no change; JTAG goes through AgentHub, not the broker |
| KR260 policy, scripts, Admin card | Claude | a script change means new hashes plus a supervised check |

**No second agent, service or recovery daemon on Windows.** The recovery path
is only Butters → the existing Desktop Agent → the fixed wrapper.
