#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
DATASOURCE_FILE="/etc/grafana/provisioning/datasources/home-sensor-influxdb.yml"
DASHBOARD_PROVIDER_FILE="/etc/grafana/provisioning/dashboards/home-sensor-dashboards.yml"
DASHBOARD_FILE="/var/lib/grafana/dashboards/home-sensor/home-sensor-environment.json"
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

file_exists() {
  [[ -f "$1" ]]
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

service_known() {
  systemctl cat grafana-server.service >/dev/null 2>&1
}

datasource_rendered() {
  [[ -f "${DATASOURCE_FILE}" ]] || return 1
  ! grep -q '__INFLUXDB_' "${DATASOURCE_FILE}"
}

grafana_http_ready() {
  command -v curl >/dev/null 2>&1 || return 1
  local status
  status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' "${GRAFANA_URL}/api/health")"
  [[ "${status}" == "200" ]]
}

require_linux

check "grafana command exists" command_exists grafana
check "datasource provisioning file exists" file_exists "${DATASOURCE_FILE}"
check "datasource provisioning file is rendered" datasource_rendered
check "dashboard provider file exists" file_exists "${DASHBOARD_PROVIDER_FILE}"
check "home sensor dashboard JSON exists" file_exists "${DASHBOARD_FILE}"

if command -v systemctl >/dev/null 2>&1; then
  check "grafana-server service is known to systemd" service_known
fi

if command -v curl >/dev/null 2>&1; then
  check "Grafana HTTP health endpoint responds" grafana_http_ready
else
  warn "curl is not available; skipping Grafana HTTP health check"
fi

if [[ "${FAILED}" == "0" ]]; then
  log "Grafana verification passed"
else
  die "Grafana verification failed"
fi
