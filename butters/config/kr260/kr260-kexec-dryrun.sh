#!/bin/bash
# KR260 kexec dry run: stage the running kernel, verify it is staged, unload it,
# verify nothing is staged. It never executes a kernel and never reboots: the
# only kexec operations it can issue are load (-l) and unload (-u).
#   sudo bash kr260-kexec-dryrun.sh
# KX_* variables exist only so tests can point at fakes; sudo's env_reset
# strips them, so a real run always uses the paths below.
set -u
KEXEC=${KX_KEXEC:-/sbin/kexec}
DUMPIMAGE=${KX_DUMPIMAGE:-/usr/bin/dumpimage}
LOADED=${KX_LOADED:-/sys/kernel/kexec_loaded}
BOOT=${KX_BOOT:-/boot}
FIT=${KX_FIT:-/boot/firmware/image.fit}
FDT=${KX_FDT:-/sys/firmware/fdt}
CMDLINE=${KX_CMDLINE:-/proc/cmdline}
RELEASE=${KX_RELEASE:-$(uname -r)}
ROOT_ARG=root=LABEL=writable

say() { echo "$(date -u +%FT%TZ) $*"; }
staged() { cat "$LOADED"; }
kx() {  # the only door to kexec: load or unload, nothing else
    case "$1" in
    -l|-u) "$KEXEC" "$@" ;;
    *) say "REFUSED kexec $1"; return 99 ;;
    esac
}
fit_sha1() {  # sha1 U-Boot verified for FIT image "$1" (kernel-1, ramdisk-1)
    "$DUMPIMAGE" -l "$FIT" | awk -v want="($1)" '/^ Image [0-9]+ \(/{img=$3} /Hash value/ && img==want {print $3; exit}'
}

[ "$(id -u)" = 0 ] || [ -n "${KX_KEXEC:-}" ] || { echo "run with sudo" >&2; exit 2; }
[ $# -eq 0 ] || { echo "usage: sudo bash $0   (no arguments)" >&2; exit 2; }

kernel=$BOOT/vmlinuz-$RELEASE
initrd=$BOOT/initrd.img-$RELEASE
say "release=$RELEASE kernel=$kernel initrd=$initrd dtb=$FDT"

# Preconditions: nothing already staged, the exact booted artifacts, sane cmdline.
[ "$(staged)" = 0 ] || { say "ABORT: a kexec image is already staged (kexec_loaded=$(staged)); investigate, nothing changed"; exit 10; }
for f in "$kernel" "$initrd" "$FDT" "$CMDLINE"; do
    [ -r "$f" ] || { say "ABORT: missing or unreadable $f; nothing changed"; exit 11; }
done
want_k=$(fit_sha1 kernel-1); want_i=$(fit_sha1 ramdisk-1)
have_k=$(sha1sum < "$kernel" | cut -d' ' -f1); have_i=$(sha1sum < "$initrd" | cut -d' ' -f1)
[ -n "$want_k" ] && [ "$have_k" = "$want_k" ] || { say "ABORT: $kernel sha1 $have_k != booted FIT kernel $want_k; nothing changed"; exit 12; }
[ -n "$want_i" ] && [ "$have_i" = "$want_i" ] || { say "ABORT: $initrd sha1 $have_i != booted FIT ramdisk $want_i; nothing changed"; exit 12; }
say "kernel and initrd match the booted FIT images (sha1 $have_k, $have_i)"
grep -qw -- "$ROOT_ARG" "$CMDLINE" || { say "ABORT: $ROOT_ARG not in $CMDLINE; nothing changed"; exit 13; }
say "cmdline:$(cat "$CMDLINE")"

# Stage.
kx -l "$kernel" --initrd="$initrd" --dtb="$FDT" --reuse-cmdline
rc=$?
after_load=$(staged)
say "load rc=$rc kexec_loaded=$after_load"
result=PASS
if [ "$rc" -ne 0 ] || [ "$after_load" != 1 ]; then
    result="FAIL: staging (rc=$rc, kexec_loaded=$after_load)"
fi

# Unload, always, then verify; one retry.
kx -u; urc=$?
if [ "$(staged)" != 0 ]; then
    say "unload rc=$urc left kexec_loaded=$(staged); retrying once"
    kx -u; urc=$?
fi
final=$(staged)
say "unload rc=$urc kexec_loaded=$final"
if [ "$final" != 0 ]; then
    say "CLEANUP NEEDED: a kernel is still staged. Run 'sudo /sbin/kexec -u' and check /sys/kernel/kexec_loaded is 0."
    say "Until then do NOT execute the staged kernel (no kexec execute option, no systemd kexec target). A power cycle also discards it."
    exit 20
fi
say "DRY RUN $result"
[ "$result" = PASS ] || exit 21
