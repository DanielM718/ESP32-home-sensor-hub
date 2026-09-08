# Desktop Agent remote validation — 2026-09-08

## Recovered state

Branch `feature/desktop-remote-management-v2`, HEAD
`4925c64cd5f7937b6d2333a63ac4dcf378cd367c`. No staged changes. The interrupted
work already contained the agent package, transport, coordinator integration,
Tools controls, deployment and tests. Existing foundation changes were preserved;
no reset, duplicate project, firmware edit or ESP-NOW worktree change was made.
The preserved previous full-suite result was 846 passed (4 warnings, 61.99s).

This continuation checked/deployed the pending state/timeout changes, added an
explicit interactive installation self-test, clarified expired browser-session
feedback, aligned example identity configuration with working mDNS, updated
documentation and changed the fixed Shutdown helper to non-forcing `/s /t 0`.
The temporary installation-validation task was removed after successful completion.

## Stop-ship dispositions

All six reviewed issues are **ALREADY RESOLVED** in the resulting implementation:

| Review item | Evidence / resolution |
|---|---|
| R4: broker through Bash | Confirmed original backslash construction defect; fixed the quoted forward-slash Windows helper path. Real broker `desktop.parsec_status` returns structured success through Windows PowerShell after the Bash default-shell change. Real SSH quoting, spaces, stderr and nonzero exit tests pass. No disruptive power action was used to test quoting. |
| R1/D1: reachable machine transport | Dedicated LAN-only TLS ingress on port 8443 forwards only the machine WebSocket route. Windows initiates; server identity is pinned before credentials are sent. Browser origin/session/CSRF/passkey checks are unchanged. |
| W1: interactive session | Limited InteractiveToken task; session/owner/executable checks and launch session verification. Real agent and newly launched applications are in active session 1, not Session 0. |
| R2: ownership split | Broker owns WOL and Parsec service operations; agent owns user-session application state/launch. Streaming workflow composes existing broker, SSH and agent abstractions. |
| R5/W7: registry protection | Explicit administrator/SYSTEM ownership and write access, Daniel read/execute only, load-time ACL checks and registry hashes. The earlier child-inheritance installation error was corrected; no broad writable registry is accepted. |
| T8: shared authorization/audit | New actions register in the existing SkillRegistry and use AuthenticationLevel, ActionCoordinator and audit store. A real GUI request produced an elevated pending plan with zero jobs started; it was cancelled, not bypassed. |

## Transport and deployment

Kept option A: the already implemented, tested independent LAN WSS ingress.
Adding Tailscale enrollment to Windows would not simplify this recovered working
deployment enough to justify an additional enrollment dependency. Windows remains
off Tailscale. Butters retains private Tailscale Serve for its browser UI; no
Funnel, router change, public listener or inbound Windows listener/firewall rule.
The agent URL/pin can be changed deliberately for a later transport migration.

Stable desktop identity: `DESKTOP-G4CFVL1`; working LAN resolution:
`DESKTOP-G4CFVL1.local`. WOL Ethernet MAC: `34:5A:60:D7:4C:2C`.
The rediscovered IPv4 `192.168.1.209` is an observation, not the SSH routing identity.
Any old HostKeyAlias IP label identifies an existing trusted key, not a destination.

Windows: `C:\ProgramData\Butters\DesktopAgent`, task `\Butters\DesktopAgent`
with limited interactive logon/unlock startup, restart policy and periodic
IgnoreNew supervision. Credentials are user-DPAPI protected under
`%LOCALAPPDATA%\ButtersAgent`; registries/code are administrator protected.
No Windows reboot, BIOS change, hypervisor installation or cloud key was required.

Butters: existing web service gains the hub/actions/Tools controls; the only new
service is `butters-agent-ingress`. Configuration is under `/etc/butters` with
restricted credential/key permissions. Per-device token authentication and
HMAC-signed, nonce/deadline-bound requests are distinct from browser authentication.
Only local registered application names are accepted; no wire executable or shell.

## Remote evidence

- Existing broker WOL returned success. Initial discovery did not immediately
  recover SSH; the previously documented Ethernet fallback sent three 116-byte
  frames on eth0. Targeted ARP returned the exact known Ethernet MAC twice;
  hostname-based SSH recovered. No broad LAN scan or invented address/MAC.
- Nine real SSH checks passed: sentinel, Bash `uname`, `pwd`, Git 2.54.0.windows.1,
  Python 3.11.9, exit 23, stderr plus exit 7, literal quoting and paths with spaces.
- Agent status reports connection, ACTIVE session 1 and GUI capability. SSH
  authentication remains a separate observation; agent crash testing previously
  demonstrated SSH success while the agent was unavailable.
- Reconnect after Butters web restart passed. Windows process-crash recovery
  passed via the scheduled supervision tick; do not infer an exactly one-minute
  recovery guarantee. No full production-machine reboot was needed.
- Interactive installation self-test completed with Task Scheduler result 0.
  All three applications started from stopped state and second launches returned
  `already_running`: Git Bash/mintty PID 20004; Parsec initial PID 17768, later
  normal secondary PID 4260; VS Code initial PID 32752 with its child processes.
  Executable paths, parent PIDs, creation times and session 1 were inspected.
  Windows reported visible windows for all three in subsequent live API status.
  These were explicit operator installation tests of the same engine, **not**
  a claim that an unattended browser passkey was approved.
- Real Action API: desktop status, app list, VM list and SSH test HTTP 200.
  Arbitrary executable-path request HTTP 403. Passkey renewal options HTTP 200;
  valid GUI launch stayed pending at elevated authorization with zero jobs.
- Real Tools browser: agent connected/ACTIVE; git_bash, parsec and vs_code running
  with real launch controls; SSH Test returned the sentinel through
  `/api/desktop/actions`. No unexpected JavaScript errors. Screenshot retained at
  `/tmp/butters-agent-tools.png` (temporary diagnostic artifact).
- No usable Hyper-V/VMware/VirtualBox backend or registered VM was discovered.
  VM list honestly reports unavailable/empty; no VM start/stop claim is made.
- Targeted agent/compute tests: 56 passed in 3.41s. Coverage includes malformed,
  stale, duplicate/conflicting IDs and idempotency keys, HMAC/nonce, wrong pin/auth,
  busy/timeout/cancellation, missing executable, unknown action/app, unavailable
  session, failed launch, authorization and streaming composition.
- Eight existing infrastructure services and the new ingress remained active.
  Environmental health/data and existing dashboard/SSH checks passed.
  `OPENAI_API_KEY: not configured`; no cloud calls or credential output.

Final full suite: **846 passed, 4 deprecation warnings in 59.77s**; log
`/tmp/butters-agent-final-tests-privileged-20260908.log`. An initial restricted
sandbox run could not exercise sockets; the permitted full rerun passed.
`git diff --check` passed. Final real broker Parsec status → ensure → status
passed: the existing broker started the service (1,071 ms), retained manual
startup configuration and reported service/user host present and plausibly ready.
This was an authorized operator validation of existing broker primitives, not
an unattended browser authorization or a tested streaming client connection.

## Deferred checks / limitations

**REMOTE VALIDATION INCOMPLETE — requires later local visual confirmation**:
human observation of the Git Bash/Parsec/VS Code windows. OS window/session
evidence is not physical observation.

User-approved browser passkey GUI launch and complete streaming workflow remain
manual acceptance checks. Authentication renewal can start normally, but no
credential assertion was fabricated. The old expired-session message was not
a Git Bash failure. Streaming primitives/composition are implemented; an actual
streaming client connection has not been claimed. No real compute project or VM
is registered. No durable Windows command queue or automatic stale replay exists.

## Final shutdown

The installation self-test had finished, the temporary task was removed and
no inspected build processes remained. Using the established fixed Windows
desktop-control helper over operator SSH, `Shutdown` returned
`accepted=true`. The helper used normal `/s /t 0`, without `/f`; public/browser
destructive-action gates were not enabled for this maintenance operation.
Butters then observed TCP/22 unavailable and ping unreachable at the freshly
verified desktop address. The real agent API returned HTTP 200 with
`agent_connected=false`, `agent_disconnected`, session UNKNOWN and GUI capability
false. All eight existing infrastructure services plus the
agent ingress remained active; environmental health returned `status=ok`.
**Windows was shut down; Butters was not shut down.**

## Rollback details

See [agent runbook](../butters-agent/README.md) for startup, debugging, logs,
configuration and scoped uninstall. Initial Butters backups:
`/var/backups/butters-agent.67xus1n7`; keep the existing SSH foundation backups.
Windows shutdown helper backup:
`C:\ProgramData\Butters\desktop-control.ps1.before-normal-shutdown-20260908`.
Restore only reviewed corresponding files, disable/remove only the new ingress
and DesktopAgent task, archive the dedicated agent directories/credentials,
restart relevant web/broker components and recheck environmental health.
Do not remove SSH keys or the shared Windows Butters directory.

Next: complete the deferred passkey/visual acceptance checks before considering
voice/cloud orchestration over these same deterministic actions.
