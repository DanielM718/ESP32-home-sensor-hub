# KR260 management and recovery

Butters observes exactly one board (`kria`, machine ID
`7a6ccc6abe67407b93558d1efcb36587`, `eth1`) through the root-only action
broker. The web process has no SSH key. Every gate ships closed:

| Capability | Gate | Shipped |
|---|---|---|
| Telemetry | `[kr260] enabled` (assistant), `"kr260.status"` + `[kr260]` (broker) | off |
| Linux restart (kexec) | `[actions.kr260_reboot]` (assistant), `"kr260.reboot"` (broker) | off |
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
| `restart_already_staged` | wrapper exit 10: a kexec image was already staged; nothing changed |
| `restart_precondition_failed` | wrapper exit 11–13: kernel/initrd missing or not the booted FIT, or no `root=LABEL=writable` |
| `watchdog_active` | wrapper exit 14: a hardware watchdog is running or `KExecWatchdogUSec` ≠ 0 |
| `pl_workload_active` | wrapper exit 15: the PL is not a reviewed configuration (see below) |
| `restart_failed` | wrapper exit 21–22: staging failed or systemd refused the kexec; unloaded |

Every wrapper code is a refusal before the kexec handoff, so it starts no
cooldown. A successful request returns `outcome: restart_requested` with
`restart_confirmed: false`. Status then reports `last_reboot_request.state`:

- `requested` until the board answers again;
- `confirmed` only when the identity-verified board reports a different boot
  ID;
- `unconfirmed` if 300 s pass without one.

An SSH drop during the handoff counts as possibly sent.

Observations are cached for `poll_interval_seconds` (45 s); an explicit
refresh re-probes at most every 10 s, so an offline board costs one ping and
one bounded SSH attempt per window. There is no background poller.

## Restart authorization (history; current design in "Production installation")

- 2026-10-28: a plain `systemctl reboot` over non-interactive SSH was denied by
  polkit ("Interactive authentication required").
- 2026-10-29: a NOPASSWD grant for exactly
  `/usr/bin/systemctl --no-wall --no-block reboot` was installed on the board.
  The warm reboot it allows **wedges this board** (below).
- The broker now sends `/usr/bin/sudo -n /usr/local/sbin/butters-kr260-kexec-restart`.
  The replacement
  [`config/kr260/butters-kr260-reboot.sudoers`](config/kr260/butters-kr260-reboot.sudoers)
  grants only that wrapper, with no arguments, and removes the warm-reboot
  grant when it is installed.
- Until that replacement is installed, the board still carries the old grant.
  `"kr260.reboot"` must stay `false`.

A second restart is refused for 300 s unless the board reports a new boot ID.
A restart with an unknown outcome counts as possibly sent.

Testing the sudoers boundary: `sudo -n -l <cmd>` is **not** a valid test.
`ubuntu` is in `sudo` with `(ALL:ALL) ALL` (password required), and once any
NOPASSWD rule exists, `-l` answers "permitted by some rule" for every
command. Test the boundary by *running* argv that cannot restart anything;
each must fail with "a password is required". On 2026-10-29, 16 such
variants of the old grant were refused, and `sudo -ll` showed the rule loaded
from `/etc/sudoers.d/butters-kr260-reboot` with `RunAsUsers: root` and
`!authenticate`.

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

### Third-party match: SoC-Labs KR260 lockup report (not our data)

[SoC-Labs `LOCKUP_RECOVERY.md`](https://github.com/SoC-Labs/Kria-260/blob/main/docs/LOCKUP_RECOVERY.md)
reports, for two KR260s:

- `sudo reboot` → PSCI SYSTEM_RESET → TF-A → PMUFW `XPfw_ResetSystem()` → a
  raw CRL soft SRST, "**without** the PMU-orchestrated idle/isolation drain".
- The board "goes dark: no PMU banner, no U-Boot, console UART held in reset
  (`RST_LPD_IOU2` bit 2)".
- Over JTAG, the A53 is halted in Reset Catch at `0xFFFF0000`.
- Their mechanism is that un-drained AXI transactions deadlock the LPD/FPD
  interconnect, so the next boot hangs. The report presents this as inferred,
  with no bus-level evidence.
- Workarounds: a `kexec` reboot ("~65 s **without** invoking the PMUFW
  reset"), and unattended `rst -por` over the KR260's FTDI cable (serial
  `XFL1EAUJ5SPO`; POR 4.7 s, boot ~95 s). "The carrier reset button is also
  `PS_SRST_B`", "Neither can a watchdog" recover the board.

AMD's [Restart solution](https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/18841820/Zynq+UltraScale+MPSoC+Restart+solution)
documents the hazard in general terms: "it is necessary to stop/complete any
ongoing transaction ... before resetting them", and PMU firmware must not
reset components "while leaving unfinished transactions in the interconnect".
Idling needs `ENABLE_NODE_IDLING` and `IDLE_PERIPHERALS`, and PL AXI masters
need user code to stop. [UG1137 System Reset](https://docs.amd.com/r/en-US/ug1137-zynq-ultrascale-mpsoc-swdev/System-Reset):
a system reset resets PS and PL, "Preserves debug states", and does not
resample boot-mode pins. Neither AMD page mentions KR260 warm-reboot hangs.

| Their observation | Ours (2026-10-29) | Match |
|---|---|---|
| warm `reboot` → no output before FSBL | clean shutdown, `reboot: Restarting system`, no FSBL banner for 10 min | yes |
| PSCI → PMUFW system reset | `shutdown_scope` = `[system]` | consistent; PMUFW path not traced by us |
| A53 in Reset Catch at `0xFFFF0000` | A53 #0 `Running`, then `Reset Catch` 7 s into our attach; #1–#3 `Reset`; PC not read | partial; our catch was caused by the attach, and theirs ran with a permanently attached `hw_server` |
| UART held in reset | COM5 silent | consistent; not verified by register |
| recovered by POR | recovered by 12 V power cycle | yes |
| AXI interconnect deadlock | not observed or tested | unestablished on both boards |

Our board: SOM `SMK-K26-XCL2G` rev 1, carrier `SCK-KR-G` rev 1 (`5100-01`).
Kria A/B boot firmware in QSPI (Image Selector, golden copy, Persistent
Register, Image A/B, Recovery). FSBL, PMUFW and BL31 v2.8 from release 2023.2;
U-Boot 2023.01. Both cadence watchdogs are inactive with `bootstatus=0`.

### Normal restart: kexec (dry run and one supervised restart passed)

Read-only checks on the board:

- `CONFIG_KEXEC=y`, `CONFIG_KEXEC_FILE=y`, `CONFIG_KEXEC_SIG=y` (not forced),
  `kexec_load_disabled=0`, lockdown `[none]`.
- No EFI; U-Boot boots a FIT image. `kexec-tools` 1:2.0.28-0ubuntu3 was
  installed on 2026-10-29 (below).
- Kernel and initramfs: `/boot/vmlinuz` and `/boot/initrd.img` (6.8.0-1015).
  `/sys/firmware/fdt` holds the DT U-Boot passed in (KR revB, with U-Boot
  fixups). Root is `LABEL=writable` on the USB SSD `sda2`.

Kexec skips the boot ROM, FSBL, PMUFW reset and U-Boot, so there is no FSBL
banner and no `SOFT` reset. It leaves in place everything those stages would
reset:

- The PL stays configured and keeps running, so a PL AXI master can still
  write to DDR while the new kernel boots. `dfx-mgr` reloads its default app
  (`k26-starter-kits`) at boot, reprogramming a live PL.
- PMUFW node and requirement state carry over.
- Clocks carry over (`clk_ignore_unused` is set).
- The debug state carries over. Secondaries go through PSCI CPU_OFF/ON, so a
  JTAG attach during kexec can catch them, exactly as at boot.
- The runtime deep-idle setting is lost, as with any boot.

For FPGA work: unload the PL app (`xmutil unloadapp`) and quiesce any
PL DMA before a kexec. Treat a design with AXI masters as unsafe to kexec
until it is shown to stop cleanly.

Pitfall: if the final `kexec -e` fails, `systemd-shutdown` falls back to an
ordinary reboot, which is the wedge. A wrapper must load and verify
(`/sys/kernel/kexec_loaded` = 1) before calling `systemctl kexec`. It must
never fall back to `systemctl reboot` itself.

#### Installed and dry-run (2026-10-29)

- `kexec-tools` 1:2.0.28-0ubuntu3 was installed by the owner at 00:34:56Z
  from the inspected `.deb` (sha256 `c647ace7…8424`), with
  `use_grub_config=false` preseeded.
  - Files added: `/sbin/kexec`, `/sbin/kexec-load-kernel`,
    `/sbin/vmcore-dmesg`, `/sbin/coldreboot` (a plain `/sbin/reboot`
    wrapper: never use it here), the `finalrd` hook
    `/usr/share/finalrd/kexec-tools.finalrd` (copies `/sbin/kexec` into the
    shutdown pivot root), and `/etc/default/kexec` (unused by our scripts).
  - 2.0.28 ships no services and no "handle reboots" option: `kexec.target`
    stays `disabled`, `reboot.target` has no kexec dependency, and `reboot`
    is unchanged.
- [`config/kr260/kr260-kexec-dryrun.sh`](config/kr260/kr260-kexec-dryrun.sh)
  (sha256 `4e3388db…`) passed at 00:39:01–00:39:03Z. It confirmed that
  `/boot/vmlinuz` and `/boot/initrd.img` match the SHA-1s U-Boot verified in
  the booted FIT (`b136ee87…`, `beb7508b…`) and that the cmdline carries
  `root=LABEL=writable`. Then load rc=0 → `kexec_loaded=1`, unload rc=0 →
  `kexec_loaded=0`, `DRY RUN PASS`.
- Afterwards: the same boot `208bb0b2`, CPUs `0-3`, `cpu-sleep-0` still
  disabled, `eth1` 1000, no failed units, `fpga0` `operating` with the
  overlay loaded. `/boot` and `/boot/firmware` were unchanged
  (`boot.scr.uimg` sha256 `690fe122…`).
- The kernel logged `PEFILE: Unsigned PE binary`. kexec-tools used
  `kexec_file_load`: it decompressed the gzip Image, and the kernel checked
  its PE signature. Unsigned images are accepted only because
  `KEXEC_SIG_FORCE` is unset and lockdown is `none`; either change would
  break kexec. On this path arm64 builds the next DT from its own boot-time
  FDT, which is the same source as `/sys/firmware/fdt`.

#### Restart script

[`config/kr260/kr260-kexec-restart.sh`](config/kr260/kr260-kexec-restart.sh)
runs the dry run's preconditions (nothing staged, FIT-matching kernel and
initrd, SSD root in the cmdline) and then two more checks:

- every `/sys/class/watchdog/*/state` must be `inactive`;
- `KExecWatchdogUSec` must be `0`.

A hardware watchdog survives kexec and, unfed, would fire the SRST that
wedges the board. The script then runs the same `kexec -l … --reuse-cmdline`
as the dry run, verifies `kexec_loaded=1`, and calls `systemctl --no-wall
--no-block kexec`. Any failure unloads and exits; it never requests a reboot.

Residual fallback risk: `systemctl kexec` runs `systemd-shutdown`, which
`finalrd` copies into the pivot root along with `/sbin/kexec`.
`systemd-shutdown` runs `kexec -e`, and if that returns it falls back to an
ordinary reboot, i.e. the wedge. The staged-image check makes this unlikely;
its recovery is a 12 V power cycle.

#### Supervised kexec restart procedure (owner present)

1. Preflight:
   - boot ID recorded; CPUs `0-3`; `eth1` up; `kexec_loaded=0`; both
     watchdogs `inactive`; no JTAG process on the desktop;
   - COM5 logger, 1 Hz ping and 10 s SSH heartbeat running;
   - no apt or dpkg jobs.
2. PL: the default `k26-starter-kits` app shows no PL or PS DMA activity
   (`zynqmp-dma` IRQs 0), so it stays loaded for the first test, and the
   result is recorded. Custom designs with AXI masters must be unloaded
   first (`sudo xmutil unloadapp`).
3. The owner runs `sudo bash /tmp/kr260-kexec-restart.sh` once. It must print
   `staged: kexec_loaded=1` and `kexec requested`. Any `ABORT` stops the test
   with nothing changed. Never retry automatically.
4. Watch COM5. Expected: systemd stop messages, then
   `kexec_core: Starting new kernel`, then `[    0.000000] Booting Linux` or
   `Linux version`, with **no FSBL banner**.
5. Success:
   - a new boot ID, the same kernel release, CPUs `0-3`, `eth1` 1000, SSH
     with the pinned host key;
   - `systemctl is-system-running` = `running`, Butters telemetry `online`;
   - `fpga0` `operating` and the dfx-mgr app loaded again;
   - no new kernel warnings beyond the known set;
   - `journalctl -b -1` ends with the kexec stop sequence;
   - `/boot` unchanged.
   Then re-stage the scripts (`/tmp` is cleared) and the owner re-applies the
   deep-idle workaround.
6. Abort and recovery (owner power-cycles 12 V; no JTAG):
   - `reboot: Restarting system` on COM5 means the fallback took the warm
     path: expect the wedge.
   - `Starting new kernel` followed by no kernel output within 15 s.
   - No login prompt within 150 s, or no SSH within 240 s.

#### Supervised kexec restart, 2026-10-29: passed

Preflight:

- boot `208bb0b2`; CPUs `0-3`; `cpu-sleep-0` disabled.
- `kexec_loaded=0`; both watchdogs `inactive`; `KExecWatchdogUSec=0`.
- The starter-kit overlay has no PL device nodes (only `firmware-name` and
  four `pl_resetn` resets); the UIOs are PS `axi-pmon`; all `zynqmp-dma` IRQs
  were 0; no XRT or UIO users.
- Script sha256 `138cea29…` (= `39d4af0`); no JTAG process; no apt or jobs.

The owner ran the script once.

| UTC | Event |
|---|---|
| 00:50:31.758 | `sudo bash /tmp/kr260-kexec-restart.sh` (journal) |
| 00:50:35.9 | SSH session closed; services stopping |
| 00:50:37.648 | `Reached target kexec.target`; 00:50:37.753 syncing filesystems; 00:50:38.248 journal stopped |
| 00:50:38.252 | last old-kernel console line |
| 00:50:38.469 | `Booting Linux on physical CPU 0x0` (217 ms later; no FSBL, U-Boot or `Restarting system`) |
| 00:50:38.5 | last ping reply |
| 00:50:40.886 | `smp: Brought up 1 node, 4 CPUs` |
| ≈00:50:53 | journald started (kernel +14.8 s) |
| 00:51:12.98 | starter-kit overlay applied (kernel +33.9 s) |
| 00:51:29.354 | ping back (network out ≈51 s) |
| 00:51:34.233 | `kria login:` |
| 00:51:43.093 | first SSH heartbeat on boot `63d18cfd` (≈71 s after the command) |

`kexec_core: Starting new kernel` is below the console loglevel, so it does
not appear on COM5. The handoff shows as the jump from kernel time
`[2094.5…]` to `[0.000000]`. Until NTP syncs, the new boot's first journal
entries carry the timesyncd-restored clock (00:50:18), about 20 s early.

Verified afterwards:

- Identity: new boot ID `63d18cfd`, same kernel build (#16, 2025-04-16), same
  cmdline; host key matches the pin.
- Health: `running` with no failed units; CPUs `0-3`; `eth1` 1000 with the
  same IPv4 address and default route.
- Root: fsck reports `writable: clean`, mounted `rw`.
- FPGA: `fpga0` `operating`, overlay `applied`, `dfx-mgr` active with
  "Default firmware detected: k26-starter-kits".
- Watchdogs `inactive`, `bootstatus=0`; `kexec_loaded=0`.
- Butters telemetry `online`, identity verified.
- Boot media unchanged (`boot.scr.uimg` `690fe122…`).
- Warnings and errors are only the known set. `Failed to find module
  'dmaproxy'/'usb5744'` also appears in the two previous cold boots.
- As expected, the deep-idle setting returned to its default (`disable=0`)
  and `/tmp` was cleared. The scripts were re-staged with committed hashes.

#### FPGA workloads across kexec

The PL keeps running through a kexec, so a PL bus master could write DDR under
the new kernel. The wrapper therefore restarts only reviewed PL configurations
and otherwise exits 15 (`pl_workload_active`) before staging anything.

Required:

- `/sys/class/fpga_manager/fpga0/state` readable.
- Every applied overlay `applied`, with a plain firmware `path` whose file in
  `/lib/firmware` hashes to an entry in `SAFE_OVERLAYS`.
  - Today that is only `k26_starter_kits.dtbo`, sha256 `1e998ffd…`: no PL
    device nodes, only `firmware-name` and the four `pl_resetn` lines.
  - dfx-mgr copies the loaded dtbo there.
- A PL in state `operating` with no reviewed overlay is refused.

Refused:

- the `zocl` or `dmaproxy` module loaded;
- any process holding `/dev/uio*` open, except the four PS AXI perf monitors
  (`ffa00000`, `fd0b0000`, `fd490000`, `ffa10000`);
- any process holding `/dev/dma_proxy*`, `/dev/xclmgmt*` or `/dev/zocl*` open.

To allow a new design, first review its masters and how it quiesces them on
`pl_resetn`. Then add its dtbo sha256 to `SAFE_OVERLAYS` in a reviewed
commit. The policy cannot see bus masters inside a reviewed bitstream; the
review is what covers that.

#### Production installation (steps 1–4 installed 2026-10-29; Butters not deployed)

Artifacts in `config/kr260/` at the commit being installed. Check every hash
on the board before installing:

| File | Installed as | sha256 |
|---|---|---|
| `kr260-kexec-restart.sh` | `/usr/local/sbin/butters-kr260-kexec-restart` (root:root 0755) | `9fa2e930…43e5` |
| `butters-kr260-reboot.sudoers` | `/etc/sudoers.d/butters-kr260-reboot` (root:root 0440), **replacing** the `systemctl reboot` grant | `ceda3a10…554a` |
| `kr260-deep-idle.sh` | `/usr/local/sbin/butters-kr260-deep-idle` (root:root 0755) | `2225e79b…ac90` |
| `butters-kr260-deep-idle.service` | `/etc/systemd/system/butters-kr260-deep-idle.service` (root:root 0644) | `cbbca1db…cbef` |

Order, owner present, each step checked before the next:

1. Stage the four files in `/tmp/butters-kr260/` and compare `sha256sum`
   against the table.
2. Wrapper:
   `sudo install -o root -g root -m 0755 kr260-kexec-restart.sh /usr/local/sbin/butters-kr260-kexec-restart`,
   then `sudo /usr/local/sbin/butters-kr260-kexec-restart check`. It must
   print `CHECK OK` and exit 0; it stages nothing.
3. Sudoers:
   - Validate the replacement first: `sudo visudo -cf butters-kr260-reboot.sudoers`.
   - Install it:
     `sudo install -o root -g root -m 0440 butters-kr260-reboot.sudoers /etc/sudoers.d/butters-kr260-reboot`,
     then `sudo visudo -c`.
   - Check without restarting, from the Pi through the broker key:
     `sudo -n -l` must list only
     `(root) NOPASSWD: /usr/local/sbin/butters-kr260-kexec-restart ""`.
   - Boundary execution tests, each of which must say "a password is
     required":
     - `sudo -n /usr/bin/systemctl --no-wall --no-block reboot`; this proves
       the old grant is gone, and with it the broker's warm-reboot path;
     - `sudo -n /usr/local/sbin/butters-kr260-kexec-restart check`;
     - `sudo -n /usr/local/sbin/butters-kr260-kexec-restart --help`;
     - `sudo -n /usr/bin/true`.
   - Never run the bare wrapper as a "test": that one is the restart.
4. Deep-idle service:
   - `sudo install -o root -g root -m 0755 kr260-deep-idle.sh /usr/local/sbin/butters-kr260-deep-idle`
   - `sudo install -o root -g root -m 0644 butters-kr260-deep-idle.service /etc/systemd/system/`
   - `sudo systemctl daemon-reload`
   - `sudo systemctl enable --now butters-kr260-deep-idle`
   - Check: `systemctl is-active butters-kr260-deep-idle` reports `active`,
     and `bash /tmp/kr260-deep-idle.sh status` shows `cpu-sleep-0 disable=1`
     and `WFI disable=0` on cpu0–3.
   - If the runtime workaround is already applied, the unit's `apply` keeps
     the saved originals.
5. Butters (separate approval): deploy the broker and web change, add the
   `[kr260]` broker section with `"kr260.status" = true`, and keep
   `"kr260.reboot" = false` until step 6 is authorized.
6. One supervised restart from Admin, with the procedure and abort criteria
   above. Expect `restart_requested`, then `last_reboot_request.state =
   confirmed` on a new boot ID, with deep idle disabled again by the unit.

Installed by the owner on 2026-10-29 (boot `63d18cfd`), each step verified
before the next:

- 01:16:45Z, wrapper: root:root 0755, sha256 `9fa2e930…`. `check` printed
  `CHECK OK`, staging nothing.
- 01:19Z, sudoers: `visudo -cf` and `visudo -c` parsed OK; the file is root:root
  0440.
  - `sudo -n -l` lists only
    `(root) NOPASSWD: /usr/local/sbin/butters-kr260-kexec-restart ""`.
  - `sudo -ll` finds no `systemctl` or `reboot` grant anywhere, so the
    warm-reboot path is gone.
  - Nine safe probes were refused with "a password is required": `wrapper
    check`, `wrapper --help`, `wrapper ""`, `/usr/bin/true`, `systemctl
    --version`, `systemctl --no-wall --no-block --version`, `/bin/sh -c true`,
    `-s true`, `-E /usr/bin/true`.
  - The old reboot argv was deliberately not executed as a test: had the
    grant survived, the test itself would have wedged the board.
- 01:20:42Z, deep-idle unit: enabled and `active (exited)`, `Result=success`.
  The journal shows `APPLIED: cpu-sleep-0 disabled on 4 CPUs; WFI untouched`,
  with the saved originals all `0`. Script root:root 0755 `2225e79b…`; unit
  root:root 0644 `cbbca1db…`.
- Afterwards: same boot ID, CPUs `0-3`, `kexec_loaded=0`, `running`, no
  failed units. Production broker unchanged: no `[kr260]` section,
  `desktop.wake=false`.

Rollback:

- Wrapper and sudoers: `sudo rm /etc/sudoers.d/butters-kr260-reboot
  /usr/local/sbin/butters-kr260-kexec-restart`. The broker then reports
  `reboot_unauthorized`. Do **not** reinstall the old `systemctl reboot`
  grant.
- Deep idle: `sudo systemctl disable --now butters-kr260-deep-idle` restores
  the saved values via `ExecStop`. Then `sudo rm
  /etc/systemd/system/butters-kr260-deep-idle.service
  /usr/local/sbin/butters-kr260-deep-idle && sudo systemctl daemon-reload`.
- kexec-tools: `sudo apt-get remove kexec-tools` (no services, so nothing
  else changes).

Constraints that remain:

- Kernel upgrades fail closed: after `flash-kernel` writes a new FIT, the
  wrapper refuses, and switching kernels needs a 12 V power cycle while the
  warm reboot wedges.
- The `systemd-shutdown` fallback: if `kexec -e` returns, systemd falls back
  to an ordinary reboot. That is the wedge.

#### Production deployment, 2026-10-29: monitoring on, restart off

- Deployed `ba675b4` (branch `deploy/kr260-gated-20261029`) at 01:35:36Z with
  `install-beta1 --start` from `worktrees/kr260-management-recovery-deploy`.
  - That commit is `be6d1fd` **minus `1e36895`** (the desktop monitor-off-before-WoL
    change, which has never been validated live). The 12 KR260 commits were
    cherry-picked onto the previous production commit `dbb5e02`, with one
    positional conflict resolved by dropping `_metered_watts` (it belongs to
    `1e36895`).
  - The deploy differs from `be6d1fd` by exactly `1e36895`'s changed lines,
    file for file. Its diff against `dbb5e02` touches only KR260 code,
    configs, the Admin KR260 UI and wording, and tests.
  - Suite on the deploy branch: 2264 passed, 2 skipped.
  - Installer: "Deployment preserves all 47 declared approvals"; rollback tree
    `/opt/butters.previous` = `dbb5e02`.
- Configuration (each candidate parsed by the new code as root before it went
  live):
  - `/etc/butters/action-broker.toml` gained `[kr260]` (host, `ubuntu`,
    `/etc/butters/kr260/id_ed25519`, machine ID) and the operations
    `"kr260.status" = true` and `"kr260.reboot" = false`.
  - `/etc/butters/assistant.local.toml` gained `[kr260] enabled = true`.
  - Nothing else changed. Broker gates are the previous eight plus
    `kr260.status`; `desktop.wake = false`. Web: `actions.kr260_reboot` is
    disabled and unconfigured.
- Restarted: `butters-web` (by the installer) and `butters-action-broker`.
  Not restarted: the agent ingress and the KR260.
- Before the deploy, production was byte-identical to `dbb5e02`: the C-locale
  tree digest matched `DEPLOYMENT`. A digest taken with `sort` under
  `en_GB.UTF-8` differs only because of collation.
- Backup: `/var/backups/butters-predeploy-20261029T013306Z` (root 0700). It
  holds both configs, `DEPLOYMENT`, SQLite-API copies of every
  `/var/lib/butters/*.sqlite3`, and `MANIFEST.txt`.
- Verified after deploy:
  - `/readyz` ready on all seven checks; no warnings.
  - The Desktop Agent reconnected; `desktop.sunshine_status` through the
    broker returned ok.
  - A live `kr260.status` through the broker as `butters` (audited
    `peer_uid=996`) returned `online`, identity verified (`kria`, the
    expected machine ID, boot `63d18cfd`), 31.5 °C, memory 11.8 %, and
    `eth1` up with 1 IPv4 and 5 IPv6 addresses.
  - The served `/assets/admin.js` is the new one.
  - The NAS Agent's last session was 2026-10-27; it was absent before the
    deploy too.
- Rollback: `sudo systemctl stop butters-web && sudo rm -rf /opt/butters && sudo
  mv /opt/butters.previous /opt/butters`, then restore both configs from the
  backup per its `MANIFEST.txt`, then `sudo systemctl start butters-web && sudo
  systemctl restart butters-action-broker`.
- Found during verification: the board's `unattended-upgrades` (allowed
  origins include `noble-security`) has pending `linux-image-xilinx`
  6.8.0-1036, `flash-kernel` and `u-boot-tools`. Installing them would
  regenerate `boot.scr.uimg` and `image.fit`. The wrapper would then refuse
  restarts (FIT mismatch, fail closed), and the next cold boot would run an
  unvalidated kernel. That is an owner decision (hold the packages or accept
  them); nothing was changed.

#### Supervised Admin restart procedure

1. Preflight:
   - Admin → KR260 shows `online · identity verified`;
   - board: `kexec_loaded=0`, the deep-idle unit `active`, no apt or dpkg
     running, `/boot/firmware` unchanged (`boot.scr.uimg` `690fe122…`);
   - COM5 logger, 1 Hz ping and SSH heartbeat running.
   - Optionally run `sudo /usr/local/sbin/butters-kr260-kexec-restart check`
     (owner, password). Keep the word `check`.
2. Open the gate on both sides:
   - broker `"kr260.reboot" = true` (then restart `butters-action-broker`);
   - overlay `[actions.kr260_reboot] enabled = true` and `configured = true`
     (then restart `butters-web`).
   - Confirm Admin shows the Restart button enabled.
3. The owner presses **Restart KR260 (kexec)…**, confirms, and completes the
   fresh passkey prompt.
4. Expected:
   - job result `outcome: restart_requested`, `restart_confirmed: false`;
   - the Admin "Last restart" reads `requested`, then `confirmed` once a
     refresh sees a new boot ID (about 70–90 s; refreshes are cached up to
     45 s, explicit refresh at most every 10 s);
   - COM5 shows the kernel restarting with no FSBL banner;
   - afterwards, the deep-idle unit is `active` with `cpu-sleep-0
     disable=1`.
5. If it fails:
   - a refusal code (`restart_precondition_failed`, `pl_workload_active`, …)
     means nothing was restarted;
   - `unconfirmed` after 300 s, or the abort criteria above, means manual
     12 V recovery.
6. Close the gate again on both sides and restart both services, unless the
   owner decides to keep it.

#### Boot-critical update holds (2026-10-29, owner)

`unattended-upgrades` installs from `noble` and `noble-security`; automatic
reboot is not set, so it defaults to false. It would have installed kernel
6.8.0-1036 (`linux-xilinx`, `linux-image-xilinx`, `linux-headers-xilinx`) and
`u-boot-tools`. Five eligible security updates (`udev`, `kmod`,
`klibc-utils`, `sg3-utils-udev`, `ntfs-3g`) also trigger `update-initramfs
-u`, whose post-update hook runs `flash-kernel` and rewrites `boot.scr.uimg`
and `image.fit`. Applied:

    sudo apt-mark hold linux-xilinx linux-image-xilinx linux-headers-xilinx flash-kernel u-boot-tools xlnx-image-update
    sudo sed -i 's/^update_initramfs=yes$/update_initramfs=no/' /etc/initramfs-tools/update-initramfs.conf
    # backup: /etc/initramfs-tools/update-initramfs.conf.butters-backup-20261029

Verified:

- the six packages are `hi` (held, installed);
- 6.8.0-1015 is the only kernel installed and running;
- hashes unchanged: `boot.scr.uimg` `690fe122…`, `image.fit` `b007246e…`,
  `initrd.img-6.8.0-1015-xilinx` `cdfd3b01…`;
- `apt-get -s dist-upgrade` keeps the six back and still offers `udev`,
  `kmod`, `openssl` and `sudo`.

With `update_initramfs=no` those packages update their userspace files, but
the initramfs and FIT stay as validated. A held kernel never rebuilds.

Rollback, as planned maintenance followed by a supervised cold boot:

    sudo apt-mark unhold linux-xilinx linux-image-xilinx linux-headers-xilinx flash-kernel u-boot-tools xlnx-image-update
    sudo sed -i 's/^update_initramfs=no$/update_initramfs=yes/' /etc/initramfs-tools/update-initramfs.conf

#### Supervised Admin restart, 2026-10-29: passed end to end

Gate opened temporarily at 01:51:35Z on both sides: broker
`"kr260.reboot" = true`, and the overlay `[actions.kr260_reboot] enabled =
configured = true`. Preflight checks were all green, with boot `63d18cfd`.
The owner clicked once in Admin and completed the passkey prompt.

| UTC | Event |
|---|---|
| ≈01:55:17 | Admin audit: `pending_auth`, then fresh `webauthn`, `completed` (job `ac-2WTK7…`, 6.7 s) |
| 01:55:26 | broker: `operation=kr260.reboot outcome=accepted result=ok peer_uid=996`, the only one |
| board | `sudo` of `/usr/local/sbin/butters-kr260-kexec-restart` with no TTY (the broker over SSH); `kexec.target`; filesystems synced |
| 01:55:32.660 → .815 | last old-kernel console line → `Booting Linux` (155 ms). **No** FSBL, PMU, BL31, U-Boot or `Restarting system` |
| 01:55:33.8 – 01:56:23.4 | ICMP down (≈50 s) |
| 01:55:39 | `butters-kr260-deep-idle` re-applied `cpu-sleep-0` disabled on 4 CPUs, WFI untouched |
| 01:56:27.3 | `kria login:` |
| 01:56:33.7 | SSH on boot `bdaa97be` (≈67 s after the click) |

State transitions:

- the job result was `outcome: restart_requested`, `restart_confirmed:
  false`, previous boot `63d18cfd`; no premature success;
- after the board answered on the new boot, Admin → KR260 showed `Last
  restart: confirmed` (owner-observed refresh);
- `unconfirmed` did not occur.

After the restart:

- identity and host key verified; `running` with no failed units; CPUs
  `0-3`; `eth1` 1000;
- `fpga0` `operating` with the overlay applied and dfx-mgr active;
- root fsck `clean`, mounted `rw`;
- watchdogs `inactive` with `bootstatus=0`; `kexec_loaded=0`;
- holds and `update_initramfs=no` unchanged; boot media hashes unchanged;
- only the known warnings.

Gate closed again:

- the broker first (01:57:46Z, so a second click would be refused while the
  web process kept its record);
- then the web overlay, after the owner had seen `confirmed`, with
  `butters-web` restarted at 02:03:07Z.

The live configs are byte-identical to the pre-test closed copies: broker
`kr260.status` on, `kr260.reboot` off, `desktop.wake` off; web `kr260`
enabled, `actions.kr260_reboot` disabled and unconfigured. Telemetry
afterwards: `online`, identity verified, boot `bdaa97be`.

Remote restart stays disabled. Keeping it on permanently needs separate
authorization.

#### Manual 12 V recovery (the board does not come back)

1. Do not retry the restart, issue `reboot`, attach JTAG or `rst -por`.
2. Preserve evidence:
   - the COM5 log (last line `Restarting system` = the fallback; no
     `[    0.000000]` after the old kernel's last line = a stuck handoff);
   - the Pi ping and heartbeat logs.
3. Confirm no `hw_server`/`xsdb` runs on the desktop, by process list only.
4. Owner: unplug the KR260 12 V barrel, wait 10 s, reconnect, and leave
   Micro-USB connected.
5. Expect the FSBL banner within about 1 s, ping in about 85 s and SSH in
   about 70–90 s. Verify the new boot ID, identity and host key, `running`,
   CPUs `0-3`, `eth1`, `fpga0` and telemetry. Re-stage the `/tmp` scripts;
   the deep-idle unit, once installed, re-applies itself.

### Dead-Linux recovery: JTAG POR (wiring unconfirmed)

- XSDB `help rst`: `-por` pulses "the POR pin on the JTAG cable".
- SoC-Labs ran `rst -por` successfully on a KR260 FTDI cable (`XFL1…`, the
  same family as ours, `XFL11R5N0BBZA`). That is strong third-party evidence
  that the carrier wires it, but it is not our board or revision.
- Our own non-invasive evidence is only suggestive: Windows exposes FT4232H
  interfaces C and D with no COM port, as D2XX-only, so they are not UARTs.
- No public KR260 schematic was found that ties an FT4232H line to
  `PS_POR_B`. The U-Boot KR revB DT shows the SLG7XL45106 reset chip only for
  USB, SD, hub and GEM resets.
- Check the AMD KR260 carrier schematic (design files, login required)
  before relying on it.
- The reset button is `PS_SRST_B` (per SoC-Labs), the same path as the
  failing warm reset.

### Watchdog: incidental to this failure, and no recovery path

- `RebootWatchdogSec=10min` made systemd open `/dev/watchdog0` (FPD
  `fd4d0000`) during shutdown. Setting 10 min failed (max 516 s), so it ran at
  its 60 s default.
- The system reset happened about 4 s later and resets the PS watchdogs too,
  so it cannot have fired.
- Nothing arms a watchdog again before Linux, and its expiry action is the
  same PMUFW reset, or APU restart only in a recovery-enabled PMUFW, which
  this build is not known to be.
- A watchdog is therefore expected not to recover an interconnect-level
  wedge. SoC-Labs reaches the same conclusion. Leaving it as is costs
  nothing; `RuntimeWatchdogSec` stays 0.

### Recommended architecture

| Operation | Recommended | Why | Not |
|---|---|---|---|
| Normal restart of a live board | validated kexec wrapper through the one-argv sudoers grant | avoids the PMUFW warm reset; graceful | `systemctl reboot`: shown to wedge this board |
| Recovery of a dead board | external 12 V switching (HA smart plug) | a true power-on of SOM and carrier; independent of Windows, `hw_server`, cable wiring and debug state | warm reset, the reset button, a watchdog |
| Recovery, secondary | JTAG `rst -por`, only after the wiring is confirmed and one supervised test passes | no power interruption to the SSD; documented to work on a KR260 by a third party | `rst -system` (preserves debug state) |

Both POR and 12 V switching are abrupt (no filesystem sync). The board booted
with no filesystem errors after two power cycles on 2026-10-28/29. No
`fsck` was run.
12 V switching also cuts power to the SSD.

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
