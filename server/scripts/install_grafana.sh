#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

GRAFANA_APT_SOURCE="${GRAFANA_APT_SOURCE:-deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main}"

require_linux
require_root
require_command apt-get

log "Installing Grafana installer prerequisites"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gpg

require_command curl
require_command gpg

log "Adding Grafana apt repository"
install -d -m 0755 -o root -g root /etc/apt/keyrings
curl --fail --silent --show-error --location https://apt.grafana.com/gpg.key \
  | gpg --dearmor --yes --output /etc/apt/keyrings/grafana.gpg
chmod 0644 /etc/apt/keyrings/grafana.gpg
printf '%s\n' "${GRAFANA_APT_SOURCE}" > /etc/apt/sources.list.d/grafana.list
chmod 0644 /etc/apt/sources.list.d/grafana.list

log "Installing Grafana OSS"
apt-get update
apt-get install -y grafana

if command -v systemctl >/dev/null 2>&1; then
  log "Enabling Grafana service"
  systemctl enable grafana-server.service
else
  warn "systemctl not available; enable and start Grafana manually"
fi

log "Grafana installation step complete"
log "Next: run scripts/provision_grafana.sh"
