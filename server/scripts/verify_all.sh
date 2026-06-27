#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

require_linux
require_root

checks=(
  verify_install.sh
  verify_mqtt.sh
  verify_influxdb.sh
  verify_api.sh
  verify_grafana.sh
  verify_tailscale.sh
)

for check_script in "${checks[@]}"; do
  log "Running ${check_script}"
  "${SCRIPT_DIR}/${check_script}"
done

log "All deployment verification checks passed"
