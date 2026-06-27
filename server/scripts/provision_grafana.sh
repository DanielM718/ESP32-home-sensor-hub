#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
ENV_FILE="${ENV_FILE:-${PROJECT_ROOT}/backend/.env}"
GRAFANA_DASHBOARD_SOURCE="${PROJECT_ROOT}/config/grafana/dashboards"
GRAFANA_DATASOURCE_TEMPLATE="${PROJECT_ROOT}/config/grafana/provisioning/datasources/home-sensor-influxdb.yml.tmpl"
GRAFANA_DASHBOARD_PROVIDER_SOURCE="${PROJECT_ROOT}/config/grafana/provisioning/dashboards/home-sensor-dashboards.yml"
GRAFANA_DATASOURCE_TARGET="/etc/grafana/provisioning/datasources/home-sensor-influxdb.yml"
GRAFANA_DASHBOARD_PROVIDER_TARGET="/etc/grafana/provisioning/dashboards/home-sensor-dashboards.yml"
GRAFANA_DASHBOARD_TARGET_DIR="/var/lib/grafana/dashboards/home-sensor"

require_linux
require_root
require_command python3

load_env_value() {
  local key="$1"
  [[ -f "${ENV_FILE}" ]] || return 1
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d '=' -f 2-
}

INFLUXDB_URL="${INFLUXDB_URL:-$(load_env_value INFLUXDB_URL || true)}"
INFLUXDB_ORG="${INFLUXDB_ORG:-$(load_env_value INFLUXDB_ORG || true)}"
INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-$(load_env_value INFLUXDB_BUCKET || true)}"
INFLUXDB_READ_TOKEN="${INFLUXDB_READ_TOKEN:-$(load_env_value INFLUXDB_READ_TOKEN || true)}"
INFLUXDB_READ_TOKEN="${INFLUXDB_READ_TOKEN:-$(load_env_value INFLUXDB_TOKEN || true)}"
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-$(load_env_value GRAFANA_ADMIN_PASSWORD || true)}"

[[ -n "${INFLUXDB_URL}" ]] || die "Missing INFLUXDB_URL"
[[ -n "${INFLUXDB_ORG}" ]] || die "Missing INFLUXDB_ORG"
[[ -n "${INFLUXDB_BUCKET}" ]] || die "Missing INFLUXDB_BUCKET"
[[ -n "${INFLUXDB_READ_TOKEN}" ]] || die "Missing INFLUXDB_READ_TOKEN or INFLUXDB_TOKEN"
[[ -f "${GRAFANA_DATASOURCE_TEMPLATE}" ]] || die "Missing datasource template: ${GRAFANA_DATASOURCE_TEMPLATE}"
[[ -f "${GRAFANA_DASHBOARD_PROVIDER_SOURCE}" ]] || die "Missing dashboard provider: ${GRAFANA_DASHBOARD_PROVIDER_SOURCE}"
[[ -d "${GRAFANA_DASHBOARD_SOURCE}" ]] || die "Missing dashboard source directory: ${GRAFANA_DASHBOARD_SOURCE}"

log "Installing Grafana provisioning directories"
install -d -m 0755 -o root -g root /etc/grafana/provisioning/datasources
install -d -m 0755 -o root -g root /etc/grafana/provisioning/dashboards
install -d -m 0755 -o grafana -g grafana "${GRAFANA_DASHBOARD_TARGET_DIR}"

log "Rendering InfluxDB datasource provisioning file"
export INFLUXDB_URL INFLUXDB_ORG INFLUXDB_BUCKET INFLUXDB_READ_TOKEN
python3 - "${GRAFANA_DATASOURCE_TEMPLATE}" "${GRAFANA_DATASOURCE_TARGET}" <<'PY'
from pathlib import Path
import os
import sys

source = Path(sys.argv[1])
target = Path(sys.argv[2])
content = source.read_text()
for key in ("INFLUXDB_URL", "INFLUXDB_ORG", "INFLUXDB_BUCKET", "INFLUXDB_READ_TOKEN"):
    value = os.environ[key]
    content = content.replace(f"__{key}__", value)
target.write_text(content)
PY

if getent group grafana >/dev/null 2>&1; then
  chown root:grafana "${GRAFANA_DATASOURCE_TARGET}"
else
  chown root:root "${GRAFANA_DATASOURCE_TARGET}"
fi
chmod 0640 "${GRAFANA_DATASOURCE_TARGET}"

install -m 0644 -o root -g root "${GRAFANA_DASHBOARD_PROVIDER_SOURCE}" "${GRAFANA_DASHBOARD_PROVIDER_TARGET}"
find "${GRAFANA_DASHBOARD_TARGET_DIR}" -type f -name '*.json' -delete
install -m 0644 -o grafana -g grafana "${GRAFANA_DASHBOARD_SOURCE}"/*.json "${GRAFANA_DASHBOARD_TARGET_DIR}/"

if [[ -n "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
  log "Resetting Grafana admin password from GRAFANA_ADMIN_PASSWORD"
  grafana cli --homepath /usr/share/grafana admin reset-admin-password "${GRAFANA_ADMIN_PASSWORD}"
else
  warn "GRAFANA_ADMIN_PASSWORD is not set; set a non-default admin password manually"
  warn "Example: sudo grafana cli --homepath /usr/share/grafana admin reset-admin-password '<new-password>'"
fi

if command -v systemctl >/dev/null 2>&1; then
  log "Restarting Grafana"
  systemctl restart grafana-server.service
else
  warn "systemctl not available; restart Grafana manually"
fi

log "Grafana provisioning complete"
