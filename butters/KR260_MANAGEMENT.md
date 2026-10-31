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

### JTAG reset (`rst -por`): validated 2026-10-29 (one supervised POR)

#### Does the KR260 wire the FTDI POR line? Yes: confirmed by the Step 2 POR below

| Evidence | Source | Kind |
|---|---|---|
| XSDB `rst -por` "generate[s] a pulse on the POR pin on the JTAG cable" | installed XSDB 2026.1 `help rst` | ours |
| Xilinx FTDI cables drive ADBUS6 = POR_B to `PS_POR_B` (open-collector, wired-OR) and ADBUS7 = SRST_B to `PS_SRST_B`; AMD's Kria FTDI table marks POR_B and SRST_B as used on **both** KV260 and KR260 | [Ethereal Wake summary](https://etherealwake.com/2024/06/xilinx-ftdi-jtag/) of AMD's Kria CC FTDI design table | secondary |
| `rst -por` over a KR260's own FTDI cable (`XFL1EAUJ5SPO`) recovered a wedged board (POR 4.7 s, boot ≈95 s) | [SoC-Labs `LOCKUP_RECOVERY.md`](https://github.com/SoC-Labs/Kria-260/blob/main/docs/LOCKUP_RECOVERY.md) | third-party, live |
| Our cable is `Xilinx SCK-KR XFL11R5N0BBZA`: FT4232H channel A, D2XX with no COM port. Channels C and D also have no COM port | Windows PnP, `jtag targets` | ours |
| UG1092: SW2 "Push button that resets the SOM via the device POR_B signal"; J1 can route the watchdog to SRST; J3 is direct JTAG that bypasses the FTDI | [UG1092 Interfaces](https://docs.amd.com/r/en-US/ug1092-kr260-starter-kit/Interfaces) | AMD, primary |
| SoC-Labs instead says "the carrier reset button is also `PS_SRST_B`" | SoC-Labs | contradicts UG1092 |
| Schematic net FT4232H ADBUS6 → buffer → `PS_POR_B` | AMD KR260 carrier schematic (login required) | **not checked** |

The local AMD install has no per-board metadata (`program_ftdi/boards.tcl`
holds only placeholders). We did not open the FTDI device to read its EEPROM:
opening channel A changes the state of the very pin wired to POR_B. The first
supervised `rst -por` is therefore also the wiring test.

UG1092's SW2 = POR_B, if true on this board, makes **SW2 a manual recovery
without unplugging 12 V**. That is also unverified, and worth testing once
under supervision.

#### What each reset does

- `rst -por`, if wired: a cold reset. The PS, the PL configuration and the
  debug logic reset, the boot-mode pins are resampled (UG1137), DDR contents
  are lost, and nothing is synced to disk. COM5's FTDI is USB-powered and
  stays up.
- `rst -system` (the default) preserves debug state, and the debugger then
  resumes the cores. It is the warm path. **Never used here.**
- `rst -processor`, `-cores` and `-cluster` are debug operations, not
  recovery.

The runtime deep-idle setting is gone after any reset. The deep-idle unit
re-applies it about 10 s into Linux, but **no debugger may stay attached
through a boot**.

#### Artifacts (development; nothing staged on the desktop)

| File | Role | sha256 |
|---|---|---|
| `config/kr260/kr260-jtag-por.tcl` | XSDB script | `a7709d8f…` (PR #9; Steps 1–2 below ran `e3338289…`) |
| `config/kr260/kr260-jtag-por.ps1` | Windows wrapper | `ee16ce33…` (PR #9; Steps 1–2 below ran `dc5502f7…`) |
| `tests/tcl/kr260_jtag_por_harness.tcl` | plain-Tcl harness | — |
| `src/butters/integrations/kr260_reset.py` + `scripts/kr260-reset-verdict` | verdict from the evidence | — |

`kr260-jtag-por.tcl check|por <cable-serial> <board-address> [confirm-por]`:

1. Probe the board (below). Anything but silence refuses
   (`REFUSED_REACHABLE before_attach`, `REFUSED_PROBE …`) without attaching.
   Attach.
2. Poll `jtag targets` (hw_server scans asynchronously) and require
   **exactly one** cable: an `SCK-KR` cable whose last word is the given
   serial, with exactly `xck26 04724093` then `arm_dap 5ba00477`. A second
   board on the same hw_server is a refusal, not a guess.
3. Select `PSU` and require it to be the active target.
4. Probe again while attached (`REFUSED_REACHABLE attached`).
5. `check`: print `RESULT CHECK_OK` and detach.
6. `por`: requires the literal `confirm-por`. Issues `rst -por` **once**,
   with only the time stamp between it and the second probe: no
   `rst -system`, no retry, a second call is refused. Prints
   `RESULT RST_ISSUED at=…` or `RESULT RST_ERROR …`.
7. Always detaches (`try … finally`).

The probe is one TCP connection attempt to the board's port 22, closed without
sending anything. An accepted or refused connection is `reachable`, silence
for 1 s (or the OS's "host is unreachable") is `dark`, and anything else is
`unknown`, which refuses like `reachable`. Each probe prints
`PROBE <moment> <verdict> at=<UTC>`, which the wrapper relays.

Every refusal happens before any reset and prints a specific `RESULT`
code. The file contains no other `rst`, and no `stop`, `con`, `mwr`, `rwr`,
`dow`, `fpga` or `exec`.

`kr260-jtag-por.ps1 -Mode check|por [-BoardAddress <ip>] [-Confirm confirm-por]`:

- refuses while any hw_server, xsdb, rdi_xsdb, cs_server or vivado process
  runs, so every debugger process left afterwards is its own;
- passes only fixed, validated arguments;
- has a 90 s timeout;
- stops every debugger process the moment XSDB exits;
- prints UTC-stamped lines ending in `RESULT`, and exits 0 only for
  `CHECK_OK` or `RST_ISSUED`.

Tests (2026-10-29):

- The harness ran under the desktop's Git `tclsh86.exe` (Tcl 8.6.17), which
  has no XSDB commands; the harness refuses to run otherwise. **52 of 52
  checks passed**, covering:
  - both success paths, and `RST_ERROR` with no fallback;
  - every refusal: usage, confirmation, an empty chain, another cable, two
    cables, a wrong product, a wrong IDCODE, a missing DAP, target selection,
    PSU not active, and connect failure;
  - a repeated reset;
  - disconnect on every path.
- Mutants with a `rst -system` fallback, a disabled chain check or a
  disabled confirmation were each caught.
- The wrapper parses, and its parameter validation rejects a bad mode or
  serial. No hw_server ran at any point.
- `tests/test_kr260_jtag_por.py` (pytest) has 18 tests: static safety checks
  on both files, all verdict states including this session's real warm,
  kexec and cold-boot logs, the parsers and the CLI. It runs the Tcl harness
  when `KR260_TCLSH` or `tclsh` is available.
- PR #9 added the reachability probes. The harness now has 99 checks, all
  passing on the same tclsh: probe order, every refusal at either probe,
  address validation, the verdict table and real local sockets. There are
  also 3 more static pytest checks. See *Windows runtime validation* below.

#### Telling a real reset from a returned command

`kr260-reset-verdict --issued-at … --boot-id-before … --console <COM5 log>
--heartbeat <ssh heartbeat> --ping <ping -D>` reads the existing monitoring
logs (COM5 logger, `jtag-heartbeat.sh`, `ping -D`). `RST_ISSUED` proves only
that XSDB accepted the command.

| Verdict | Evidence | Action |
|---|---|---|
| `por_confirmed` | new FSBL banner after the reset, then a new boot ID | verify health |
| `pending` / `booting` | under 15 s with no banner yet, or a banner while waiting for SSH | wait |
| `no_effect` | no banner within 15 s; the same boot still answers and ping stays up | the POR line did nothing. Stop and do not retry |
| `dark_without_console` | no banner; the board stopped answering | 12 V power cycle |
| `stuck_in_boot` | banner but no kernel in 60 s, no login in 150 s, or no SSH in 240 s | 12 V power cycle |
| `warm_path` | `reboot: Restarting system` (the PMUFW warm reset) | 12 V power cycle |
| `unexpected_restart` | a new boot ID without a banner (kexec-like) | investigate |

Abort rules, with no automatic retry, ever:

- `RST_ERROR`, a `REFUSED_*` code or `no_effect`: stop, keep the evidence and
  investigate.
- Any "12 V power cycle" verdict: preserve COM5, confirm no hw_server runs
  (process list only), and the owner power-cycles 12 V. Once validated, SW2
  may serve instead.

#### Proposed supervised experiment (two separately authorized steps)

Preflight:

- boot ID recorded; CPUs `0-3`; `eth1` up;
- deep-idle unit `active`, `cpu-sleep-0 disable=1` (required for any
  attach);
- no apt or dpkg running; the package holds in place; nothing staged;
- the reviewed FPGA overlay only;
- no debugger process on the desktop;
- COM5 logger, 1 Hz `ping -D` and the 10 s SSH heartbeat running, and the
  COM5 offset marked.

Then:

1. **Attach-only check.** Stage both files in
   `C:\Users\Daniel\Desktop\KR260-serial\por\` and compare hashes. Run
   `kr260-jtag-por.ps1 -Mode check`. Expect `CHECK_OK`, all four A53s
   `Running`, the debugger stopped, and Linux undisturbed for 5 minutes. This
   is the same attach that passed on 2026-10-28, now through the guarded
   script.
2. **One POR**, on the healthy board:
   - First `sync` over SSH, so the abrupt reset loses nothing written.
   - Run `kr260-jtag-por.ps1 -Mode por -Confirm confirm-por` once.
   - Expected: `RST_ISSUED`; COM5 shows the FSBL banner within about 2 s
     (`Reset Mode`, BL31, U-Boot), then the kernel, then login, as in a 12 V
     cycle. SSH back on a new boot ID in about 70–95 s.
   - `kr260-reset-verdict` must say `por_confirmed`. The deep-idle unit must
     have re-applied, and the holds and boot hashes must be unchanged.
   - If it says `no_effect`, the line is not wired on this board: record it
     and fall back to 12 V switching.
3. Optional, separately: press SW2 once on a healthy board and expect the
   same `por_confirmed` evidence. That validates the manual alternative.

#### Step 1, attach-only check: passed (2026-10-29)

Preflight:

- boot `bdaa97be`; CPUs `0-3`; `eth1` up; `running`;
- WFI `disable=0` and `cpu-sleep-0` `disable=1` on cpu0–3; the deep-idle
  unit active and enabled;
- nothing staged; no apt;
- COM5 logger open; 1 Hz ping and 10 s SSH heartbeat running;
- no debugger on the desktop.

Windows: no installer, update worker, hypervisor or pending reboot. An
interactive Codex CLI session (started 02:57:56Z from SSH) was idle; its
sandboxed commands had exited, and nothing touched Vivado, XSDB or the FTDI.

The staged files were hash-identical to `811a1cb` (`e3338289…`, `dc5502f7…`).

Ran once, 03:01:57.750–03:02:12.393Z: `kr260-jtag-por.ps1 -Mode check`. That
is `xsdb -no-ini kr260-jtag-por.tcl check XFL11R5N0BBZA`, which runs
`connect`, polls `jtag targets`, `targets -set -nocase -filter {name ==
"PSU"}`, `targets`, then `disconnect`. No `rst` exists on that path.

| Check | Result |
|---|---|
| Cable | `Xilinx SCK-KR XFL11R5N0BBZA` (one cable) |
| Chain | `xck26 04724093`, `arm_dap 5ba00477` |
| Target | `4* PSU` active |
| Cores | A53 #0–#3 `Running`; R5 `No Power`; RPU `Reset` |
| Result | `RESULT CHECK_OK cable=XFL11R5N0BBZA`, wrapper exit 0 |
| Detach | the wrapper stopped its own hw_server (pid 32920) at 03:02:12.343Z, 14.6 s after start; no debugger left; 3121 only `TimeWait` |
| Ping | 366/366, no gaps (before 37, during 14, after 301); max 0.28 ms |
| SSH | 35/35 on boot `bdaa97be`; per-CPU `arch_timer` rose every interval (≥427), no stalls |
| Linux | `running`, CPUs `0-3`, `cpu-sleep-0` still disabled, no kernel messages, COM5 silent |
| 5 min after detach | clean (until 03:07:18Z) |

The guarded path behaved as designed. The staged files were removed
afterwards. Step 2 (one `rst -por`) needs its own authorization.

#### Step 2, one JTAG POR: passed, the integrated FTDI asserts a real POR (2026-10-29)

Preflight:

- everything listed for Step 1 rechecked: boot `bdaa97be`, deep idle
  disabled on 4 cores and the unit active, nothing staged, no apt or DMA,
  holds in place, boot hashes unchanged, no debugger;
- Windows: no installer, update or pending reboot, and Codex idle;
- the owner confirmed being at the 12 V supply;
- scripts hash-identical to `811a1cb`;
- 1 Hz ping and SSH heartbeat restarted; `sync` at 03:26:27.484Z (Dirty
  0 kB).

Ran once: `kr260-jtag-por.ps1 -Mode por -Confirm confirm-por`.

- The chain validated (one `SCK-KR XFL11R5N0BBZA`, `xck26 04724093`,
  `arm_dap 5ba00477`, `4* PSU`), with all four A53s `Running`.
- `RESULT RST_ISSUED at=2026-10-29T03:26:40.012Z`; hw_server stopped at
  03:26:42.020Z; no debugger left (3121 only `TimeWait`).

| UTC | Event (COM5 logger stamps; the banner's is an upper bound) |
|---|---|
| 03:26:40.012 | `rst -por` issued |
| ≤03:26:42.070 | `Zynq MP First Stage Boot Loader`, then `Release 2023.2`, `MultiBootOffset: 0x40`, `Reset Mode: System Reset` |
| 03:26:42.103 / .305 | PMU Firmware 2023.2 / BL31 v2.8 |
| 03:26:43.283 | U-Boot 2023.01 (`Reset reason: SOFT`, as after a 12 V cycle) |
| 03:26:44.4 | ICMP down |
| 03:26:56.674 / .735 | `Starting kernel` / `Linux version 6.8.0-1015-xilinx` |
| 03:26:59.703 | 4 CPUs up |
| 03:27:09.670 | `sda2` mounted |
| 03:27:32.697 | starter-kit overlay applied |
| 03:27:47.8 | ICMP up (out ≈63 s) |
| 03:27:52.605 | `kria login:` |
| 03:27:56.682 | SSH on the **new boot `329affab`**, 76.7 s after the reset |

`kr260-reset-verdict` on the real logs gives `por_confirmed` (`fsbl_after_s`
2.058, an upper bound; `ssh_after_s` 76.67). There were no Linux shutdown
or warm-reset lines.

After the reset:

- identity and host key verified; `running`, no failed units; CPUs `0-3`;
  `eth1` 1000;
- the deep-idle unit re-applied (`cpu-sleep-0` disabled on 4 CPUs, WFI
  untouched);
- `fpga0` `operating` with the overlay applied and dfx-mgr active;
- root fsck `clean`, mounted `rw`;
- watchdogs `inactive`, `bootstatus=0`; `kexec_loaded=0`;
- boot hashes, holds and `update_initramfs=no` unchanged;
- production telemetry `online`, identity verified;
- stable since (heartbeats all OK, timers advancing).

Expected abrupt-reset artifact: journald replaced the previous boot's
`system.journal` as "uncleanly shut down"; its last entry is the 03:26:27
`sync`. New-boot warnings were the known cold-boot set (KASLR seed, TPM
self-test, `dmaproxy`/`usb5744` modules).

Two things this run taught:

- **The classifier first said `unexpected_restart`.** The logger stamps a
  line when it starts. The banner was appended to the unterminated
  `kria login: ` prompt, stamped 01:56:27, so the reset filter discarded it.
  `parse_console` now splits a banner or warm-reset marker off a line where
  it follows other text, and dates it by the next stamped line. The
  regression test uses this exact excerpt and fails on the old parser.
- hw_server was gone about 2 s after the reset, while the boot ROM and FSBL
  were starting. The boot was unaffected: a POR clears debug state, and
  deep idle is irrelevant before Linux. Keep that window short.

Conclusion:

- The KR260's integrated FT4232H **does** drive a usable power-on reset
  through `rst -por`. It is the cold path (boot ROM, then FSBL, then U-Boot),
  not the warm path that wedges, and it recovers in about 77 s.
- That makes JTAG POR a practical remote recovery for an unresponsive
  KR260, **as long as** Windows, the USB cable and XSDB work.
- It was tested on a healthy board. A truly wedged board (the warm-reboot
  hang) has not been tested yet. That case is the same reset mechanism
  SoC-Labs reports recovering.
- Like 12 V, it is abrupt (no unmount).

#### Butters → Desktop Agent recovery (PR #9: implemented, gated off, not live-validated)

Branch `feature/kr260-jtag-recovery-agent`, draft PR #9, stacked on PR #8.
The Windows Desktop Agent side is owned by Windows Codex. Its integration
contract is `KR260_JTAG_AGENT_HANDOFF.md`.
Nothing is installed on the desktop and nothing is deployed. Both gates ship
closed. All reset paths have run only against simulated hardware.

##### Architecture

```
Admin page ── POST /api/admin/tools/kr260/jtag {"operation","confirm":true}
   │  administrator session + CSRF; FRESH passkey bound to this one plan
   ▼
reset_kr260_jtag / check_kr260_jtag skill (one job, never retried)
   ▼
Kr260Adapter._jtag_run  ── gate actions.kr260_jtag_recovery
   │  fresh broker probe; dark ≥300 s over ≥2 probes; no kexec ≤600 s;
   │  no possibly-sent reset ≤1800 s (in memory and in the job store)
   ▼
AgentHub.kr260_jtag ── signed frame over the agent's pinned WSS, no parameters
   ▼
Desktop Agent Kr260Jtag ── gate kr260-jtag.toml enabled; hash + ACL of both files;
   │  debugger check, then one ping, last; one reset per 30 min
   ▼
powershell -File C:\ProgramData\Butters\kr260\kr260-jtag-por.ps1 (fixed argv)
   ▼
xsdb kr260-jtag-por.tcl: TCP probe; exactly one SCK-KR cable XFL11R5N0BBZA,
chain xck26 04724093 + arm_dap 5ba00477, target PSU; TCP probe; one
`rst -por`; disconnect
   ▼
Butters status(): new boot ID from the identity-verified board ⇒ confirmed
```

##### Authentication and authorization

| Layer | Requirement |
|---|---|
| Browser | administrator identity, CSRF token, `{"operation": "check"\|"reset", "confirm": true}` and nothing else |
| Plan | frozen server-side, skill named by the server; the passkey assertion is bound to that plan and consumed once (a replay is refused before any ceremony) |
| Passkey | `FRESH` for both operations, even in an elevated session |
| Skill | administrator audience, confirmation required, no local console, absent from the conversational planner |
| Butters gate | `actions.kr260_jtag_recovery.enabled` and `.configured` (overlay keys, machine-owned, ship false) |
| Agent gate | `kr260-jtag.toml` `enabled = true`, Administrators-owned; without it the agent does not even advertise the actions |
| Audit | every step: `pending_auth`, then the job outcome with its fixed code; arguments are always `{}` |

##### Safety conditions (each checked independently)

| Condition | Butters | Agent | Script |
|---|---|---|---|
| Board unreachable | dark (no ping and SSH `host_unreachable`/`ssh_timeout`) for ≥300 s over ≥2 probes, re-probed at execution | no ICMP echo reply from Windows, checked after the slow checks, immediately before PowerShell | no TCP answer from port 22 immediately before attaching and again immediately before `CHECK_OK`/`rst -por` |
| Not just restarted | no kexec request that may have run in the last 600 s, in memory and from the persistent job table (survives a Butters restart) | — | — |
| One reset per 30 min | in memory and from the persistent job table (survives a Butters restart) | in memory | one `rst -por` per process |
| Exact hardware | — | cable serial from local config | exactly one `SCK-KR` cable with that serial; the exact chain; PSU selected |
| Approved code | — | SHA-256 of `.ps1` and `.tcl`, admin-only ACL, before every run | — |
| No other debugger | — | none of hw_server, xsdb, rdi_xsdb, cs_server, vivado running | wrapper refuses too |
| No fallback | never `rst -system`, never a retry or a different action | no retry; a failure is reported once | no `rst` other than `-por`, no stop/con/mwr/dow/fpga |
| Cleanup | — | kills the PowerShell tree on timeout, then any debugger left (`debugger_cleanup`) | wrapper stops hw_server as XSDB exits |

##### Time of check to time of use

Measured on 2026-10-29: `rst -por` ran 12.4 s after XSDB started (start-up,
`connect`, chain scan). With only the agent's ping before PowerShell, a board
that came back by itself within about 13 s was still reset (and attached to,
which can hang a board that runs with deep idle).

Now the script probes the board itself immediately before `connect` and again
immediately before `rst -por`. No debugger operation was added, and there is
still exactly one attach. What remains:

| Window | Size | Why it cannot shrink further here |
|---|---|---|
| Last probe → `rst -por` | the probe's own 1 s of silence, plus microseconds | a silence can only be observed for some time; 1 s is about 1000× this LAN's SSH answer time |
| Board up, SSH not yet listening | a few seconds in every boot | Windows reports a refused connection only after about 2 s of retries (measured: 2.03 s), so within 1 s it reads as silence. The agent's ICMP ping just before PowerShell covers it. |
| Board alive with no network at all (FSBL, U-Boot, early kernel) | about 60 s of a boot | indistinguishable from dead over the network. Butters requires 300 s of darkness, longer than any boot (kexec about 70 s, cold about 80 s). |

So a board that recovers by itself during a request is refused whenever it
answers on the network before `rst -por`: `REFUSED_REACHABLE`, then
`failed` (nothing sent). The authorization is consumed; another attempt needs
a new request and a fresh passkey. The agent's own 30-minute latch still
counts that attempt.

##### State machine

Board (`jtag_recovery.board`): `online` (identity verified) ·
`degraded` (something answered, not the verified board) · `unreachable` (dark,
not yet 300 s/2 probes) · `unreachable_confirmed` · `monitoring_unavailable`
(the broker cannot observe; never treated as dark).

Request (`jtag_recovery.last_attempt.state`):

```
            refused before the wrapper ──────────────────────────► failed (not_sent)
request ─┤
            RST_ISSUED / RST_ERROR / NO_RESULT / timeout / lost agent
                 │  (all "may have reset": the 30-min interval starts)
                 ▼
             requested ──(verified board, different boot ID)──► confirmed   [latched]
                 │      ──(verified, previous boot unknown,
                 │         uptime < age)──────────────────────► booted      [latched]
                 │      ──(verified, same boot ID)────────────► no_effect   [latched]
                 └─(300 s without a verified new boot)────────► unconfirmed
                                  (a later new boot ID still turns it confirmed)
```

Evidence shown with it: `new_boot_id`, `same_boot_id`, `uptime_after_request`,
`network_only` (ping answers, Linux/SSH does not: booting or stuck),
`identity_mismatch` (another machine answers: never confirmed),
`monitoring_unavailable`, `no_answer`. `RST_ISSUED` alone is never a success
for the board: it is only `requested`.

Blocked reasons (`reset_blocked_reason`, explained on the page): `gate_closed`,
`agent_unconfigured`, `agent_disconnected`, `agent_awaiting_heartbeat`,
`agent_heartbeat_aging`, `agent_heartbeat_stale`, `agent_jtag_not_offered`
(agent gate off), `board_reachable`, `offline_not_established`,
`restart_recent`, `reset_rate_limited`.

##### Agent command contract

Request: `kr260.jtag_por.check` or `kr260.jtag_por.reset`, `parameters = {}`,
signed and bound to the connection like every agent frame. Result (no wrapper
text crosses the wire):

| Field | Values |
|---|---|
| `success` | true only for exit 0 with `CHECK_OK` (check) or `RST_ISSUED` (reset) |
| `jtag_result` | `CHECK_OK`, `RST_ISSUED`, `RST_ERROR`, `CONNECT_FAILED`, `REFUSED_USAGE`, `REFUSED_UNCONFIRMED`, `REFUSED_CHAIN`, `REFUSED_TARGET`, `REFUSED_REPEAT`, `REFUSED_DEBUGGER_ACTIVE`, `REFUSED_MISSING_SCRIPT`, `REFUSED_REACHABLE`, `REFUSED_PROBE`, `NO_RESULT` |
| `reset_may_have_run` | reset only; true for `RST_ISSUED`, `RST_ERROR`, `NO_RESULT` |
| `reset_issued_at` | reset only; the script's UTC stamp for `RST_ISSUED` |
| `debugger_cleanup` | `clean`, `forced` (the agent stopped leftovers), `failed` (some survived) |
| `error` | `jtag_failed`, or an agent refusal: `board_reachable`, `debugger_active`, `integrity_failed`, `rate_limited`, `busy`, `unsupported_action` |

Butters maps a failed result to `jtag_<code>` (for example `jtag_refused_chain`)
or the agent's refusal code. Butters rejects an unknown code, or a success
whose code does not match the action, as `malformed_result` (which counts as
"may have reset").

##### Expected diagnostic output

Agent log (`%LOCALAPPDATA%\ButtersAgent\agent.jsonl`): `kr260_jtag_loaded`
with the config's SHA-256 and `success` = enabled, or `kr260_jtag_disabled`
`invalid_kr260_config`; one `result` line per request with the action and
`success`. Butters: the job's `failure_code`, and the audit trail. On a
supervised run also keep the COM5 log and use `kr260-reset-verdict` as in Step
2 above; that is still the only source of the FSBL-banner evidence.

##### Timeouts and cooldowns

| What | Value |
|---|---|
| Dark period before recovery | 300 s, ≥2 probes (observer probes at most every 10 s on refresh, 45 s by poll) |
| After a kexec request | 600 s (Butters, persisted) |
| Stored job history unreadable or malformed | counts as a job just now: both waits stay in force; the status view keeps working |
| Wall clock not trusted (`clock_trust.py`) | JTAG check and reset, and the kexec restart, are refused with `clock_unsynchronized`; the dark period keeps counting |
| Between reset attempts | 1800 s (Butters, persisted; and the agent) |
| Confirmation window | 300 s, then `unconfirmed` (a late new boot still confirms) |
| Agent request window | 30 s: ≤5 s checks, XSDB 19 s inside a 21 s process deadline, ≤2 s cleanup, 2 s left for the transport |
| Script probes | 1 s each, two per run on a dark board |
| Skill job timeout | 60 s; the Admin page waits up to 75 s |

##### Read-only FPGA telemetry

The fixed telemetry command also prints `fpga_state` (FPGA Manager),
`fpga_status` (driver error text) and up to four `fpga_overlay=<name> <status>
<sha256 of /lib/firmware/<path>>` lines. The observer reports `fpga.state`
(`operating`, `operating_without_overlay`, `unconfigured`,
`unreviewed_design`, `error`, `unrecognized`, `unavailable`), the overlays, the
design named by a reviewed overlay hash (the kexec wrapper's
`SAFE_OVERLAYS`), and `starter_kit_loaded`. Verified read-only on the board
2026-10-29: `operating`, `k26-starter-kits_image_1 applied 1e998ffd…`. The
bitstream (`k26_starter_kits.bit.bin`, 1.8 MB, sha256 `f9dfe95d…`) is not hashed
per poll; the overlay names it. A broker without these lines reads as
`unavailable`. Nothing here can program the PL.

##### Safe deployment order (not performed)

Production `ba675b4` is PR #8's work **without `1e36895`** (monitor-off before
WoL, live-unvalidated) on `deploy/kr260-management-recovery`. PR #8 and PR #9
both contain `1e36895` in their history, so **neither may be merged or deployed
as a branch**. Build the candidate on the production line instead:

1. `git switch -c deploy/kr260-jtag-recovery deploy/kr260-management-recovery`,
   then cherry-pick, in order, PR #8's later commits `a1f0b48 be8f291 811a1cb
   2b2b4c9 ca7e813` and PR #9's commits.
2. Verify the exclusion. `git diff deploy/kr260-jtag-recovery
   feature/kr260-jtag-recovery-agent --stat` must list exactly the files and
   line counts of `git show --stat 1e36895`. Every `+` line of that diff must
   be a `+` line of `1e36895`, and every `-` line a `-` line of it.
   - Patch IDs do not match, because context differs: PR #9 also edits
     `broker.py`.
   - `git revert 1e36895` on PR #9 conflicts in `broker.py`, because
     production resolved `_metered_watts` by hand. Do not use either check.

   Dry run 2026-10-29, repeated after PR #9's probe commit:
   - every cherry-pick applied cleanly on `ba675b4`;
   - the diff against the PR #9 head is exactly `1e36895`'s 8 files
     (+967/−33). Counting duplicate lines, every `+` and `-` line is one of
     `1e36895`'s and none of its lines is missing;
   - relative to `ba675b4` the candidate touches no desktop, Wake-on-LAN,
     monitor or Sunshine file. Its only `broker.py` change is the FPGA lines
     of the telemetry command;
   - local branch `deploy/kr260-jtag-recovery-candidate`, not pushed.
3. Run both suites on the candidate. Compute the tree digest with `LC_ALL=C`.
4. **Butters first.** From the long-lived deploy worktree (it has the models
   tree): `install-beta1 --start`. It keeps `/opt/butters.previous` and
   restarts only `butters-web`. Then restart `butters-action-broker` (the
   telemetry command changed). Check that:
   - `/readyz` is ready and `DEPLOYMENT` names the candidate;
   - the broker still has `kr260.status=true`, `kr260.reboot=false` and
     `desktop.wake=false`;
   - the overlay has no `actions.kr260_jtag_recovery`;
   - `/api/admin/tools/kr260` shows `fpga` and
     `jtag_recovery.reset_blocked_reason = gate_closed`;
   - the agent still connects, with a hello unchanged.
5. **Desktop Agent second, gate off** (no Windows restart). Owner, in an
   elevated shell:
   - create `C:\ProgramData\Butters\kr260\` and protect it **before**
     copying anything. `C:\ProgramData\Butters` grants `BUILTIN\Users`
     inherited Write (checked 2026-10-29). With that ACL the agent refuses
     every run as `integrity_failed`. Mirror `install-task.ps1`:
     ```
     icacls C:\ProgramData\Butters\kr260 /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F "DESKTOP-G4CFVL1\Daniel:(OI)(CI)RX"
     icacls C:\ProgramData\Butters\kr260 /setowner *S-1-5-32-544 /T
     ```
     The agent's user needs read and execute only.
   - copy `kr260-jtag-por.ps1` and `.tcl` from the candidate, re-run
     `/setowner … /T`, and check both SHA-256 values;
   - write `C:\ProgramData\Butters\DesktopAgent\kr260-jtag.toml` (next to
     `agent.toml`, Administrators/SYSTEM-owned) with `enabled = false` and
     both hashes;
   - back up `venv\Lib\site-packages\butters_agent`, then install the
     agent **from local branch `deploy/desktop-agent-kr260-jtag` (`f2f216d`)**,
     not from the candidate. The desktop runs `origin/main`'s agent, and the
     candidate's agent would also ship PR #5's batched `desktop.app.list`
     snapshot (`7682879`, `d3f3be5`), which has never run on Windows.
     `f2f216d` is `origin/main` plus exactly PR #9's agent diff. Install it as
     an offline wheel: `pip install --no-index --no-deps --force-reinstall`.
     The kit is in `kr260-diagnostics/2026-10-30-deploy/windows-bundle/`;
   - restart only the Desktop Agent process: `Stop-ScheduledTask` then
     `Start-ScheduledTask -TaskPath '\Butters\' -TaskName DesktopAgent`. The
     task runs at `LeastPrivilege` as the logged-on user, ignores a second
     instance, and re-triggers every 5 minutes.
   Then check, in this order:
   - `agent.jsonl` shows `kr260_jtag_loaded` with `success: false`;
   - Butters shows the agent `connected` and `jtag_not_offered`;
   - Sunshine, SSH and the app actions are unaffected.
6. Stop there. Enabling either gate is the supervised validation below.

Compatibility: Butters accepts a hello only if every advertised action is in
its own `butters_agent.protocol.SCHEMAS`. Checked 2026-10-29 against the
agent installed on the desktop, production `ba675b4` and the candidate:

| Agent | Butters | Result |
|---|---|---|
| installed (5 actions) | candidate (7) | accepted |
| candidate, agent gate off | production | accepted; the hello is the same 5 actions |
| candidate, agent gate on | candidate | accepted; the JTAG actions are offered only now |
| candidate, agent gate on | production | **rejected at hello**: the whole agent disconnects |

So close the agent gate before any Butters rollback.

**Deployed 2026-10-30 03:03Z (Butters only).**
- **Production:** `040ca84`, tree digest `7cd33ea1…`; `/opt/butters.previous` is `ba675b4`.
- **Backup:** `/var/backups/butters-predeploy-20261030T030059Z`.
- **Approvals:** the installer preserved all 50, and the 47 existing effective ones are unchanged.
- **Gates:** `kr260.status=true`, `kr260.reboot=false`, `desktop.wake=false`; JTAG recovery `gate_closed`.
- **Health:** FPGA telemetry `operating`; the Desktop Agent reconnected 17 s after the restart.
- **Not done:** the Windows agent is not installed.

##### Rollback (no Windows restart)

- **Agent gate:** set `enabled = false` (or delete `kr260-jtag.toml`) and
  restart the agent process. The actions disappear from the hello.
- **Butters gate:** remove `actions.kr260_jtag_recovery` from
  `/etc/butters/assistant.local.toml` and restart `butters-web`.
- **Agent code:** restore the backed-up `site-packages\butters_agent` and
  restart the agent task as above. The `C:\ProgramData\Butters\kr260\`
  files are inert without the gate.
- **Butters code:**
  1. Close the agent gate first.
  2. Swap `/opt/butters.previous` back as for earlier releases.
  3. Restart `butters-web` and `butters-action-broker`.
  4. Verify `DEPLOYMENT`.

##### Known limitations

- Never run against hardware through this path. The Windows helpers have
  run natively, but only against simulated wrappers and a fake XSDB (below).
- Recovery of a genuinely wedged board is still untested (only a healthy POR
  passed).
- The scripts that ran live (Steps 1–2) are the pre-probe versions. The
  probe has run inside XSDB 2026.1 (Tcl 8.6.13), but never in a run that
  attached.
- The residual time-of-check windows above.
- The agent's task runs as the logged-on user with a limited token. Every
  live XSDB run so far was elevated (SSH). hw_server has inbound firewall
  rules and the FTDI driver needs no elevation, but its cable access under
  the agent's token is unverified.
- The agent's 30-minute latch is in memory: an agent restart clears it, while
  Butters' persisted check remains.
- The persisted waits use wall-clock job times, and this Pi 4 has no RTC.
  Butters therefore starts a KR260 recovery operation (JTAG check or reset,
  or kexec restart) only while the clock is trusted:
  - timesyncd's tmpfs marker `/run/systemd/timesync/synchronized` exists (a
    sync during this boot);
  - the marker is not from before this boot and not in the future;
  - wall and monotonic time have not diverged by more than 2 s since the
    marker last changed. That catches a step such as a manual `date -s`. It
    is latched until the next sync re-anchors.

  Otherwise the blocked reason is `clock_unsynchronized`. Nothing is sent and
  no wait starts. Synchronized earlier and offline now is trusted, so a kexec
  restart stays available during an Internet outage. `butters-web` startup is
  not delayed.

  Two durable records in the state directory, beside but independent of the
  job store, close the restart and database-loss cases. Both are written
  atomically (temporary file, fsync, rename, fsync of the directory) with a
  SHA-256 checksum; missing, damaged or edited reads as unknown.
  - `kr260-clock-baseline.json` holds the boot ID, boot time, wall time and
    marker time of the last trusted anchor, and a step flag.
    - A restarted Butters on the same boot checks the clock against it, so a
      step made while Butters was down, or just before a restart, is still
      caught (100 ppm drift allowance across syncs it missed).
    - A new boot must not have an earlier clock than the stored one.
    - Missing, corrupt or unwritable: untrusted until a sync this process
      sees.
  - `kr260-recovery-events.json` holds the latest kexec request and reset
    that may have run. The waits use the later of it and the job store.
    - Missing or corrupt means the history is unknown: both count as just
      now, written down once the clock is trusted. A lost history therefore
      costs one full interval, never a shorter one.
    - Only an existing record with no event adds no wait.
    - A malformed time counts as just now.

  Write-ahead:
  - A kexec request and a JTAG reset are written to the events record
    **before** they are sent.
  - If that write fails, the operation is refused with
    `safety_state_unavailable`, and nothing is sent.
  - A crash, a timeout or a lost reply after the write keeps the time.
  - Only a definite "not sent" outcome puts back the previous time, and only
    if the record still holds this request's time, so a newer event is never
    lost. If that undo fails, the longer wait stays.
  - Updates take a per-path thread lock plus `flock`, and use unique
    temporary files and complete writes.
  - A full disk, or a read-only state directory, raises and leaves the
    previous record intact.
  - The JTAG attach-only check sends no reset and records nothing.

  Clock baseline tolerance:
  - **No sync since the baseline:** 2 s. Wall and boot time cannot
    legitimately diverge then, however long the Pi has been offline.
  - **Syncs missed while Butters was down:** 100 ppm of the elapsed time,
    capped at 30 s. Anything larger waits for a sync this process sees.
  - **A sync seen live:** trusted, and it becomes the new baseline.
  - **Stored wall times** are treated as 32 s later
    (`MAX_UNDETECTED_SECONDS`), so no error the check can miss ends a wait
    early.

  The checksum detects accidental damage, not tampering. Anyone who can
  write `/var/lib/butters` can forge a record, or restore an older copy.

  On first deployment both records are absent. Recovery and kexec therefore
  stay blocked until timesyncd's next poll (up to about 34 min), and the waits
  then run once. The gates are closed today.

  Residual cases:
  - Wiping the whole state directory resets the clock baseline too. It is
    then untrusted until the next sync, after which a full interval
    applies.
  - Records stamped before this policy existed are trusted. Production had
    one, the 2026-10-28 kexec restart, stamped after that boot's sync.
- FSBL-banner evidence needs COM5, which is not wired into the agent.
- A slow `hw_server` start can exceed the 19 s XSDB deadline (expected run
  about 16.5 s). That gives `NO_RESULT`, which counts as possibly reset.
- Depends on Windows being up, the agent connected and the cable present.
  The independent recovery remains a 12 V switch.

##### Windows runtime validation (2026-10-29, owner away, nothing installed)

All of this ran from a temp folder, using the agent venv's Python 3.11.9 and
the PR #9 source, without touching the running agent. There was no JTAG, no
hw_server and no real wrapper in POR mode.

| Area | Result |
|---|---|
| Fixed tool paths | `powershell.exe`, `PING.EXE`, `tasklist.exe`, `taskkill.exe` present |
| Process list | `tasklist /FO CSV /NH`: 339 rows parsed in 0.22 s; no debugger running; a missing `tasklist` fails closed |
| Ping | healthy board `TTL=` in 1.03 s; an unused on-link address (ARP fails) and an unrouted one read silent in 2.8–3.0 s, exit 1; a missing `PING.EXE` fails closed |
| Hashes | `file_sha256` of the copied scripts equals the Linux SHA-256 (no CRLF rewrite by scp) |
| ACL | the production `apps.toml` passes (0.25 s); a user-writable temp file is refused `unsafe_registry_permissions`; a missing file is refused |
| Wrapper launch | a simulated wrapper with the real `param()` block receives the agent's exact argv for check and POR; a bad serial, mode or address is rejected by PowerShell before the body runs; exit codes and last-`RESULT`-wins parse; malformed and unknown codes read `NO_RESULT` |
| Timeout | a hanging simulated wrapper with two children: the whole tree (3 PIDs) was gone 4.1 s into a 4 s deadline; an unrelated process survived |
| Debugger cleanup | `stop_debuggers` on a uniquely named copy of `PING.EXE` killed only it |
| Whole action | `Kr260Jtag.invoke` with real ping, tasklist, hashing and PowerShell (ACL stubbed for the temp files): check and reset in 3.2–3.5 s; a second reset `rate_limited`; a healthy board `board_reachable` with no PowerShell; a wrong hash `integrity_failed` |
| Real wrapper, fake XSDB | check mode with `-Xsdb` pointing at Git `tclsh` and stub debugger commands: a dark address got `CHECK_OK` with both `PROBE` lines relayed; the healthy board got `REFUSED_REACHABLE before_attach` with no `connect` |
| Tcl harness | Git Tcl 8.6.17: 99 of 99, including real sockets: an accepting listener and a refused port read reachable, a silent address dark in <1.5 s |
| Probe inside XSDB | `xsdb.bat` 2026.1 (Tcl 8.6.13), with every cable command replaced before sourcing: KR260:22 reachable in 2 ms, two dark addresses dark in 1.0 s, a refused port reachable at 3 s; no hw_server or cs_server appeared |
| Tool versions | Vivado/Vitis 2026.1 `xsdb.bat` and `hw_server.bat` present; `hw_server.exe`, `xsdb.exe` and `cs_server.exe` built 2026-06-16; PowerShell 5.1.26100; Windows build 26200 |

##### Supervised validation plan (owner present, each step separately approved)

1. **Deploy and agent install, gates closed** (steps 4–5 above). Expect
   `gate_closed`, then `jtag_not_offered`.
2. **Agent gate on, healthy board.**
   - Expect the agent `connected` and `board_reachable` on the page.
   - Through the hub (or the open Butters gate), both actions are refused
     before PowerShell starts, and no `hw_server` starts.
   - Close the Butters gate again.
3. **The installed wrapper on a healthy board, as the agent's user.** Do
   this only with the deep-idle unit active (`state1/disable` = 1 on all
   four CPUs). From a non-elevated PowerShell on the desktop, run
   `kr260-jtag-por.ps1 -Mode check` from `C:\ProgramData\Butters\kr260\`.
   - Expect `PROBE before_attach reachable` and
     `RESULT REFUSED_REACHABLE before_attach`, exit 1.
   - Expect no hw_server at any point. XSDB never connects.
   - This is the first run of the installed files under the agent's token.
     The platform methods themselves already passed natively (below).
4. **The attach-only check on a dark board.** Requires a genuinely dark board.
   - Do not wedge a board deliberately without a separate decision.
   - Keep COM5 logging running.
   - Use the Admin "JTAG attach check" with a fresh passkey.
   - Expect `CHECK_OK` and `debugger_cleanup` `clean`.
5. **One Admin JTAG reset on that dark board.**
   - Expect `requested`, then `confirmed` with `new_boot_id` within about
     80 s, and `por_confirmed` from `kr260-reset-verdict`.
   - Afterwards verify the deep-idle unit, the package holds and the FPGA
     state.
6. Close both gates unless separately approved to keep them open.

### Before unattended JTAG recovery

- The deep-idle workaround is persisted (`butters-kr260-deep-idle.service`,
  installed 2026-10-29), but it is absent for the first ~10 s of every boot.
  Recovery tooling must never attach during a boot.
- Attach only when the board is confirmed unresponsive (ping, SSH and COM5
  all silent). Reset, detach and kill `hw_server` in one step. Allow one
  attempt, followed by a human.
- Normal restarts use kexec, validated 2026-10-29 including through Admin.
  The warm reboot wedges the board.
- Independent options:
  - a Home Assistant smart plug on the 12 V supply, for a true power cycle;
  - SW2, if it is POR_B as UG1092 says;
  - the Cadence watchdogs (`RuntimeWatchdogSec=0`), whose expiry takes the
    PMUFW path. They are not a recovery for the wedge.
