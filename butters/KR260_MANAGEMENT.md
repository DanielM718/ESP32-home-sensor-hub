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

## Reboot authorization (installed on the board; broker gate off)

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
   must print the command path and exit 0. `sudo -n -l <cmd>` is **not** a
   boundary test: `ubuntu` is in `sudo` with `(ALL:ALL) ALL` (password
   required), and once any NOPASSWD rule exists `-l` answers "permitted by
   some rule" for every command. Test the boundary by *running* argv that
   cannot reboot, each of which must fail with "a password is required":
   `/usr/bin/true`, `/bin/sh -c true`, `systemctl --version`,
   `systemctl --no-wall --no-block status`,
   `systemctl --no-wall --no-block reboot --help`,
   `systemctl --version --no-wall --no-block reboot`, `-s true`, `-i true`,
   `-E /usr/bin/true`. On 2026-10-29 all 16 such variants were refused, and
   `sudo -ll` showed the rule loaded from
   `/etc/sudoers.d/butters-kr260-reboot` with `RunAsUsers: root` and
   `!authenticate`.
3. Add the `[kr260]` section from `config/action-broker.example.toml` to
   `/etc/butters/action-broker.toml`, set `"kr260.status" = true`, restart
   `butters-action-broker`, enable `[kr260]` in the assistant overlay, and
   confirm Admin → KR260 shows `online · identity verified`.
4. Only then, supervised: set `"kr260.reboot" = true` and
   `[actions.kr260_reboot] enabled = configured = true`, run one reboot from
   Admin, and confirm `last_reboot_request.boot_changed` becomes `true` and
   `eth1` returns. Decide afterwards whether the gate stays open.

**Blocked: a Linux warm reboot does not restart this board (2026-10-29).**
Do not set `"kr260.reboot" = true` until a warm reboot is shown to come back.

### Linux reboot test, 2026-10-29: shut down cleanly, did not restart

Issued once through the dev broker's `kr260_reboot()` (identity check, then
the exact argv) with COM5, a 1 Hz ping and a 10 s SSH heartbeat running, and
`cpu-sleep-0` disabled at runtime.

| UTC | Event |
|---|---|
| 00:04:50.856 | identity check: machine ID matched |
| 00:04:51.483 | `sudo -n /usr/bin/systemctl --no-wall --no-block reboot`, exit 0 |
| 00:04:55 | journal: `systemd-reboot.service` finished; systemd and systemd-shutdown "Using hardware watchdog 'cdns_wdt watchdog' … /dev/watchdog0", "Failed to set timeout to 10min: Invalid argument" (`RebootWatchdogSec=10min`; watchdog0 max is 516 s); filesystems synced |
| 00:04:59.808 | COM5: `reboot: Restarting system` (the last byte) |
| 00:05:00.097 | last ping reply |
| 00:05:00–00:15:27 | no COM5 output at all (no FSBL banner); ARP `FAILED`/`INCOMPLETE` |

The firmware's `shutdown_scope` is `[system]`, so Linux asked the PMU firmware
for a full system reset (not a POR). By comparison, every cold boot prints the
FSBL banner within about a second.

Read-only JTAG inspection of the hung board (00:13:03–00:13:18Z,
`connect; jtag targets; targets; targets; disconnect`; `hw_server` stopped
after):

| | 00:13:11Z | 00:13:18Z |
|---|---|---|
| A53 #0 | `Running` | `Reset Catch, EL3(S)/A64` |
| A53 #1–#3 | `Reset` | `Reset` |
| R5 #0–#1 | `RPU Reset` | `RPU Reset` |

Core #0 was executing, eight minutes after the reboot, without any console
output. It then went through a reset within 7 s, and the attached debugger
caught it. That Reset Catch was caused by the inspection, so it does not
support "a debug state retained from the 23:44Z attach caught the core". The
first listing (`Running`) is evidence against that idea, though not proof. A
reset inside a 7 s window after 8 silent minutes suggests core #0 is in a
silent reset loop before FSBL prints. A firmware-level warm-reset failure
(boot ROM, Kria image selector or FSBL, or the board's reset path) fits, but
is not proven: only one reset was observed. A Linaro KV260 change notes "kv260
HW issues" that need a power cycle before capsule updates, which is related at
most.

A 12 V power cycle (00:15Z) restored a normal boot. FSBL 00:15:28.213Z (`Reset
Mode: System Reset`, `MultiBootOffset: 0x40`) → BL31 v2.8 → U-Boot 2023.01
(`Reset reason: SOFT`) → kernel 00:15:42.9Z → 4 CPUs → ping 00:16:32.5Z →
login 00:16:37.5Z. New boot `208bb0b2`, identity and host key matched, no
failed units, telemetry `online`. Only the known warnings appeared. The reboot
wiped `/tmp` (the staged idle script) and the runtime idle setting.

Next questions, each needing separate approval:

- Does a warm reboot fail with no prior JTAG attach this power cycle? This
  separates a firmware/board warm-reset fault from a debugger effect.
- Does it fail with `RebootWatchdogSec=0`? This rules out the watchdog armed
  during shutdown.
- Do `shutdown_scope` `ps_only` or `subsystem` behave differently?

Until a warm reboot is shown to work, the only reset known to recover this
board is a power cycle, so a smart plug on its 12 V supply is the most
reliable remote recovery.

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

1. **Passed 2026-10-28 23:44:54–23:53:12Z** (boot `9b247aba`). Setup:
   `cpu-sleep-0` `disable=1` and `WFI` `disable=0` on cpu0–3, with COM5, a
   1 Hz ping and a 10 s SSH heartbeat running. The script
   (`connect; jtag targets; targets ×19; disconnect`) used no stop, resume,
   reset, register, memory or programming command.

   | Check | Result |
   |---|---|
   | Attach | 180 s; 19 inspections, all four A53s `Running` every time |
   | Linux CPU activity | per-CPU `arch_timer` counts rose every 10 s on cpu0–3 (≥397/interval); CPUs `0-3` online |
   | Ping | 530/530 (before 30, during 188, after 311); max 0.80 ms during attach |
   | SSH heartbeat | 51/51; boot ID unchanged |
   | Kernel / console | no kernel messages; COM5 silent (no stall, oops or panic) |
   | Detach | `disconnect`; `hw_server` stopped 23:48:06.8Z; no process left, port 3121 only `TimeWait` |
   | 5 min after detach | clean |

   This shows that, with `cpu-sleep-0` disabled, a read-only attach no longer
   disturbs Linux. It does not show that deep idle was the sole cause of the
   earlier hangs. No deep-idle-on control was run, by decision: it would
   deliberately hang the board.
2. JTAG reset: see below. Not yet run.
3. Only then: intentionally halt Linux and test whether JTAG recovers it.

Butters → Desktop Agent → JTAG stays unimplemented until all three pass.

### JTAG reset (`rst -por`): plan, not yet run

The installed XSDB (2026.1) `help rst` says:

- `rst -por` "generate[s] a pulse on the POR pin on the JTAG cable". It works
  only if the KR260 carrier wires that FT4232H line to the SOM's PS_POR_B.
  That is unconfirmed: the first supervised attempt is also the test. If it is
  wired, it is a cold reset. The whole PS, the PL configuration and the debug
  logic reset, the boot-mode pins are resampled (UG1137), DDR contents are
  lost, and nothing is synced to disk.
- `rst -system` (the default) resets the system but, per UG1137, preserves
  debug state, and by default the debugger resumes the cores. A reset catch
  could therefore survive the reset and trap the cores at FSBL or at Linux
  secondary-CPU bring-up. Do not use it for recovery.
- `rst -processor`, `-cores` and `-cluster` reset cores only and leave them
  suspended. They are debug operations, not recovery.

After a reset the runtime deep-idle workaround is gone. `cpu-sleep-0` is
active again as soon as Linux boots, so **`hw_server` must be gone before the
board boots**. The reset script issues `rst -por`, then `disconnect`, and the
wrapper kills the `hw_server` it started in the same step, within about a
second. Never leave `hw_server` attached through a boot.

Proposed reset script, read-only apart from the one reset:

    connect
    after 5000
    targets -set -nocase -filter {name =~ "PSU"}
    puts [targets]                          ;# pre-reset core states, for the record
    if {[catch {rst -por} err]} { puts "RST ERROR: $err" } else { puts "RST ISSUED" }
    disconnect

Confirm the reset executed, and the return to Ubuntu, by four independent
signals:

- COM5 shows a new `Zynq MP First Stage Boot Loader` banner within about 2 s.
- ICMP drops, then returns about 70 s later.
- `/proc/sys/kernel/random/boot_id` changes and `uptime` is small.
- `journalctl --list-boots` gains an entry, and the previous boot ends with no
  shutdown sequence.

U-Boot's `Reset reason` is not a usable signal: it read `SOFT` even after a
12 V power cycle, probably because of Kria's image-selector soft reset.

Success means all of these:

- COM5 reaches `kria login:`.
- SSH works with the pinned host key, with the expected hostname and machine
  ID.
- `systemctl is-system-running` reports `running`.
- CPUs `0-3` are online and `eth1` is up at 1000.
- Butters telemetry reports `online`.
- No new kernel warnings beyond the known set (TPM self-test, `mtdoops`,
  `OF: overlay`).

Abort and recovery criteria (no automatic retry, ever):

- `RST ERROR`, or no FSBL banner within 10 s: the reset did not execute. Check
  that Linux is still up (ping, SSH, COM5). Do not retry; investigate cable POR
  wiring.
- FSBL but no `Starting kernel` within 60 s, no login prompt within 150 s, or
  no SSH within 240 s: the boot is stuck. Preserve COM5, confirm no
  `hw_server` is running, and have the owner power-cycle the 12 V supply.
- Linux is up but degraded (`eth1` down, CPUs missing, failed units): collect
  the journal, do not reset again, and report.

After a successful boot, the owner re-runs
`sudo bash kr260-deep-idle.sh apply` before any further JTAG attach.

### Before unattended JTAG recovery

- Persist the deep-idle workaround: a root systemd oneshot that runs
  `kr260-deep-idle.sh apply` at boot. Even then it is absent for the first
  ~20 s of every boot, so recovery tooling must never attach during a boot.
- Attach only when the board is confirmed unresponsive (ping, SSH and COM5
  all silent). Reset, detach and kill `hw_server` in one step. Allow one
  attempt, followed by a human.
- The restricted Linux reboot was validated first (2026-10-29). Its
  authorization path works, but the board did not come back from the warm
  system reset (see "Linux reboot test" above). A `rst -por` would be a
  different reset (cable POR line) and is still untested. JTAG remains the
  path only for a board whose Linux is already dead.
- Simpler independent options:
  - a Home Assistant smart plug on the KR260's 12 V supply, for a true power
    cycle;
  - the Cadence watchdogs (`cdns-wdt`, currently unused:
    `RuntimeWatchdogSec=0`), for self-reset. Check that a watchdog-driven
    system reset does not preserve a debugger reset catch, as `rst -system`
    would.
