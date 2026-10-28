# Desktop access modes: supervised validation and deployment checklist

Use this at home, with the desktop in front of you. Steps marked **[APPROVE]**
change live state. Each one needs its own explicit go-ahead at the moment it
runs; approving one never approves the next. Stop at the first unexpected
result, and go to §10 (rollback).

Do not run these steps while Vivado, an installer, or a VM is running
unattended. Record each result in a dated notes file.

## 0. Preconditions (read-only)

- [ ] Pi: `git -C ~/worktrees/desktop-access-modes-completion log -1` shows the
      reviewed commit, and `/opt/butters/DEPLOYMENT` still names production
      (`1cd55af`).
- [ ] Windows: `git log -1` on `fix/mtt-virtual-display-activation` shows
      `aa7648a`, and `docs/MTT_NEXT_SESSION_HANDOFF.md` has been re-read.
- [ ] Both monitor outlets are in their normal local state: G27QC on, OLED in
      whatever state you want preserved.

## 1. Back up (Windows, elevated PowerShell) — read-only copy

```powershell
$b = "C:\ProgramData\Butters\backups\$(Get-Date -Format yyyyMMddTHHmmss)-modes-predeploy"
New-Item -ItemType Directory $b | Out-Null
Copy-Item 'C:\Program Files\Sunshine\config\sunshine.conf','C:\Program Files\Sunshine\config\apps.json' $b
Copy-Item 'C:\Program Files\Sunshine\config\sunshine_state.json' $b -ErrorAction SilentlyContinue
Copy-Item 'C:\ProgramData\Butters\*.ps1' $b
Get-ChildItem 'C:\Program Files\Sunshine\config' -Filter *.log | Copy-Item -Destination $b
Get-ScheduledTask -TaskPath '\Butters\' | ForEach-Object { Export-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath | Set-Content "$b\$($_.TaskName).xml" }
```

On the Pi, `sudo cp -a /etc/butters/action-broker.toml /var/backups/action-broker-toml-$(date -u +%Y%m%dT%H%M%SZ).bak`.

## 2. Integrate and validate the activation helper (Windows)

- [ ] Push `fix/mtt-virtual-display-activation`, then integrate it with this
      branch as described in "Windows integration contract" in
      `docs/DESKTOP_SESSION_MANAGEMENT.md`.
- [ ] Re-run the Pi suite on the integrated branch.
- [ ] **[APPROVE]** Install the matched helpers: `install-desktop-control.ps1`,
      elevated. This also copies `sunshine-session.ps1`.
- [ ] Read-only: `configure-sunshine-vdd.ps1 -AuditOnly`, with both outlets on.
      There must be exactly one MTT, one G27QC, and one FO32U2 identity.
      Record `edid_ids_valid` and the manufacturer byte order.
- [ ] Read-only: `desktop-control.ps1 -Operation DisplayStatus` prints only the
      five contract keys.

## 3. MTT 2560×1440@60

- [ ] **[APPROVE]** Run `desktop-control.ps1 -Operation VirtualDisplayActivate`
      once while it is inactive. Expect `accepted=true`,
      `virtual_mode_verified=true`, and no change to either physical display.
- [ ] Run it a second time. Expect `already_active=true`, with no flicker and
      no topology change.
- [ ] Display Settings shows MTT at 2560×1440, 60 Hz. Its `DISPLAYn` number is
      irrelevant.

## 4. Sunshine: persistence reset, configuration, and controlled restart

- [ ] **[APPROVE]** In the Sunshine Web UI → Troubleshooting, run **Reset
      Display Device Persistence** (`/api/reset-display-device-persistence`).
      Then confirm that `display_device.state` is absent. Never delete it
      behind a running Sunshine.
- [ ] **[APPROVE]** Run `configure-sunshine-vdd.ps1 -Apply`. It sets
      `output_name` to the MTT device ID and `dd_configuration_option =
      disabled`.
- [ ] Confirm there is no `global_prep_cmd`, and that `apps.json` has only the
      reviewed pre-stream guard and no display/outlet undo commands.
- [ ] **[APPROVE]** Restart Sunshine once with no client connected:
      `Restart-Service SunshineService`. Then confirm
      `-Operation SunshineStatus` reports `virtual_display_ready=true`.

## 5. Sunshine API credential (Windows, elevated)

Create the file empty, protect it, and take ownership for Administrators
**before** writing the secret. A pre-existing file could be owned by a standard
user, who would keep WRITE_DAC; the helper refuses any credential file whose
owner is not SYSTEM or Administrators, or whose ACL inherits.

```powershell
$p = 'C:\ProgramData\Butters\sunshine-api-credential.json'
Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
New-Item -ItemType File -Path $p | Out-Null
icacls $p /inheritance:r /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)'
icacls $p /setowner '*S-1-5-32-544'
$c = Get-Credential -Message 'Sunshine Web UI administrator'
@{ username = $c.UserName; password = $c.GetNetworkCredential().Password } | ConvertTo-Json | Set-Content -LiteralPath $p -Encoding UTF8
(Get-Acl $p).Owner; (Get-Acl $p).Access | Format-Table IdentityReference, FileSystemRights
```

- [ ] With no app running, `-Operation SunshineSessionStatus` reports
      `session_state=terminated`.
- [ ] With an app running and no client connected,
      `curl.exe -s http://127.0.0.1:47989/serverinfo` shows a non-zero
      `<currentgame>` and `SUNSHINE_SERVER_BUSY`. This confirms the installed
      build reports the running app to unpaired HTTP clients. If it shows 0,
      stop: the helper's `terminated` would be unsafe.
- [ ] Confirm the certificate path
      `C:\Program Files\Sunshine\config\credentials\cacert.pem` is the one
      Sunshine serves (the `pkey`/`cert` settings in `sunshine.conf`).

## 6. New Launch versus Resume (Moonlight client, Gaming mode)

- [ ] Launch Desktop. Expect `SunshineSessionStatus` = `active`, captured on
      MTT.
- [ ] Disconnect the client without quitting. Expect `resumable` (app running,
      no stream socket).
- [ ] Resume. It lands on MTT, and the pre-stream guard did **not** run, as
      the source predicts.
- [ ] Quit the app from Moonlight. Expect `terminated`.
- [ ] **[APPROVE]** With MTT inactive, attempt a Launch. The guard must refuse
      it. Never let it capture G27QC or the OLED.

## 7. Enable the Pi side in stages

Each stage is **[APPROVE]**: it is a reviewed config change, deploy, and
restart. Production deployment goes through `install-beta1` from
`worktrees/main-merge` after merge (see `butters-deploy-needs-models-tree`).

1. Broker TOML (root-owned, edited by hand): set
   `desktop.primary_monitor_on`, `desktop.sunshine_session_status`, and
   `desktop.display_status` to true. Restart `butters-action-broker.socket`.
2. In `assistant.toml`, set `display_status_enabled` and
   `sunshine_session_status_enabled` to true.
3. Set `virtual_display_activation_enabled = true`, and enable
   `desktop.virtual_display_activate` in the broker TOML.
4. Set `virtual_display_deactivation_enabled = true` and
   `sunshine_session_end_enabled = true`, with the matching broker gates.

## 8. Mode transitions (one stage at a time, each **[APPROVE]**)

- [ ] **Gaming with MTT inactive.** Expect MTT activated → `virtual_display_ready`
      → both outlets off → capture re-verified.
- [ ] **Gaming with a stream active.** Expect `sunshine_reused=true`, with no
      MTT activation and no Sunshine restart. The stream is uninterrupted.
- [ ] **Gaming → Local, stream active.** Expect G27QC on, OLED unchanged,
      `physical_display_verified=true`, `virtual_display_action=retained`
      (`sunshine_session_present`). The stream is still running.
- [ ] **Local with OLED off.** The OLED outlet stays off (check HA history),
      and the result is `ready` with one physical display.
- [ ] **Local with no Sunshine app.** Expect `virtual_display_action=deactivated`,
      with the physical display still active.
- [ ] **Supersession.** Start Gaming, then within a few seconds select Local.
      Gaming reports `mode_transition_superseded`, and no monitor-off arrives
      after Local.

## 9. End Remote Session → Local

- [ ] **[APPROVE]** With a stream active, press **End Remote Session → Local**.
      Expect this order: G27QC on → physical verified → close →
      `remote_session_terminated=true` → MTT removed. The Moonlight client
      shows the stream ended.
- [ ] Repeat with nothing running. The close is skipped
      (`sunshine_session_end=null`).
- [ ] Rename the credential file temporarily. Expect
      `sunshine_api_unconfigured`, no close, and MTT kept. Restore the file.

## 10. Rollback and monitor recovery

1. Pi:
   - Restore the backed-up broker TOML and restart
     `butters-action-broker.socket`.
   - Production code is unchanged until a deploy. After a deploy, restore
     with `/opt/butters.previous` per the deploy manifest.
2. Monitors:
   - `sudo -u butters /opt/butters/scripts/fixed-monitor-broker-call
     primary-on`. This works only once the new code and the
     `desktop.primary_monitor_on` gate are deployed.
   - Use `on` only when the OLED really needs power.
3. Windows:
   - Restore `sunshine.conf`, `apps.json`, and the helpers from §1.
   - Restore the task XML with `Register-ScheduledTask -Xml`.
   - **[APPROVE]** One Sunshine restart, with no client connected.
4. Never compensate by restarting Windows, changing topology by hand during a
   session, or toggling outlets outside the fixed helper.

## 11. Memory stability (observation only; no stress test)

Over 30 minutes idle, and again across three Gaming ↔ Local cycles, sample
every 60 s:

```powershell
Get-Process sunshine, python, pythonw -ErrorAction SilentlyContinue |
  Select-Object Name, Id, WorkingSet64, PrivateMemorySize64, HandleCount, @{n='t';e={Get-Date -Format s}}
```

- [ ] Sunshine and Desktop Agent private bytes and handles stay flat: no
      monotonic growth across the cycles.
- [ ] The Desktop Agent heartbeat stays at 15 s. One on-demand catalog request
      shows a single snapshot, with no lingering PowerShell children.
- [ ] On the Pi, `systemctl status butters-web butters-action-broker` shows
      stable memory, with no repeated broker `request_timeout`.
