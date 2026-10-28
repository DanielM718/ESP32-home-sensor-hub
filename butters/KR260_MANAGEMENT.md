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

Not a capability. Channel A of the FT4232H is the JTAG cable
(`Xilinx SCK-KR XFL11R5N0BBZA`, D2XX, no COM port); channel B is the Linux
console (`COM5`, 115200 8N1, no flow control). The chain is `xck26` (IDCODE
`04724093`) and `arm_dap` (`5ba00477`). `hw_server` scans asynchronously: wait
a few seconds after `connect` before `jtag targets`, or the list comes back
empty (as on 2026-10-28 20:28Z).

### 2026-10-28 hangs: observed

- 01:59:10–01:59:40Z: Vivado Hardware Manager launched `hw_server`, opened
  `XFL11R5N0BBZA` and refreshed `arm_dap_1` (Vivado journal and log). Linux
  logged `tpm_tis_spi spi2.0: SPI transfer timed out` at 02:00:59–02:01:10Z
  and nothing after. There was no shutdown, and the board needed a power cycle.
- 20:27:36–20:28:34Z: `xsdb connect; targets; jtag targets`. The last journal
  entry is 20:26:19Z. timesyncd restored the clock from 20:27:04 on the next
  boot, so the disk was still being written then. The cron job due at 20:30:01
  and sysstat due at 20:30:08 never reached the journal (writeback ≤35 s). The
  hang therefore falls between 20:27:04 and about 20:30:40Z.
- 22:50Z, board dead (serial silent, no ARP): a read-only `targets` showed
  Cortex-A53 #0, #1 and #3 in `Reset Catch, EL3(S)/A64` at `pc=0xfffea154`
  (TF-A BL31 in OCM, base `0xfffea000`), #2 `Running`, and the RPU in reset.
- No panic, oops, RCU stall, watchdog or pstore record exists for either hang.
  pstore and `mtdoops` are not configured and `kernel.panic=0`, so their
  absence is not evidence that no crash occurred.
- A 12 V power cycle recovered the board: FSBL → BL31 v2.8 → U-Boot 2023.01 →
  kernel 15 s → SSH 67 s after the first console byte, with all four CPUs
  online.

### Explanation: supported, not yet proven

On this board `state0` is `WFI` (1 µs) and `state1` is `cpu-sleep-0`, a PSCI
power-down (`arm,psci-suspend-param = 0x40000000`, 900 µs, 10 ms residency).
The cores spend over half of idle time in `state1`, and every wake from it
re-enters the core through reset at the TF-A warm entry. With a debugger
attached, reset catch halts each waking core there, so SMP Linux waits forever
on the halted cores. This matches the observed core states, and both hangs
followed a debugger attach within 1–3 minutes. The same failure is reported
publicly for ZynqMP (PYNQ ZCU104 "hangs when JTAG is used": disable cpuidle).
It has not been reproduced in a controlled experiment, and the 22:50Z read
cannot tell whether the cores were caught at 20:27Z or re-caught by that read.

### Mitigation: keep cores out of power-down (runtime only)

Disable `cpu-sleep-0` by name and keep WFI. Nothing persists: a reboot restores
the default. `config/kr260/kr260-deep-idle.sh` saves the original values in
`/run`, refuses to run if the mapping differs, reads back every write, and
restores everything if any step fails:

    bash kr260-deep-idle.sh check           # validate only; no root, no writes
    sudo bash kr260-deep-idle.sh status
    sudo bash kr260-deep-idle.sh apply      # cpu-sleep-0 disable=1 on all CPUs
    sudo bash kr260-deep-idle.sh rollback   # restore the saved values

Confirm the setting before any JTAG session: `status` must show
`state1 name=cpu-sleep-0 disable=1` and `state0 name=WFI disable=0` on cpu0–3.
A reboot silently drops it.

Equivalent one-liners (check `state1/name` first):

    for f in /sys/devices/system/cpu/cpu*/cpuidle/state1/disable; do echo 1 | sudo tee "$f"; done
    for f in /sys/devices/system/cpu/cpu*/cpuidle/state1/disable; do echo 0 | sudo tee "$f"; done

Cost, measured on `ina260_u14` with 60 × 1 Hz samples at idle (2.4 % CPU,
1.333 GHz fixed by the `userspace` governor): none measurable.
(2026-10-28; baseline 23:14Z, disabled 23:31Z and 23:32Z, boot `9b247aba`.)

| | `cpu-sleep-0` enabled | disabled (2 runs) |
|---|---|---|
| SOM power, mean | 3.618 W | 3.620 W |
| SOM power, min–max | 3.58–3.95 W | 3.59–3.82 W |
| SoC temp (AMS temp1), mean | 30.59 °C | 30.92 °C |
| `cpu-sleep-0` residency | 51–72 % of time | 0 (usage frozen) |
| CPUs online | 0–3 | 0–3 |
| Ping, LAN | 62/62, 0.252 ms avg | 124/124, 0.209 ms avg |

The power difference is +2 mW (+0.06 %), ±14 mW at 95 % confidence. That is
below the INA260's 10 mW resolution, so a few milliwatts of core leakage
cannot be resolved. The +0.3 °C is within the sensor's ±0.7 °C sample spread
and room drift. Wake latency improves from 900 µs to 1 µs. These figures are
for idle only; no load test was run.

`cpuidle.off=1` gives the same WFI-only behavior but is a boot argument. The
Kria boot script hard-codes `bootargs`; setting `LINUX_KERNEL_CMDLINE` in
`/etc/default/flash-kernel` and running `flash-kernel` rewrites both
`boot.scr.uimg` and the 64 MB `image.fit`. Prefer the runtime setting until the
supervised JTAG test below shows it is sufficient.

### Rules

- Never attach `hw_server`, `xsdb` or Vivado Hardware Manager to the board
  while Linux runs unless `cpu-sleep-0` is disabled on every CPU.
- Load bitstreams through Linux FPGA Manager (`xmutil` / `dfx-mgr`, state
  `operating`, with the boot overlay already in use), not `program_hw_devices`.
  JTAG programming of the PL under a running Linux can also hang the AXI bus.
- Keep the COM5 logger running during any JTAG session. It is the only record
  if Linux stops.

### Supervised validation order (owner present, each step separately approved)

1. With `cpu-sleep-0` disabled and the COM5 and ping logging running:
   `connect`, wait, then `jtag targets` and `targets`. All four A53s must read
   `Running`, and ping and SSH must stay up. Disconnect, then stop the
   `hw_server` that was started. If any core reads `Reset Catch` or
   `Stopped`, disconnect and stop `hw_server` at once. Do not resume, halt or
   reset a core without separate approval. If Linux does not recover, capture
   COM5 and have the owner power-cycle the board.
2. Only then: a supervised `rst -por` with Linux running. Verify that Linux
   and `eth1` return.
3. Only then: intentionally halt Linux and test whether JTAG recovers it.

Butters → Desktop Agent → JTAG stays unimplemented until all three pass.
