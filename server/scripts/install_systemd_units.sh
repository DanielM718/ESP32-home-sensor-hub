#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
ENABLE_SERVICES=0
START_SERVICES=0
UNITS=(
  home-sensor-bridge.service
  home-sensor-dashboard.service
)

usage() {
  cat <<USAGE
Usage: sudo scripts/install_systemd_units.sh [--enable] [--start]

Install systemd unit files for the home sensor Python services.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable)
      ENABLE_SERVICES=1
      shift
      ;;
    --start)
      START_SERVICES=1
      ENABLE_SERVICES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

require_linux
require_root

for unit in "${UNITS[@]}"; do
  source_file="${PROJECT_ROOT}/systemd/${unit}"
  target_file="/etc/systemd/system/${unit}"
  [[ -f "${source_file}" ]] || die "Missing unit file: ${source_file}"

  log "Installing ${unit}"
  install -m 0644 -o root -g root "${source_file}" "${target_file}"
done

log "Reloading systemd manager configuration"
safe_systemctl daemon-reload

if [[ "${ENABLE_SERVICES}" == "1" ]]; then
  for unit in "${UNITS[@]}"; do
    log "Enabling ${unit}"
    safe_systemctl enable "${unit}"
  done
fi

if [[ "${START_SERVICES}" == "1" ]]; then
  for unit in "${UNITS[@]}"; do
    log "Starting ${unit}"
    safe_systemctl restart "${unit}"
  done
fi

log "systemd unit installation complete"
