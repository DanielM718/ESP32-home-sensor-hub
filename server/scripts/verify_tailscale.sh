#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

FAILED=0

check() {
  local description="$1"
  shift

  if "$@"; then
    printf '[ok] %s\n' "${description}"
  else
    printf '[fail] %s\n' "${description}"
    FAILED=1
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

tailscaled_service_known() {
  systemctl cat tailscaled.service >/dev/null 2>&1
}

tailscale_status_ok() {
  tailscale status >/dev/null 2>&1
}

tailscale_ipv4_present() {
  [[ -n "$(tailscale ip -4 2>/dev/null)" ]]
}

require_linux

check "tailscale command exists" command_exists tailscale

if command -v systemctl >/dev/null 2>&1; then
  check "tailscaled service is known to systemd" tailscaled_service_known
fi

check "tailscale status succeeds" tailscale_status_ok
check "tailscale IPv4 address is assigned" tailscale_ipv4_present

if [[ "${FAILED}" == "0" ]]; then
  log "Tailscale verification passed"
  tailscale ip -4
else
  die "Tailscale verification failed"
fi
