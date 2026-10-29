#!/bin/bash
# Temporarily disable (or restore) the KR260's PSCI power-down idle state
# "cpu-sleep-0" while keeping WFI. Runtime sysfs only: no boot configuration
# is touched and everything reverts on reboot.
#   bash kr260-deep-idle.sh check            validate only, no root, no writes
#   sudo bash kr260-deep-idle.sh status|apply|rollback
# KR260_IDLE_CPU_ROOT / KR260_IDLE_ORIG exist only so tests can point at a fake
# tree; sudo's env_reset strips them, so a real run always uses /sys and /run.
set -u
TARGET=cpu-sleep-0
CPU_ROOT=${KR260_IDLE_CPU_ROOT:-/sys/devices/system/cpu}
ORIG=${KR260_IDLE_ORIG:-/run/kr260-deep-idle.orig}  # tmpfs: gone on reboot, like the setting

states() {  # every CPU's "<cpuN>/cpuidle/stateK/disable" whose state is named $TARGET
    for c in "$CPU_ROOT"/cpu[0-9]*; do
        [ -d "$c/cpuidle" ] || continue
        for s in "$c"/cpuidle/state*; do
            [ "$(cat "$s/name" 2>/dev/null)" = "$TARGET" ] && echo "$s/disable"
        done
    done
    return 0
}

status() {
    echo "online=$(cat "$CPU_ROOT/online")"
    for c in "$CPU_ROOT"/cpu[0-9]*; do
        for s in "$c"/cpuidle/state*; do
            [ -d "$s" ] && echo "$(basename "$c") $(basename "$s") name=$(cat "$s/name") disable=$(cat "$s/disable")"
        done
    done
    [ -f "$ORIG" ] && { echo "saved originals ($ORIG):"; cat "$ORIG"; }
    return 0
}

validate() {  # fills $paths; fails closed without writing anything
    mapfile -t paths < <(states)
    [ "${#paths[@]}" -eq 4 ] || { echo "expected 4 $TARGET states, found ${#paths[@]}; nothing changed" >&2; return 1; }
    local p state idle
    for p in "${paths[@]}"; do
        state=${p%/disable}      # .../cpuN/cpuidle/stateK
        idle=${state%/*}         # .../cpuN/cpuidle
        [ "$(cat "$state/name")" = "$TARGET" ] || { echo "mapping changed at $state; nothing changed" >&2; return 1; }
        [ "$(cat "$idle/state0/name" 2>/dev/null)" = "WFI" ] || { echo "state0 is not WFI in $idle; nothing changed" >&2; return 1; }
        [ "$state" != "$idle/state0" ] || { echo "$TARGET is state0 in $idle; refusing to disable WFI" >&2; return 1; }
        case "$(cat "$p")" in 0|1) ;; *) echo "unexpected value in $p; nothing changed" >&2; return 1 ;; esac
    done
}

restore() {
    local rc=0 path value
    while read -r path value; do
        { echo "$value" > "$path" && [ "$(cat "$path")" = "$value" ]; } || { echo "RESTORE FAILED: $path" >&2; rc=1; }
    done < "$ORIG"
    return $rc
}

mode=${1:-status}
case "$mode" in
check|status) ;;
apply|rollback) [ "$(id -u)" = 0 ] || [ -n "${KR260_IDLE_CPU_ROOT:-}" ] || { echo "run with sudo" >&2; exit 2; } ;;
*) echo "usage: bash $0 check | sudo bash $0 status|apply|rollback" >&2; exit 2 ;;
esac

case "$mode" in
check)
    validate || exit 1
    echo "CHECK OK: would set disable=1 on:"; for p in "${paths[@]}"; do echo "  $p (now $(cat "$p"))"; done ;;
status) status ;;
apply)
    validate || exit 1
    online=$(cat "$CPU_ROOT/online")
    [ -f "$ORIG" ] || for p in "${paths[@]}"; do echo "$p $(cat "$p")"; done > "$ORIG"
    for p in "${paths[@]}"; do
        if ! { echo 1 > "$p" && [ "$(cat "$p")" = 1 ]; }; then
            echo "write/verify failed at $p; restoring originals" >&2; restore; exit 1
        fi
    done
    [ "$(cat "$CPU_ROOT/online")" = "$online" ] || { echo "CPU set changed; restoring" >&2; restore; exit 1; }
    echo "APPLIED: $TARGET disabled on ${#paths[@]} CPUs; WFI untouched"; status ;;
rollback)
    [ -f "$ORIG" ] || { echo "no saved originals; restoring disable=0"; states | sed 's/$/ 0/' > "$ORIG"; }
    if restore; then echo "ROLLED BACK"; rm -f "$ORIG"; status; else status; exit 1; fi ;;
esac
