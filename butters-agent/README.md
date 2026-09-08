# Butters interactive Desktop Agent

## Implemented architecture

Manual Tools UI / future voice caller → existing Action API → existing
SkillRegistry authorization → ActionCoordinator jobs and audit → registered action.

Three independent backends remain:

- Broker/WOL: cold start and fixed Windows service/recovery operations.
- SSH: Git, Python, registered builds/tests and other headless computation.
- Desktop Agent: allowlisted application launches inside the logged-in session.

The Windows agent initiates `wss://butters.lan:8443/agent/v1/session`.
`butters-agent-ingress.service` terminates TLS on the private IPv4 addresses
assigned to **eth0 and wlan0 only**. It forwards only the exact agent WebSocket
upgrade to the existing loopback web daemon. It refuses browser/API paths,
cookies, Origin, Authorization and Tailscale identity headers. It is not a
second Action API. Existing browser routes keep their Tailscale/session/CSRF/
Origin/passkey protections. No Windows listener, firewall rule, Funnel, router
change, or Windows Tailscale installation is required.

This retains the transport already installed and validated before the interrupted
session. Adding Windows to Tailscale would not remove the need for explicit
machine authentication and would unnecessarily replace a working deployment.
The URL and listener configuration can be changed later without changing actions.

## Identity and authentication

Logical device: `desktop`, Windows computer `DESKTOP-G4CFVL1`.
LAN SSH uses `DESKTOP-G4CFVL1.local`: the short name intermittently disappeared
through Tailscale DNS forwarding, while Windows mDNS resolved correctly.
Ethernet WOL identity remains `34:5A:60:D7:4C:2C`. `.209` is an observed address,
not a configured destination. The old IP in SSH `HostKeyAlias` is only a pin label.

The agent validates the server's SHA-256 SPKI pin **before sending its token**.
The token and independent HMAC key are stored under the Windows user's
`%LOCALAPPDATA%\ButtersAgent\credentials.dpapi`, encrypted using user-scoped
DPAPI, with UI forbidden and no machine-scope encryption. Butters stores the
token hash, not the token. Its signing/TLS keys and configuration are
`root:butters 0640` under `/etc/butters`; the unprivileged service needs read access.
Credentials never enter command-line arguments, source, browser storage or logs.

Signed frames bind their complete canonical JSON payload, including connection
nonce, request ID, action, target, parameters, timestamp and timeout. New
authenticated connections supersede old ones. Heartbeats run every 15 seconds;
state becomes unavailable after 45 seconds, GUI capability expires after 30.
No durable Windows command queue exists. Disconnect cancels active work before
further side effects where possible. Cancellation cannot undo an already
created process. Requests have at most 30 seconds; clock skew/freshness checks
fail closed. A 512-operation/300-second in-memory cache returns terminal results
for duplicate request IDs or idempotency keys and rejects conflicting reuse.
After agent restart the cache is gone: application process observation provides
convergent duplicate-launch protection, not exactly-once delivery across crashes.

Protocol and action schema are version 1. This initial version has no predecessor
to negotiate. There is no arbitrary shell, executable path, argument vector,
force-stop, input injection, screen capture or remote registry-edit action.

## Windows installation and startup

Installed root: `C:\ProgramData\Butters\DesktopAgent`.
Python 3.11.9 was discovered and reused through an isolated `venv`; no interpreter
upgrade was required. This intentionally differs from the design's speculative
Python 3.12 requirement.

1. Copy this package to `DesktopAgent\package` and create its dedicated venv.
2. Install that local package using the venv's `python.exe -m pip install`.
3. Provision `agent.toml` with the server URL/SPKI pin and `apps.toml` with
   **discovered** local executable paths. Never accept these paths over the wire.
4. Enroll credentials as the actual Windows user using
   `python.exe -m butters_agent.provision`, with the credential JSON supplied on
   protected stdin. It refuses to overwrite an existing DPAPI blob.
5. Run `install-task.ps1 -PythonW <venv\Scripts\pythonw.exe>` elevated as that
   user. It refuses to replace an existing task. The program/registry directory
   must be administrator-owned, SYSTEM/Administrators writable, user read/execute.

The existing task is `\Butters\DesktopAgent`: InteractiveToken, Limited,
logon/unlock triggers, five-minute IgnoreNew supervision, three one-minute
restart attempts, unlimited execution time, and no battery-stop condition.
It runs `pythonw` without a console. It is **not** a Session-0 Windows service.
No autologin, UAC suppression, BIOS, memory-training or reboot change is made.
After reboot without login, SSH can work while GUI actions correctly remain
unavailable. Locked/ambiguous/non-console sessions cannot launch applications.

Debug from a terminal in the logged-in Windows session:

```powershell
& 'C:\ProgramData\Butters\DesktopAgent\venv\Scripts\python.exe' -m butters_agent --config 'C:\ProgramData\Butters\DesktopAgent\agent.toml'
Get-ScheduledTask -TaskPath '\Butters\' -TaskName DesktopAgent
Get-ScheduledTaskInfo -TaskPath '\Butters\' -TaskName DesktopAgent
Get-Content "$env:LOCALAPPDATA\ButtersAgent\agent.jsonl" -Tail 20
```

Stop the scheduled agent before manual debugging; a second authenticated
connection intentionally supersedes the first. SSH is suitable for installation
and inspection, not for substituting an interactive agent session.

## Applications, state and VMs

Discovered applications were Parsec, Git Bash and per-user VS Code. Entries in
the administrator-controlled `apps.toml` contain an absolute local `.exe` path,
exact process image paths, and optional `require_visible`. Registry ownership
and write permissions are checked at load; malformed entries are unavailable.
Process detection filters by owner, exact executable path and agent session.
Results include PIDs, session ID and Windows visible-window enumeration.
`visible_window` is remote OS evidence, **not a claim of human visual inspection**.

Registered actions: `desktop.agent.status`, `desktop.session.status`,
`desktop.app.list/status/launch`, `desktop.vm.list/status/start/stop`, and
`desktop.streaming.status/prepare`. Applications are registry keys, not separate
action implementations. Already-running applications do not launch duplicates.
The existing `DesktopAgent` Protocol was extended additively; `run_registered`
delegates to registered app launch, and SSH compute remains separate.

`desktop.streaming.prepare` composes existing broker WOL, bounded host/SSH/agent
waits, session checks, broker Parsec service ensure, agent Parsec application
launch, and readiness verification. The agent never manages the Parsec service.
Host streaming readiness does not prove a remote Parsec stream actually connected.

No usable Hyper-V, VMware or VirtualBox installation or VM was discovered.
`VmBackend`/`UnavailableVmBackend` and the empty configuration example are the
foundation; no functioning hypervisor backend or real VM is claimed. VM control
remains unavailable. No hypervisor is installed/enabled and no VM is fabricated.

## Tools, authentication and audit

Tools derives application rows from the live registry. Disconnection or expired
browser authentication can prevent rows loading; that is not a Git Bash launch
failure. Launch is ELEVATED; VM stop is FRESH. Buttons use the existing
`POST /api/desktop/actions`, freeze a coordinator plan, renew authentication through
the existing passkey ceremony, then poll existing action-job endpoints.
Read-only agent observations require the bound administrator browser session.
Machine credentials are not accepted by browser routes.

If a session expires, reload the page and authenticate in Passkeys / Authentication.
Do not replay an old pending-action ceremony after a service restart. Session
renewal does not itself confer elevation. Tests must reuse their browser session:
the existing four-sessions-per-peer admission limit is intentionally unchanged.

The existing SQLite action audit records identity/session references, registered
parameters, authentication, outcome and coordinator job IDs. Plans retain source
and request metadata; job results correlate agent request IDs. Windows maintains
rotating secret-free JSONL diagnostics. There is no separate agent permission
database and no production passkey bypass for unattended testing.

## Butters operations and rollback

Configuration: `/etc/butters/agents.toml`, `agent-command.key`, `agent-server.key`,
`agent-server.crt`, `agent-ingress.toml`; existing compute/SSH and broker configs.
Service: `butters-agent-ingress.service`, enabled, network-online dependencies,
unprivileged `butters`, journald, hardened filesystem and restart-on-failure.
The existing `butters-web` service loads the hub and shared skills.

```sh
systemctl status butters-agent-ingress butters-web butters-action-broker.socket
journalctl -u butters-agent-ingress -u butters-web --since today
ssh desktop 'echo BUTTERS_DESKTOP_SSH_OK'
curl http://127.0.0.1:8090/readyz
curl http://127.0.0.1:8080/api/health
```

Initial deployment backups: `/var/backups/butters-agent.67xus1n7`, preserving
absolute relative paths and a `created-files.txt` manifest. They include prior
web/compute/broker files, observer configuration, pinned hosts and SSH aliases.
The source design/review and separate ESP32 worktree were not rewritten.

Rollback/uninstall:

1. Stop/disable only `butters-agent-ingress`; stop and unregister only
   `\Butters\DesktopAgent`. Do not alter the existing LockDesktop/SleepDesktop tasks.
2. Restore reviewed files from the backup tree to their matching absolute paths,
   preserving owners/modes. Restore broker gates/pins together with broker code.
   Keep the mDNS correction if it is still needed; the SSH pin must remain strict.
3. Restart only `butters-web` and restore broker socket activation. Verify WOL,
   SSH and environmental health. Never roll back sensor services for this feature.
4. Archive the dedicated Windows agent directory and user DPAPI/log directory
   before removing them. Do not delete the shared `C:\ProgramData\Butters` tree.
5. Archive/revoke the agent-only credentials and remove only the new ingress unit
   and agent configuration when no longer used. Do not remove SSH keys.

The deployment scripts document the original one-host installation, not a
general-purpose configuration migration or secret-rotation tool. Inspect existing
files before reuse; do not rerun enrollment over a working installation.

## Verification record and limitations

See `docs/DESKTOP_AGENT_REMOTE_VALIDATION.md` for the recovered evidence, stop-ship
dispositions, current deployment status and remaining remote/manual checks.
Cloud-model access is not required; `OPENAI_API_KEY` remains unconfigured.
Do not proceed to cloud/voice orchestration until the deferred GUI authorization
and visual checks are recorded honestly.
