#!/bin/bash
# KR260 restart through kexec, avoiding the PMUFW warm system reset that wedges
# this board. Same preconditions and the same load invocation as
# kr260-kexec-dryrun.sh; then a clean `systemctl kexec`. It never asks for a
# reboot: if anything is not exactly right it unloads and exits.
#   sudo bash kr260-kexec-restart.sh          restart (the only form sudoers grants)
#   sudo bash kr260-kexec-restart.sh check    run every precondition, stage nothing
# Installed as /usr/local/sbin/butters-kr260-kexec-restart (root:root 0755).
# Residual risk: if the final `kexec -e` inside systemd-shutdown fails, systemd
# itself falls back to an ordinary reboot (the wedge). The checks below make
# that unlikely; recovery is a 12 V power cycle.
# KX_* variables exist only so tests can point at fakes; sudo strips them.
set -u
KEXEC=${KX_KEXEC:-/sbin/kexec}
SYSTEMCTL=${KX_SYSTEMCTL:-/usr/bin/systemctl}
DUMPIMAGE=${KX_DUMPIMAGE:-/usr/bin/dumpimage}
LOADED=${KX_LOADED:-/sys/kernel/kexec_loaded}
WATCHDOGS=${KX_WATCHDOGS:-/sys/class/watchdog}
BOOT=${KX_BOOT:-/boot}
FIT=${KX_FIT:-/boot/firmware/image.fit}
FDT=${KX_FDT:-/sys/firmware/fdt}
CMDLINE=${KX_CMDLINE:-/proc/cmdline}
FPGA_STATE=${KX_FPGA_STATE:-/sys/class/fpga_manager/fpga0/state}
OVERLAYS=${KX_OVERLAYS:-/sys/kernel/config/device-tree/overlays}
FIRMWARE=${KX_FIRMWARE:-/lib/firmware}
UIO=${KX_UIO:-/sys/class/uio}
PROC=${KX_PROC:-/proc}
MODULES=${KX_MODULES:-/proc/modules}
RELEASE=${KX_RELEASE:-$(uname -r)}
ROOT_ARG=root=LABEL=writable
# The PL keeps running across kexec, so a PL bus master could write DDR under the
# new kernel. Only reviewed PL configurations are restarted: each applied overlay
# must be one of these sha256s. k26_starter_kits.dtbo (2024-04-10) declares no
# PL devices, only firmware-name and the four pl_resetn lines. Add a hash only
# after reviewing that design's bus masters.
SAFE_OVERLAYS="1e998ffd5d33a05da3746df3603e65603e1628006bc878209a836cc9bd3c4ae1"
# Tests (KX_KEXEC set) may supply their own fixture hashes.
[ -z "${KX_KEXEC:-}" ] || SAFE_OVERLAYS=${KX_SAFE_OVERLAYS:-$SAFE_OVERLAYS}
# UIO nodes that are PS AXI performance monitors, not PL logic.
PS_UIO_DEVICES="ffa00000.perf-monitor fd0b0000.perf-monitor fd490000.perf-monitor ffa10000.perf-monitor"

say() { echo "$(date -u +%FT%TZ) $*"; }
staged() { cat "$LOADED"; }
kx() {  # load or unload only; executing is left to systemd's clean kexec
    case "$1" in
    -l|-u) "$KEXEC" "$@" ;;
    *) say "REFUSED kexec $1"; return 99 ;;
    esac
}
fit_sha1() {
    "$DUMPIMAGE" -l "$FIT" | awk -v want="($1)" '/^ Image [0-9]+ \(/{img=$3} /Hash value/ && img==want {print $3; exit}'
}
pl_unsafe() {  # print why the PL is not known safe to carry across kexec; silent if safe
    local o status path sha n=0 fd target dev
    [ -r "$FPGA_STATE" ] || { echo "no FPGA manager state at $FPGA_STATE"; return; }
    for o in "$OVERLAYS"/*/; do
        [ -d "$o" ] || continue
        status=$(cat "$o/status" 2>/dev/null); path=$(cat "$o/path" 2>/dev/null)
        [ "$status" = applied ] || { echo "overlay $(basename "$o") is '$status'"; return; }
        case "$path" in ""|/*|*..*) echo "overlay $(basename "$o") has no plain firmware path"; return ;; esac
        sha=$(sha256sum < "$FIRMWARE/$path" 2>/dev/null | cut -d' ' -f1)
        case " $SAFE_OVERLAYS " in *" $sha "*) n=$((n + 1)) ;; *) echo "overlay $path (sha256 ${sha:-unreadable}) is not a reviewed PL design"; return ;; esac
    done
    [ "$(cat "$FPGA_STATE")" != operating ] || [ "$n" -gt 0 ] || { echo "PL is configured but no reviewed overlay is applied"; return; }
    grep -qE '^(zocl|dmaproxy) ' "$MODULES" 2>/dev/null && { echo "an accelerator or DMA-proxy module is loaded"; return; }
    for fd in "$PROC"/[0-9]*/fd/*; do
        target=$(readlink "$fd" 2>/dev/null) || continue
        case "$target" in
        /dev/uio*)
            dev=$(basename "$(readlink -f "$UIO/${target#/dev/}/device")")
            case " $PS_UIO_DEVICES " in *" $dev "*) ;; *) echo "a process holds $target ($dev) open"; return ;; esac ;;
        /dev/dma_proxy*|/dev/xclmgmt*|/dev/zocl*) echo "a process holds $target open"; return ;;
        esac
    done
}
abort() {  # unload anything we staged; never reboot
    local code=$1; shift
    say "ABORT: $*"
    if [ "$(staged)" != 0 ]; then kx -u; say "unloaded; kexec_loaded=$(staged)"; fi
    exit "$code"
}

[ "$(id -u)" = 0 ] || [ -n "${KX_KEXEC:-}" ] || { echo "run with sudo" >&2; exit 2; }
mode=restart
case "$*" in
"") ;;
check) mode=check ;;
*) echo "usage: sudo bash $0 [check]" >&2; exit 2 ;;
esac

kernel=$BOOT/vmlinuz-$RELEASE
initrd=$BOOT/initrd.img-$RELEASE
say "release=$RELEASE kernel=$kernel initrd=$initrd dtb=$FDT"

[ "$(staged)" = 0 ] || { say "ABORT: a kexec image is already staged; investigate, nothing changed"; exit 10; }
for f in "$kernel" "$initrd" "$FDT" "$CMDLINE"; do
    [ -r "$f" ] || { say "ABORT: missing or unreadable $f; nothing changed"; exit 11; }
done
want_k=$(fit_sha1 kernel-1); want_i=$(fit_sha1 ramdisk-1)
have_k=$(sha1sum < "$kernel" | cut -d' ' -f1); have_i=$(sha1sum < "$initrd" | cut -d' ' -f1)
[ -n "$want_k" ] && [ "$have_k" = "$want_k" ] || { say "ABORT: kernel sha1 $have_k != booted FIT $want_k; nothing changed"; exit 12; }
[ -n "$want_i" ] && [ "$have_i" = "$want_i" ] || { say "ABORT: initrd sha1 $have_i != booted FIT $want_i; nothing changed"; exit 12; }
grep -qw -- "$ROOT_ARG" "$CMDLINE" || { say "ABORT: $ROOT_ARG not in $CMDLINE; nothing changed"; exit 13; }

# A running hardware watchdog survives kexec and, unfed by the new kernel,
# would fire the same SRST path that wedges the board. systemd must not arm one.
for w in "$WATCHDOGS"/watchdog*; do
    [ -e "$w/state" ] || continue
    [ "$(cat "$w/state")" = inactive ] || { say "ABORT: $(basename "$w") is $(cat "$w/state"); nothing changed"; exit 14; }
done
kexec_wd=$("$SYSTEMCTL" show -p KExecWatchdogUSec --value)
[ "$kexec_wd" = 0 ] || { say "ABORT: KExecWatchdogUSec=${kexec_wd:-unreadable}, not 0; a watchdog could be armed; nothing changed"; exit 14; }
why=$(pl_unsafe)
[ -z "$why" ] || { say "ABORT: PL workload not known safe across kexec: $why; nothing changed"; exit 15; }
say "kernel/initrd match booted FIT; root=LABEL=writable present; watchdogs inactive; KExecWatchdogUSec=0; PL reviewed"
[ "$mode" = restart ] || { say "CHECK OK: every restart precondition holds; nothing staged"; exit 0; }

kx -l "$kernel" --initrd="$initrd" --dtb="$FDT" --reuse-cmdline
rc=$?
[ "$rc" -eq 0 ] && [ "$(staged)" = 1 ] || abort 21 "staging failed (rc=$rc, kexec_loaded=$(staged))"
say "staged: kexec_loaded=1; handing off to systemctl kexec (services stop cleanly, no firmware reset)"
"$SYSTEMCTL" --no-wall --no-block kexec
src=$?
[ "$src" -eq 0 ] || abort 22 "systemctl kexec refused (rc=$src)"
say "kexec requested; the board will shut down and start the new kernel"
