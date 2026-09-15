# Deployment runbook — integrated NAS controls and Jellyfin portal

Candidate: `feature/integrated-desktop-nas-controls` @ `3f408b1`
Production at time of writing: `/opt/butters` @ `5c66593`
(`feature/desktop-remote-management-v2`, installed 2026-09-11).

**Do not start this runbook until the Desktop infrastructure is explicitly
released.** Step 4 restarts `butters-web`, and that disconnects the Windows
Desktop Agent for the length of one reconnect — see "The one unavoidable
disruption" below.

## The one unavoidable disruption

`butters-agent-ingress.service` is a TLS relay. It terminates the agent's
connection on `192.168.1.157:8443` / `192.168.1.158:8443` and proxies it to
`127.0.0.1:8090`, where the agent session actually lives inside `butters-web`.

So:

* The ingress unit does **not** need restarting, and this runbook never restarts
  it. It relays the agent's next connection to the new process by itself.
* Restarting `butters-web` **does** drop the relayed WebSocket. The agent
  reconnects on its own, but there is a brief window with no agent session.

There is no way to deploy new application code without restarting
`butters-web`, so this window is a precondition of deploying at all, not
something the sequence can avoid.

## Prerequisite: a staging checkout that has the models

`install-beta1` refuses to run without `butters/models/`, and the review
worktree in `/tmp` does not have it — `/tmp` is a 871 MB tmpfs, far too small.
Stage from the root filesystem and hard-link the models (same filesystem, so
this costs no space and no time):

```bash
git -C /home/dmejiame/ESP32-home-sensor-hub worktree add \
  /home/dmejiame/worktrees/nas-portal-deploy feature/integrated-desktop-nas-controls
cp -al /home/dmejiame/ESP32-home-sensor-hub/butters/models \
       /home/dmejiame/worktrees/nas-portal-deploy/butters/models
```

The installer copies ~639 MB of models (it excludes `models/llm` and the 20M
zipformer). `/opt` has 77 GB free against a peak need of roughly 2.2 GB for the
staging, installed and previous trees together.

## Step 1 — capture production state

```bash
sudo cat /opt/butters/DEPLOYMENT | tee ~/deploy-logs/butters-pre-$(date +%F-%H%M).json
systemctl is-active butters-web.service butters-action-broker.socket \
  butters-agent-ingress.service
sudo systemctl show -p ActiveEnterTimestamp butters-agent-ingress.service
tailscale serve status
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/healthz
```

Record the agent's current reconnect count so step 6 can prove a reconnect
rather than assume one:

```bash
# from the admin UI, or: journalctl -u butters-web -n 50 | grep -i agent
```

## Step 2 — create the rollback

`install-beta1` moves the current tree to `/opt/butters.previous` during its
swap, which is the primary rollback. `/opt/butters.previous` does **not**
currently exist, so take an independent copy first — the installer keeps only
one generation.

```bash
sudo tar --numeric-owner -C /opt -czf \
  /root/butters-rollback-5c66593-$(date +%F).tar.gz butters
sudo ls -lh /root/butters-rollback-5c66593-*.tar.gz
```

## Step 3 — deploy the candidate (no restart yet)

```bash
cd /home/dmejiame/worktrees/nas-portal-deploy
git log --oneline -1          # expect 3f408b1
sudo ./butters/scripts/install-beta1
```

Without `--start` the installer swaps the tree and runs `daemon-reload` but does
not restart `butters-web`. Verify the swap before restarting anything:

```bash
sudo cat /opt/butters/DEPLOYMENT          # expect 3f408b1
sudo ls -d /opt/butters.previous          # rollback tree now exists
```

No root-owned configuration needs editing for this deploy.
`/etc/butters/action-broker.toml` keeps all seven of its current gates and
parses unchanged — this was verified against a byte-exact copy of the live file.

## Step 3b — enable the Desktop Agent ingress (migration only)

The repository template ships both agent gates **off**, and reviewed tests
assert that, so a checkout can never carry a live machine ingress into a new
deployment. Enabling them is a deliberate step on the deployed, root-owned
copies, performed only when migrating the Desktop Agent to this architecture.

Gate 1 is the application/AgentHub gate in
`/opt/butters/config/assistant.toml`: change `[agent_ingress]` `enabled` from
`false` to `true`.

Gate 2 is the TLS transport gate in `/etc/butters/agent-ingress.toml`. The
deployed file predates this key entirely, so the key must be added as
`enabled = true`; without it the new ingress loads with the gate off and binds
no listener at all.

The machine credential file named by `config_path`
(`/etc/butters/desktop-agent.toml`) must already exist, root-owned and 0600,
before either gate is turned on.

**Re-apply Gate 1 after every subsequent `install-beta1`**, because the
installer rsyncs `config/` from the checkout and resets it to the template
value.

## Step 4 — restart only `butters-web`

```bash
sudo systemctl restart butters-web.service
systemctl is-active butters-web.service
```

Do **not** restart `butters-agent-ingress.service`, the broker socket, or
`butters-live.service`.

## Step 5 — Desktop parity FIRST (gate)

Open `/admin` → Tools and confirm the Desktop section renders with all of:
status grid, Refresh Status, SSH Test, Wake Desktop, Interactive Desktop Agent
card, the application buttons, Prepare for Streaming, VM line, and Shut Down
Desktop. Confirm the NAS panel renders *below* it rather than replacing it.

Non-interactive equivalent:

```bash
sudo grep -o 'id="desktop-[a-z-]*"' \
  /opt/butters/src/butters/web/static/admin.html | sort -u
```

**Click nothing.** Every Desktop control is an effectful action.

## Step 6 — passive agent reconnect (gate)

Watch only; issue no Desktop action.

```bash
journalctl -u butters-web -f | grep -i agent     # expect a reconnect
```

In Admin → Tools the Desktop Agent line should return to connected with a fresh
heartbeat age. `SSH Test` is a bare TCP connect that starts nothing on Windows,
but it is still a click on a Desktop control, so leave it alone while Codex
owns the machine.

## Step 7 — state-model check (gate)

With the agent connected, confirm the Desktop card shows connected / session
active / recent heartbeat under **Current observed state**, and that any wake or
shutdown history appears only on the separate **Last operation** line. The two
must not contradict each other.

## Step 8 — rollback trigger

If step 5, 6 or 7 fails, roll back immediately and do not proceed to NAS:

```bash
sudo systemctl stop butters-web.service
sudo rm -rf /opt/butters.failed && sudo mv /opt/butters /opt/butters.failed
sudo mv /opt/butters.previous /opt/butters
sudo systemctl daemon-reload
sudo systemctl start butters-web.service
sudo cat /opt/butters/DEPLOYMENT            # expect 5c66593
systemctl is-active butters-web.service butters-agent-ingress.service
```

If the previous tree is unusable, restore the tarball from step 2:

```bash
sudo systemctl stop butters-web.service
sudo rm -rf /opt/butters
sudo tar --numeric-owner -C /opt -xzf /root/butters-rollback-5c66593-<date>.tar.gz
sudo systemctl daemon-reload && sudo systemctl start butters-web.service
```

Rolling back needs no broker or ingress restart, and no configuration is
reverted because none was changed.

## Step 9 — NAS status (only after Desktop passes)

Admin → Tools → NAS → Refresh Status. Expect four independent observations and
a truthful aggregate. Status probes are read-only.

## Step 10 — Wake NAS

**The NAS was online and Jellyfin healthy when this runbook was written**, so
the correct validation is to confirm the aggregate reads `READY` without
sending anything. Send a WOL only if the NAS is genuinely off, and only once —
success wording is "Wake packet sent", never "NAS booted".

## Step 11 — portal

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://sensor-pi.tail9644cc.ts.net/portal
```

Then confirm, without enrolling anyone: the portal requires authentication,
registration is refused without an invitation, no Admin control appears, wake is
POST-only, and the status view renders.

## Step 12 — NAS shutdown stays off

Do not issue a real NAS shutdown. Both gates stay false and the broker transport
stays unconfigured, so the operation is not merely disabled but unregistered.

## Step 13 — partner enrollment

Only with the partner present. Create the invitation in Admin → Tools, hand it
over directly, and let them register their own passkey on their own device.
