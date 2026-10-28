# KR260 management and recovery

Butters observes exactly one board (`kria`, machine ID
`7a6ccc6abe67407b93558d1efcb36587`, `eth1`) through the root-only action
broker. The web process has no SSH key. Every gate ships closed:

| Capability | Gate | Shipped |
|---|---|---|
| Telemetry | `[kr260] enabled` (assistant), `"kr260.status"` + `[kr260]` (broker) | off |
| Linux reboot | `[actions.kr260_reboot]` (assistant), `"kr260.reboot"` (broker) | off |
| Shutdown / halt | none; not an operation | absent |
| JTAG reset | none; not an operation | absent |

## Failure codes

The broker classifies each SSH failure into one fixed code (stderr is matched,
never returned or logged). `ConnectTimeout=5` inside a 10 s process bound.

| Code | Meaning |
|---|---|
| `host_unreachable` | no route / refused / timed out / unresolvable |
| `ssh_auth_failed` | key rejected |
| `host_key_mismatch` | pinned known_hosts did not match (identity) |
| `identity_mismatch` | machine ID did not match |
| `operation_timeout` | 10 s process bound expired |
| `reboot_unauthorized` | the board has no sudoers grant (`sudo -n` refused) |

Observations are cached for `poll_interval_seconds` (45 s); an explicit
refresh re-probes at most every 10 s, so an offline board costs one ping and
one bounded SSH attempt per window. There is no background poller.

## Reboot authorization (not yet installed)

Plain `systemctl reboot` from a non-interactive SSH session is denied by
polkit: `Call to Reboot failed: Interactive authentication required.` That is
what the first supervised attempt hit (2026-10-28 20:04:44Z); the boot ID was
unchanged afterwards. The broker now sends exactly

    /usr/bin/sudo -n /usr/bin/systemctl --no-wall --no-block reboot

and the board grants that one argv, and nothing else, with
[`config/kr260/butters-kr260-reboot.sudoers`](config/kr260/butters-kr260-reboot.sudoers).
A second reboot is refused for 300 s unless the board reports a new boot ID;
a reboot with an unknown outcome counts as possibly sent.

### Provisioning, with the owner present

1. On the KR260: install the sudoers file as shown in its header, then
   `sudo visudo -cf /etc/sudoers.d/butters-kr260-reboot`.
2. Check the grant without rebooting (from the Pi, through the root key):
   `sudo -n ssh -i /etc/butters/kr260/id_ed25519 -o UserKnownHostsFile=/etc/butters/kr260/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes ubuntu@192.168.1.125 'sudo -n -l /usr/bin/systemctl --no-wall --no-block reboot'`
   must print the command path and exit 0.
3. Add the `[kr260]` section from `config/action-broker.example.toml` to
   `/etc/butters/action-broker.toml`, set `"kr260.status" = true`, restart
   `butters-action-broker`, enable `[kr260]` in the assistant overlay, and
   confirm Admin → KR260 shows `online · identity verified`.
4. Only then, supervised: set `"kr260.reboot" = true` and
   `[actions.kr260_reboot] enabled = configured = true`, run one reboot from
   Admin, and confirm `last_reboot_request.boot_changed` becomes `true` and
   `eth1` returns. Decide afterwards whether the gate stays open.

## JTAG (Windows desktop, FT4232H VID_0403/PID_6011)

Not a capability. On 2026-10-28 20:27–20:28Z `xsdb -no-ini -eval 'connect;
targets'` and `jtag targets` (auto-started `hw_server`) both returned empty
lists. No reset was issued. That probe is the only activity found between the
last good KR260 contact (20:26:09Z) and the first failure (20:43:30Z), so do
not repeat it unsupervised.

Supervised order, owner present, board known powered and Linux reachable:

1. Watch the board (LEDs, ping in a loop) while running only
   `connect; jtag targets` — this tests whether opening the cable disturbs it.
2. Identify the target (expect the XCK26 PS/PL TAPs); record IDCODE.
3. Only after 1–2 succeed and the board stays up: a supervised `rst -por`
   with Linux running, then verify Linux and `eth1` return.
4. Only then: intentionally halt Linux and test whether JTAG recovers it.

Butters → Desktop Agent → JTAG stays unimplemented until all four pass.
