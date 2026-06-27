#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
CHARTJS_VERSION="${CHARTJS_VERSION:-latest}"
CHARTJS_URL="${CHARTJS_URL:-https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.min.js}"
TARGET_DIR="${PROJECT_ROOT}/frontend/static/vendor"
TARGET_FILE="${TARGET_DIR}/chart.umd.min.js"

require_linux
require_root
require_command curl

log "Installing Chart.js browser bundle from ${CHARTJS_URL}"
install -d -m 0755 -o root -g root "${TARGET_DIR}"

tmp_file="$(mktemp)"
trap 'rm -f "${tmp_file}"' EXIT
curl --fail --silent --show-error --location --output "${tmp_file}" "${CHARTJS_URL}"

if ! grep -q "Chart" "${tmp_file}"; then
  die "Downloaded Chart.js asset did not look valid"
fi

install -m 0644 -o root -g root "${tmp_file}" "${TARGET_FILE}"
log "Installed ${TARGET_FILE}"
