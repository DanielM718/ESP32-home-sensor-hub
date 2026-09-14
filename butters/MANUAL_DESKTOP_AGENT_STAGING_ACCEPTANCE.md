# Desktop Agent Staging Hardware Acceptance

This runbook is for an isolated, real-Windows validation environment. It is
not a production deployment, an Admin surface, or Slice 4 reintegration.

> **Never run `butters/scripts/install-beta1` for this procedure.** Never copy
> credentials, databases, configuration, systemd units, or Tailscale Serve
> state from the production installation.

## Isolation architecture

```text
Windows ButtersAgentStaging profile
  (staging config + staging DPAPI credentials + staging apps.toml)
       |
       | pinned WSS, private LAN :18443
       v
butters-agent-ingress-staging.service
  /etc/butters-staging/agent-ingress.toml
  accepts only /agent/v1/session
       |
       | loopback HTTP/WebSocket :18090
       v
butters-staging.service TCP machine upstream
  /agent/v1/session only -> AgentHub

authorized local operator in group butters-staging-ops
       |
       | /run/butters-staging/validation.sock
       | root:butters-staging-ops 0660 (no TCP validation routes)
       v
fixed staging validation runtime
  -> SkillRegistry -> PolicyValidator -> ActionCoordinator
  -> /var/lib/butters-staging/actions.sqlite3
       ^
       |
desktop-agent-staging-validate (four fixed commands only)
  state | list-apps | status APP | launch APP

Production /opt/butters, /etc/butters, /var/lib/butters, production units,
production ports, broker configuration, Tailscale Serve, browser authentication,
and Admin routes are outside every staging write path.
```

The two explicit enable gates are:

1. `[agent_ingress].enabled` in
   `/etc/butters-staging/assistant.toml` (application/AgentHub gate).
2. `enabled` in `/etc/butters-staging/agent-ingress.toml` (TLS transport gate).

Both repository templates set these gates to `false`.

## Fixed staging identities

| Boundary | Staging value |
|---|---|
| Install root | `/opt/butters-staging` |
| Configuration/secrets | `/etc/butters-staging` |
| Persistent state | `/var/lib/butters-staging` |
| Application unit | `butters-staging.service` |
| Local control socket unit | `butters-staging-validation.socket` |
| TLS ingress unit | `butters-agent-ingress-staging.service` |
| Machine-ingress upstream | `127.0.0.1:18090`, `/agent/v1/session` only |
| Service identity | user/group `butters-staging`; no human members |
| Validation operator group | `butters-staging-ops`; fixed CLI/socket access only |
| Local validation API | `/run/butters-staging/validation.sock`, `root:butters-staging-ops` `0660` |
| Private LAN TLS listener | private staging-host address, port `18443` |
| Windows data/DPAPI/log profile | `%LOCALAPPDATA%\ButtersAgentStaging` |
| Machine identity | `desktop-staging` |

The TCP machine upstream has no `/validation/...` routes. A bare loopback
`curl` therefore cannot reach state, list, status, or launch. The Unix socket's
filesystem ownership is the local-user authorization boundary: only root and
members of the `butters-staging-ops` group can connect. Membership grants only
the four fixed validation operations, including the policy-coordinated launch;
it does not grant staging-secret access. `butters-staging` is the daemon and
ingress service identity/group and must not contain human members. Staging HMAC
and TLS private credentials are owned by `butters-staging:root`, are readable
only by their service-account owner, and are not readable by operators.

The tmpfiles policy exclusively maintains the containing runtime directory as
`butters-staging:butters-staging-ops` mode `0750`. This lets operators traverse
only as needed to reach the socket. The socket unit creates and removes only the
socket itself as `root:butters-staging-ops` mode `0660`; the service unit does
not use `RuntimeDirectory=` for this shared path. The daemon verifies the socket
identity, type, mode, owner, and operator-group GID before serving it.

The validation daemon does not construct `BetaAssistantService`, browser
sessions, passkeys, Admin routes, broker clients, or a generic AgentHub command
surface. Its only persistent database is staging `actions.sqlite3`.

## NEXT task: installation and credential provisioning

Do not run these commands during implementation review. After the branch is
approved, start from its reviewed worktree:

```bash
cd /path/to/ESP32-home-sensor-hub-staging-worktree
git status --short --branch
git rev-parse HEAD

# Baseline production without restart or mutation.
sudo ./butters/scripts/desktop-agent-staging-production-proof before

# Read-only proof that both designated TCP ports and the Unix path are unused.
sudo ./butters/scripts/desktop-agent-staging-preflight

# Installs inert templates and separate units. It neither enables nor starts.
sudo ./butters/scripts/install-desktop-agent-staging
```

Confirm the printed target is `/opt/butters-staging` and both unit names end in
`staging.service`. Stop immediately if any target differs.

Authorize a named human only when hardware validation is ready, then require a
new login/session for supplementary-group membership to take effect:

```bash
sudo usermod -aG butters-staging-ops VALIDATION_OPERATOR
```

Never add a human to `butters-staging`.

Generate new staging-only material on the staging host. Do not paste these
values into a shell history on shared systems; the variables below are
illustrative operator steps for a private root shell:

```bash
sudo -i
set +o history
export HISTFILE=/dev/null
umask 077
install -d -m 0700 -o butters-staging -g root /etc/butters-staging/desktop-agent
STAGING_MACHINE_TOKEN="$(openssl rand -hex 32)"
STAGING_COMMAND_KEY="$(openssl rand -hex 32)"
printf '%s' "$STAGING_COMMAND_KEY" > /etc/butters-staging/desktop-agent/command.key
chown butters-staging:root /etc/butters-staging/desktop-agent/command.key
chmod 0400 /etc/butters-staging/desktop-agent/command.key
STAGING_TOKEN_DIGEST="$(printf '%s' "$STAGING_MACHINE_TOKEN" | sha256sum | awk '{print $1}')"

openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 30 \
  -subj '/CN=butters-desktop-agent-staging' \
  -keyout /etc/butters-staging/desktop-agent/tls.key \
  -out /etc/butters-staging/desktop-agent/tls.crt
chown butters-staging:root /etc/butters-staging/desktop-agent/tls.key \
  /etc/butters-staging/desktop-agent/tls.crt
chmod 0400 /etc/butters-staging/desktop-agent/tls.key
chmod 0400 /etc/butters-staging/desktop-agent/tls.crt
STAGING_SPKI_PIN="$(openssl x509 -in /etc/butters-staging/desktop-agent/tls.crt \
  -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum | awk '{print $1}')"
```

Replace only the placeholder digest in
`/etc/butters-staging/desktop-agent.toml`. Transfer the plaintext machine token,
command key, and SPKI pin to Windows over an operator-approved private channel;
do not store the plaintext token in the server TOML. Clear the shell variables
with `unset STAGING_MACHINE_TOKEN STAGING_COMMAND_KEY STAGING_TOKEN_DIGEST
STAGING_SPKI_PIN`, then close the root shell after enrollment. Do not pass any
credential in argv or write it under `/tmp` or the repository.
Keep `assistant.toml`, `agent-ingress.toml`, `desktop-agent.toml`, and
`butters-staging.env` as `butters-staging:root` mode `0600`. If a plaintext
machine-token file is ever temporarily required by a separately reviewed
provisioner, keep it `butters-staging:root` mode `0400` and remove it as soon as
provisioning completes.

Review and set the private staging interface in
`/etc/butters-staging/agent-ingress.toml`. Only after reviewing every path and
port, change both staging gates to `true`. Because a root editor may replace a
file, reassert the service-only configuration ownership before activation:

```bash
chown butters-staging:root /etc/butters-staging/{assistant.toml,agent-ingress.toml,desktop-agent.toml,butters-staging.env}
chmod 0600 /etc/butters-staging/{assistant.toml,agent-ingress.toml,desktop-agent.toml,butters-staging.env}
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now butters-staging.service
sudo systemctl enable --now butters-agent-ingress-staging.service
sudo systemctl --no-pager --full status \
  butters-staging.service butters-staging-validation.socket \
  butters-agent-ingress-staging.service
sudo ss -lntp | grep -E ':(18090|18443)[[:space:]]'
sudo stat --format='%a %U:%G %n' /run/butters-staging/validation.sock
```

Do not configure Tailscale Serve. Port `18090` must remain loopback-only and
serve only `/agent/v1/session`; validation remains Unix-socket-only. Port
`18443` must resolve only to private IPv4 addresses on the configured LAN
interfaces.

The staging daemon's systemd IP policy denies all networking except loopback.
The TLS proxy allows loopback and RFC1918 IPv4 space only. IPv6 is intentionally
omitted because the ingress implementation resolves and binds private IPv4
interfaces. `ProtectProc=invisible`, `ProcSubset=pid`, and
`SystemCallFilter=@system-service` constrain both units.

## Windows staging profile

Create a new administrator-owned staging directory. Do not replace the existing
agent directory or its `config.toml`, `apps.toml`, task, DPAPI file, or logs.

1. Copy `butters-agent/config.staging.example.toml` to that directory as
   `config.toml`; set only its private staging host and SPKI pin. Keep
   `profile = "staging"`, port `18443`, and `agent_id = "desktop-staging"`.
2. Copy `butters-agent/apps.staging.example.toml` to `apps.toml`. Initial
   acceptance exposes only `notepad`. Do not add Parsec or the production app
   catalog.
3. From the staging package, provision the new values into the distinct DPAPI
   root:

   ```powershell
   Set-PSReadLineOption -HistorySaveStyle SaveNothing
   $credentialJson = @{ token = '<STAGING_MACHINE_TOKEN>'; command_key = '<STAGING_COMMAND_KEY>' } | ConvertTo-Json -Compress
   try {
       $credentialJson | python -m butters_agent.provision --config .\config.toml
   } finally {
       Clear-Variable credentialJson -ErrorAction SilentlyContinue
       Remove-Variable credentialJson -ErrorAction SilentlyContinue
   }
   ```

   Close that PowerShell window immediately after provisioning so credential
   input is neither retained in session memory nor later written to PSReadLine
   history. Start the staging agent from a fresh window with intentional normal
   history behavior:

   ```powershell
   python -m butters_agent --config .\config.toml
   ```

The default/production profile uses `%LOCALAPPDATA%\ButtersAgent`; the staging
profile uses `%LOCALAPPDATA%\ButtersAgentStaging`. A nonzero staging fault delay
is rejected unless `profile = "staging"`.

## Validation CLI and authorization

Run the installed CLI only on the staging host as root or a deliberately
authorized member of `butters-staging-ops` (shown below from that operator's
new login session):

```bash
/opt/butters-staging/scripts/desktop-agent-staging-validate state
/opt/butters-staging/scripts/desktop-agent-staging-validate list-apps
/opt/butters-staging/scripts/desktop-agent-staging-validate status notepad
/opt/butters-staging/scripts/desktop-agent-staging-validate launch notepad
```

`state`, `list-apps`, and `status` execute registered observation skills through
`SkillRegistry` and `PolicyValidator`. `launch` freezes an immutable plan in the
staging action store, creates an `AuthenticationContext`, and calls
`ActionCoordinator.execute`; the worker performs the registered skill, policy,
AgentHub, signed request/ACK/result sequence. The CLI cannot name a skill, send
JSON, choose an AgentHub method, supply a path/argv, or issue an arbitrary agent
command.

The staging assertion differs from production browser authentication as follows:

- identity and session are fixed constants, not browser-controlled fields;
- it is minted only after an authorized Unix-socket request to this dedicated
  daemon;
- it expires after 30 seconds and has the normal typed `ELEVATED` level;
- method is audited as `staging_local_host_assertion`;
- it explicitly carries the frozen `plan.digest`; `ELEVATED` currently does not
  require digest equality, but retaining the binding improves audit fidelity
  and defense in depth without weakening coordinator policy;
- there is no passkey record, browser cookie, browser origin, or production
  `security.sqlite3` access;
- it does **not** bypass `PolicyValidator`, the action authorization, frozen
  digest/state, or coordinator claim/job/audit handling.

## Hardware acceptance checklist

Leave every result unchecked until observed on real hardware. Save timestamps,
CLI JSON, staging journal excerpts, and Windows staging logs beneath the staging
evidence directory; never copy production databases into evidence.

- [ ] **TLS/SPKI:** correct pin connects. With an intentionally wrong pin,
  Windows logs `server_identity_mismatch` and sends no hello/token. Restore pin.
- [ ] **Machine authentication:** correct staging token connects; a changed
  token is rejected as `unauthorized`. Browser cookies/credentials are not
  accepted by the machine ingress.
- [ ] **Heartbeats:** observe `awaiting_heartbeat`, `connected`, then—by safely
  interrupting real staging-agent heartbeats—`heartbeat_aging`,
  `heartbeat_stale`, and `disconnected` where practical.
- [ ] **Interactive session:** observe truthful `present`, `absent`, and
  `unknown` under real unlocked, locked/noninteractive, and disconnected states.
- [ ] **Catalog:** `list-apps` contains only symbolic names and bounded status.
  Confirm no path, argv, cwd, PowerShell, PID, token, key, or local secret.
- [ ] **Status:** record configured/not-running, configured/running, and unknown
  symbolic-name rejection.
- [ ] **Launch:** launch `notepad` through the CLI; visually confirm it appears.
  Record `request_to_ack_ms`, `ack_to_result_ms`, and `request_to_result_ms` from
  the job result.
- [ ] **Already running:** repeat the same launch. Expect successful
  `outcome=already_running`; do not kill or restart Notepad automatically.
- [ ] **Idempotency:** the normal CLI intentionally creates a new coordinator
  job per explicit launch and therefore does not offer a replay switch. Record
  already-running behavior separately. Protocol replay/conflicting-key coverage
  remains automated; do not claim a real same-job replay unless a separately
  reviewed fixed coordinator replay harness is added.
- [ ] **Agent restart limitation:** record that replay cache state disappears on
  agent process restart. Do not claim exactly-once behavior across restarts.
- [ ] **Locked session:** lock Windows and attempt launch. Record whether the
  server rejects `interactive_session_unavailable` or the agent rejects
  `session_inactive` after a state change. Do not weaken either check.
- [ ] **Disconnect mid-request:** stop only the staging Windows agent during a
  request. Confirm `agent_disconnected` (or the bounded equivalent), no waiter
  remains, and staging state clears.
- [ ] **ACK timing:** normal Windows ACK is under the fixed 3-second server
  window. Do not change the timeout to make this pass.
- [ ] **Late ACK:** temporarily set
  `staging_fault_ack_delay_seconds = 4` in the Windows staging config and use a
  read-only command. Confirm timeout, one ignored late ACK/terminal, and a
  healthy connection. Restore zero.
- [ ] **Timeout then late terminal:** temporarily set
  `staging_fault_result_delay_seconds = 31` and use `status notepad`. Confirm the
  caller remains timed out, one late terminal is ignored, and the connection
  remains healthy. Restore zero. These fields cannot activate in a production
  profile.
- [ ] **Supersession:** start a second staging agent configuration with the same
  staging identity/credentials. Confirm B supersedes A, A pending work fails
  `superseded_connection`, A cannot complete B work, and B must send/list its own
  catalog. Use only staging profiles and stop both afterward.

The server exposes timing only after a completed signed terminal response.
Timeout responses remain failures and cannot later become success.

## Production non-contact proof

The proof helper reads application/configuration status only. It never reads
`security.sqlite3`, `actions.sqlite3`, audit rows, action history, or passkey
records, and writes only `/var/lib/butters-staging/evidence`.

Before installation/validation:

```bash
sudo ./butters/scripts/desktop-agent-staging-production-proof before
```

After all validation, without restarting production:

```bash
sudo /opt/butters-staging/scripts/desktop-agent-staging-production-proof after
```

The `after` command compares the expected deployment identity, application tree
digest, production service PID/start timestamp/state, production unit and
configuration hashes, production TCP listeners, both production ingress gates,
loopback health/readiness, private Admin endpoint status, and broker
configuration hash (when present). Investigate any diff;
never normalize it by reinstalling or restarting production.

## Rollback/removal

Stop and remove only staging artifacts:

```bash
sudo systemctl disable --now butters-agent-ingress-staging.service
sudo systemctl disable --now butters-staging.service
sudo systemctl disable --now butters-staging-validation.socket
sudo rm /etc/systemd/system/butters-agent-ingress-staging.service
sudo rm /etc/systemd/system/butters-staging.service
sudo rm /etc/systemd/system/butters-staging-validation.socket
sudo rm /etc/tmpfiles.d/butters-staging.conf
sudo systemctl daemon-reload
sudo rm -rf -- /opt/butters-staging /opt/butters-staging.previous
sudo rm -rf -- /etc/butters-staging /var/lib/butters-staging
```

On Windows, stop only the staging process/task and remove only the staging
directory plus `%LOCALAPPDATA%\ButtersAgentStaging`. The production/default
agent profile, if one exists, is not part of rollback.

## Expected limitations

- Agent replay/idempotency cache is in memory and disappears on agent restart.
- The CLI has no generic console and no same-coordinator-job replay operation.
- Only one live operation is accepted by the Windows agent.
- Fault delays are Windows staging-profile-only and must be restored to zero.
- Staging has no public UI, passkey enrollment, production Admin parity, planner
  integration, Tailscale Serve endpoint, or production broker access.

## Acceptance results (fill in after real hardware execution)

**Run date:** _not run_

**Reviewed commit:** _not run_

**Windows version/agent version:** _not run_

**Staging host/address:** _not run_

**Evidence directory:** _not run_

**Checklist result:** _not run_

**Observed request/ACK/result timings:** _not run_

**Observed rejection layers and error codes:** _not run_

**Idempotency/restart observations:** _not run_

**Production before/after comparison:** _not run_

**Reviewer/sign-off:** _not run_
