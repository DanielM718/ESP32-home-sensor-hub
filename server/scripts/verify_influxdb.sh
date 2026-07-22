#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
ENV_FILE="${ENV_FILE:-${PROJECT_ROOT}/backend/.env}"
INFLUXDB_URL="${INFLUXDB_URL:-http://127.0.0.1:8086}"
INFLUXDB_ORG="${INFLUXDB_ORG:-home}"
INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-environment}"
INFLUXDB_LIVE_BUCKET="${INFLUXDB_LIVE_BUCKET:-environment_live}"
INFLUXDB_LIVE_RETENTION="${INFLUXDB_LIVE_RETENTION:-72h}"
INFLUXDB_TOKEN="${INFLUXDB_TOKEN:-}"
INFLUXDB_READ_TOKEN="${INFLUXDB_READ_TOKEN:-}"
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

load_backend_env() {
  [[ -f "${ENV_FILE}" ]] || return 0
  while IFS='=' read -r key value; do
    case "${key}" in
      INFLUXDB_URL) INFLUXDB_URL="${value}" ;;
      INFLUXDB_ORG) INFLUXDB_ORG="${value}" ;;
      INFLUXDB_BUCKET) INFLUXDB_BUCKET="${value}" ;;
      INFLUXDB_LIVE_BUCKET) INFLUXDB_LIVE_BUCKET="${value}" ;;
      INFLUXDB_LIVE_RETENTION) INFLUXDB_LIVE_RETENTION="${value}" ;;
      INFLUXDB_TOKEN) INFLUXDB_TOKEN="${value}" ;;
      INFLUXDB_READ_TOKEN) INFLUXDB_READ_TOKEN="${value}" ;;
    esac
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${ENV_FILE}" || true)
}

influx_ping() {
  influx ping --host "${INFLUXDB_URL}" >/dev/null 2>&1
}

bucket_readable() {
  local bucket_name="$1"
  local token="${INFLUXDB_READ_TOKEN:-${INFLUXDB_TOKEN}}"
  [[ -n "${token}" ]] || return 1
  influx bucket list \
    --host "${INFLUXDB_URL}" \
    --org "${INFLUXDB_ORG}" \
    --token "${token}" \
    --name "${bucket_name}" >/dev/null 2>&1
}

live_bucket_retention_matches() {
  local token="${INFLUXDB_READ_TOKEN:-${INFLUXDB_TOKEN}}"
  [[ -n "${token}" ]] || return 1
  local actual_seconds
  actual_seconds="$(
    influx bucket list \
      --host "${INFLUXDB_URL}" \
      --org "${INFLUXDB_ORG}" \
      --token "${token}" \
      --name "${INFLUXDB_LIVE_BUCKET}" \
      --json \
    | python3 -c '
import json
import sys

data = json.load(sys.stdin)
if isinstance(data, dict):
    data = data.get("buckets", [])
bucket = data[0] if data else {}
rules = bucket.get("retentionRules") or []
print(int(rules[0].get("everySeconds", 0)) if rules else 0)
'
  )"
  local expected_seconds
  expected_seconds="$(python3 - "${INFLUXDB_LIVE_RETENTION}" <<'PY'
import re
import sys

value = sys.argv[1]
units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
position = 0
total = 0
for match in re.finditer(r"(\d+)([wdhms])", value):
    if match.start() != position:
        raise SystemExit(1)
    total += int(match.group(1)) * units[match.group(2)]
    position = match.end()
if position != len(value) or total <= 0:
    raise SystemExit(1)
print(total)
PY
  )"
  [[ "${actual_seconds}" -gt 0 && "${actual_seconds}" == "${expected_seconds}" ]]
}

influxdb_service_known() {
  systemctl cat influxdb.service >/dev/null 2>&1
}

require_linux
load_backend_env

check "influx CLI exists" command_exists influx
check "influxd command exists" command_exists influxd
check "InfluxDB responds to ping" influx_ping
check "long-term bucket is accessible with backend token" bucket_readable "${INFLUXDB_BUCKET}"
check "live bucket is accessible with backend token" bucket_readable "${INFLUXDB_LIVE_BUCKET}"
check "live bucket has configured finite retention" live_bucket_retention_matches

if command -v systemctl >/dev/null 2>&1; then
  check "influxdb service is known to systemd" influxdb_service_known
fi

if [[ "${FAILED}" == "0" ]]; then
  log "InfluxDB verification passed"
else
  die "InfluxDB verification failed"
fi
