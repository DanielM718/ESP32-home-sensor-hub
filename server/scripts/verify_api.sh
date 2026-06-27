#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8080}"
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

http_ok() {
  local path="$1"
  local status
  status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' "${API_BASE_URL}${path}")"
  [[ "${status}" == "200" ]]
}

require_linux
require_command curl

check "dashboard API health endpoint responds" http_ok /api/health
check "latest readings endpoint responds" http_ok /api/latest
check "historical readings endpoint responds" http_ok '/api/readings?range=24h'
check "nodes endpoint responds" http_ok /api/nodes

if [[ "${FAILED}" == "0" ]]; then
  log "Dashboard API verification passed"
else
  die "Dashboard API verification failed"
fi
