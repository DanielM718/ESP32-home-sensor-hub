# Desktop compute foundation

Deployment: 2026-09-07, Butters (`dmejiame`, Raspberry Pi 4 / aarch64).

The deterministic foundation is deployed and Windows SSH is working. Butters
woke the desktop using its existing Ethernet MAC, rediscovered its address,
and verified hostname-based passwordless SSH and Git Bash for both operator
and service execution. No user compute project is registered yet.

## Architecture

```text
Voice/LLM (future structured caller) ─┐
                                    ├─ Action API / shared authorization
Manual UI (/admin → Tools) ───────────┘     → registered action → desktop SSH
                                          → future interactive Desktop Agent
```

`BetaAssistantService.execute_desktop_action` is the common authorized entry
point. `butters.actions.compute.DesktopActions` implements deterministic actions
and is also callable from the local operator CLI. Future voice callers must use
the same service entry point with their authenticated session; model output must
never bypass it. Voice routing for these new names is not enabled in this phase.

The existing Starlette app serves these endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz`, `GET /readyz` | Existing system health/readiness |
| `GET /api/desktop/status` | Online observation and SSH server banner reachability |
| `GET /api/desktop/catalog` | Action names, registered commands/projects, agent availability |
| `POST /api/desktop/actions` | Dispatch a registered action with strict parameters |

Example request body:

```json
{"action":"desktop.ssh_test","parameters":{}}
```

Supported names: `desktop.status`, `desktop.ping`, `desktop.ssh_test`,
`desktop.run_registered` (`command` name), `desktop.compile` (`project` name),
`desktop.test` (`project` name), and `desktop.build_and_test` (`project` name).
Unknown names, extra parameters, and shell strings in name fields are rejected.

Desktop endpoints require the existing administrator identity and a bound
browser session. POST also requires the configured Origin and session CSRF
token. Registered commands/builds/tests require an unexpired existing passkey
elevation, obtained in **Passkeys / Auth**. There is no new unauthenticated shell
endpoint and no new listener. Production remains loopback `127.0.0.1:8090`
behind existing Tailscale HTTPS Serve, tailnet only (not Funnel).

## Registry and execution

The installed allowlist is `/etc/butters/desktop-compute.toml`, owned
`root:butters`, mode `0640`. `BUTTERS_COMPUTE_CONFIG` can select another
operator-controlled file at startup. The service cannot edit its own commands.
Group/world-writable configuration is rejected. Restart only `butters-web`
after changing the registry; it is loaded once at startup.

Two safe commands are registered: `git_version` and `shell_info`. No actual
project directories were verified, so the project list is empty and all three
compute buttons are disabled. The commented example in
`config/desktop-compute.toml` is a placeholder, not an executable project.
Review the actual build/test scripts before registration: allowlisting a script
also trusts its repository contents and any dependencies it runs.

Results contain action/parameters, target host/hostname, success, exit code,
UTC start/completion times, duration, stdout/stderr, and timeout/truncation flags.
Each stream is capped at 64 KiB with existing secret-pattern redaction. Logs
contain action/result metadata only. Names and output are rendered as text.
One desktop operation runs at a time; there are no implicit retries. Default
SSH deadline is 60 seconds, configurable from 1 to 120 seconds. Build+test uses
one total deadline and tests run only after a successful build. Paths are quoted,
and each stage starts in the registered directory.

SSH is noninteractive, batch-only, with strict host-key checking, no agent or
port forwarding, connection/keepalive limits, and explicit identity selection.
Timeout/output overflow terminates the local SSH process; it cannot guarantee
that a remote child stopped. `remote_completion_unknown` explicitly flags this
case and transport disconnects. Check the desktop before manually retrying.
Status means an observation completed successfully; `success: true` can coexist
with `online: false`. SSH banner reachability is distinct from authenticated
execution, which is tested by `desktop.ssh_test`.

`DesktopAgent` and `UnavailableDesktopAgent` define the future GUI boundary.
No GUI-launch, shutdown, reboot, sleep, delete, or VM controls were added.

## WOL recovery and SSH identities

The saved `/home/dmejiame/scripts/wake-desktop.sh` and
`/etc/butters/action-broker.toml` both identify MAC `34:5A:60:D7:4C:2C` and
broadcast `192.168.1.255`. Windows subsequently confirmed that this is its
**Ethernet 2 / Realtek PCIe 5GbE** adapter. Its Wi-Fi adapters were disconnected.

Recovery sent a 102-byte UDP magic packet from Butters `eth0` (`192.168.1.157`)
to `192.168.1.255:9`, then three 116-byte raw Ethernet broadcast WOL frames.
All sends succeeded locally. Discovery checked neighbors, exact hostname/mDNS,
MAC-unicast ARP across the directly attached /24, passive ARP announcements,
and exact-name NetBIOS/LLMNR queries. The desktop advertised `.209` with the
expected MAC; NetBIOS and LLMNR independently confirmed its name. Normal DNS
then resolved `DESKTOP-G4CFVL1` as `DESKTOP-G4CFVL1.lan` at `.209`. No broad port
scan, router change, guessed MAC, or unverified host-key acceptance was used.

The existing WOL command remains available for repeat recovery:

```sh
/usr/bin/wakeonlan -i 192.168.1.255 34:5A:60:D7:4C:2C
getent ahostsv4 DESKTOP-G4CFVL1
ip neigh show
```

Allow boot time. If DNS is unavailable, use MAC-targeted discovery rather than
assuming the previous address. `desktop.status` does not implicitly wake the
machine; the existing separately authorized broker WOL action performs waking.

The operator `desktop` alias uses hostname `DESKTOP-G4CFVL1`, user `Daniel`, and
the verified existing `~/.ssh/windows_remote_mode` key. The other existing key,
`id_ed25519`, was rejected during the audit despite an old forced-launcher entry;
its key and Windows entry were left untouched. `HostKeyAlias 192.168.1.209`
selects the existing pinned identity, not a routing address. No static routing
address or hosts-file entry was added. Both existing private keys and the
operator's `known_hosts` remain unchanged. SSH config permissions are `0600`.

The service uses a new Ed25519 key at `/etc/butters/desktop-ssh/id_ed25519`, owned
`butters:butters`, mode `0600`, inside a `root:butters 0750` directory. Its SSH
config and pinned known-hosts file are `root:butters 0640`. Only its public key
was appended to `C:\ProgramData\ssh\administrators_authorized_keys`, with
`restrict` to deny PTY/forwarding for this headless identity. Existing entries
and the file's ACL were preserved. Public fingerprint:
`SHA256:cvkgz5nrsTDfq1Pk02d9Q/fg5YSk8VfFqewvxqBnbb8`.

## Windows default shell and rollback

Verified executable: `C:\Program Files\Git\bin\bash.exe`. Under
`HKLM\SOFTWARE\OpenSSH`, `DefaultShell` now points there and
`DefaultShellCommandOption` is `-c`. Both values were previously absent;
`DefaultShellEscapeArguments` remains absent. The setting follows the OpenSSH
[DefaultShell reference](https://github.com/PowerShell/Win32-OpenSSH/wiki/DefaultShell).
`sshd_config` syntax passed validation and was not edited. The `sshd` service
was already running with Automatic startup. New sessions immediately used Bash,
so no Windows service restart or reboot was necessary.

Backup directory:
`C:\ProgramData\Butters\ssh-foundation-backup-20260907-162715`.
It contains `registry-before.json`, `restore-shell.ps1`, `sshd_config`, and the
previous `administrators_authorized_keys`. To revert the shell, run its
`restore-shell.ps1` in elevated PowerShell and verify a new SSH connection.
It restores recorded value types or removes values that were previously absent.
Restart only `sshd` if a new session does not pick up the restored settings.

To revoke compute access, remove only the public-key entry with the fingerprint
above (comment `butters-desktop-compute`), preserving other entries and ACLs.
Restore the whole authorized-keys backup only after confirming no later entries
would be lost. The new service credential directory can then be moved into a
root-only backup directory. The previous operator alias is backed up at
`/var/backups/butters-compute.SzDc2r/ssh-config.before-wol`.

Acceptance passed: live interactive PTY with flags `himBHs`; `uname`, `pwd`, Git
2.54.0.windows.1, Python 3.11.9; remote exits 23 and 7 preserved locally; stderr
capture; literal dollar signs/metacharacters/apostrophes; and the verified
`/c/Program Files/Git` path. Repeat the nine noninteractive checks with:

```sh
python3 butters/scripts/verify-desktop-ssh.py
ssh -o BatchMode=yes desktop 'echo BUTTERS_DESKTOP_SSH_OK'
```

## Files and service changes

Created in source:

- `src/butters/actions/compute.py`
- `config/desktop-compute.toml`
- `config/desktop-ssh.example.conf`
- `windows/configure-git-bash-ssh.ps1`
- `scripts/verify-desktop-ssh.py`
- `tests/test_desktop_compute.py`
- `DESKTOP_COMPUTE.md`

Modified in source: `src/butters/web/app.py`, `src/butters/web/service.py`,
`src/butters/web/static/admin.html`, `src/butters/web/static/assets/admin.js`,
`README.md`, and `windows/README.md`.

Installed: the compute module and those four web files under `/opt/butters`,
plus `/etc/butters/desktop-compute.toml` and
`/home/dmejiame/.ssh/config`. The WOL continuation additionally installed the
dedicated private/public key, config, and known-hosts files in
`/etc/butters/desktop-ssh/`. This document is also installed as
`/opt/butters/DESKTOP_COMPUTE.md`. The deployed `service.py` preserves its original
absence of two unrelated Parsec summary strings present in source HEAD.

No services or units were created or modified. The existing enabled
`butters-web.service` was restarted twice (deployment and verified journald
metadata logging); it retains its network-online
dependency, journald logging, restart-on-failure policy, loopback binding,
read-only application/configuration directories, and persistent state directory.
No environmental service was restarted. The WOL/Windows continuation required
no further Butters service restart. Windows changes are limited to the two
shell registry values and one appended restricted public key described above.
No firewall, router, Tailscale, BIOS, memory-training, MQTT, InfluxDB, Grafana,
or sensor configuration changed.

## Verification and rollback

Recorded results (2026-09-07):

| Required check | Result |
| --- | --- |
| Butters healthy | `/healthz` HTTP 200; `/readyz` ready, local STT ready; service active/enabled, zero automatic restarts |
| Environmental services | MQTT, InfluxDB, Grafana, dashboard, bridge, export worker and printer observer active; original service start times preserved |
| Environmental endpoints | Dashboard HTML, `/api/health`, `/api/latest`, `/api/nodes` HTTP 200; InfluxDB health pass; Grafana database ok |
| Wake and discovery | Known Ethernet MAC woke and advertised `.209`; name queries and normal DNS confirmed it |
| Passwordless `ssh desktop` | Pass for operator alias and dedicated service identity, using hostname and pinned key |
| Git Bash interactive/noninteractive, Git/Python, quoting, exit codes | Pass: nine scripted checks plus a live interactive PTY |
| `desktop.status` | Live service-user CLI succeeds and reports `online: true`, `ssh_reachable: true` |
| API desktop status | Live browser GET succeeds through private Tailscale HTTPS |
| Manual desktop status | Real Chromium browser displays expected hostname, Online, and SSH reachable |
| Manual SSH Test | Real click POSTs `desktop.ssh_test`; backend returns `success: true`, exit 0, and `BUTTERS_DESKTOP_SSH_OK` |
| Registered command execution | `git_version` and `shell_info` pass through the deployed action package/service identity |
| Compile/test/build+test | All three passed against an isolated Windows temp fixture with a space-containing path; Python byte compilation and assertions ran remotely, exit 0 |
| Existing web UI | Admin overview and normal conversation page render; zero JavaScript errors; original diagnostic tool list retained |
| OpenAI key | Not configured; no key value displayed or paid request made |

Tests: **16 new compute/security tests passed**, plus **130 existing web,
authorization, desktop-workflow and broker regression tests passed**. New module
and tests pass Ruff; `git diff --check` passes. Browser testing used temporary
Chromium and libraries under `/tmp`, with no system packages installed and no
changes to browser/server security policy. Final browser testing used the real
Tailscale URL directly, without injected headers or a test proxy.

The real compile/test validation used a temporary registry and a disposable
`smoke.py` under Windows TEMP, not a fabricated user project. The fixture and
temporary registry were removed afterward. The permanent project list remains
empty until an actual project and its build/test scripts are reviewed.

Automated checks:

```sh
butters/.venv/bin/python -m pytest butters/tests/test_desktop_compute.py -q
butters/.venv/bin/python -m pytest \
  butters/tests/test_beta1_web.py butters/tests/test_web_action_auth_v2.py \
  butters/tests/test_beta1_authorization.py butters/tests/test_beta1_input_validation.py \
  butters/tests/test_desktop_workflow_v2.py \
  butters/tests/test_desktop_remote_management_v2.py \
  butters/tests/test_action_broker_v2.py -q
sudo -u butters env PYTHONPATH=/opt/butters/src PYTHONDONTWRITEBYTECODE=1 \
  /opt/butters/.venv/bin/python -m butters.actions.compute desktop.status
curl --fail http://127.0.0.1:8090/healthz
curl --fail http://127.0.0.1:8090/readyz
curl --fail http://127.0.0.1:8080/api/health
```

Open the existing private `/admin` page, choose **Tools**, then **Refresh Status**
and **SSH Test**. The latter POSTs to the action API and shows the real structured
result, including stdout/stderr and exit status. Register a reviewed project
and authenticate with a passkey before trying compute actions.

Deployment backups are `/var/backups/butters-compute.SzDc2r/{app.py,service.py,admin.html,admin.js}`.
To roll back this deployment, restore those four files to their corresponding
paths under `/opt/butters/src/butters/web/`, move the newly installed compute
module and TOML into that backup directory, and restart only `butters-web`.
For example:

```sh
sudo cp -a /var/backups/butters-compute.SzDc2r/app.py /opt/butters/src/butters/web/app.py
sudo cp -a /var/backups/butters-compute.SzDc2r/service.py /opt/butters/src/butters/web/service.py
sudo cp -a /var/backups/butters-compute.SzDc2r/admin.html /opt/butters/src/butters/web/static/admin.html
sudo cp -a /var/backups/butters-compute.SzDc2r/admin.js /opt/butters/src/butters/web/static/assets/admin.js
sudo mv /opt/butters/src/butters/actions/compute.py /var/backups/butters-compute.SzDc2r/compute.py.disabled
sudo mv /etc/butters/desktop-compute.toml /var/backups/butters-compute.SzDc2r/desktop-compute.toml.disabled
sudo systemctl restart butters-web
```

To undo the new SSH alias, remove only its `Host desktop` block (preserve any
subsequently added entries). Windows and service-key rollback are documented above.

OpenAI audit: `OPENAI_API_KEY: not configured`. Its service environment-file entry
and running web-process value are empty. Associated systemd EnvironmentFile
declarations and project configuration locations were checked without displaying
secrets. No key was added and no paid API calls were made.
