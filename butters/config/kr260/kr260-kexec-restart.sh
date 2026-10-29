#!/bin/bash
# KR260 restart through kexec, avoiding the PMUFW warm system reset that wedges
# this board. Same preconditions and the same load invocation as
# kr260-kexec-dryrun.sh; then a clean `systemctl kexec`. It never asks for a
# reboot: if anything is not exactly right it unloads and exits.
#   sudo bash kr260-kexec-restart.sh
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
RELEASE=${KX_RELEASE:-$(uname -r)}
ROOT_ARG=root=LABEL=writable

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
abort() {  # unload anything we staged; never reboot
    local code=$1; shift
    say "ABORT: $*"
    if [ "$(staged)" != 0 ]; then kx -u; say "unloaded; kexec_loaded=$(staged)"; fi
    exit "$code"
}

[ "$(id -u)" = 0 ] || [ -n "${KX_KEXEC:-}" ] || { echo "run with sudo" >&2; exit 2; }
[ $# -eq 0 ] || { echo "usage: sudo bash $0   (no arguments)" >&2; exit 2; }

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
say "kernel/initrd match booted FIT; root=LABEL=writable present; watchdogs inactive; KExecWatchdogUSec=0"

kx -l "$kernel" --initrd="$initrd" --dtb="$FDT" --reuse-cmdline
rc=$?
[ "$rc" -eq 0 ] && [ "$(staged)" = 1 ] || abort 21 "staging failed (rc=$rc, kexec_loaded=$(staged))"
say "staged: kexec_loaded=1; handing off to systemctl kexec (services stop cleanly, no firmware reset)"
"$SYSTEMCTL" --no-wall --no-block kexec
src=$?
[ "$src" -eq 0 ] || abort 22 "systemctl kexec refused (rc=$src)"
say "kexec requested; the board will shut down and start the new kernel"
