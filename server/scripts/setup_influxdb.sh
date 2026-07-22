#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
SERVICE_USER="${SERVICE_USER:-home-sensor}"
ENV_FILE="${ENV_FILE:-${PROJECT_ROOT}/backend/.env}"
INFLUXDB_URL="${INFLUXDB_URL:-http://127.0.0.1:8086}"
INFLUXDB_ORG="${INFLUXDB_ORG:-home}"
INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-environment}"
INFLUXDB_RETENTION="${INFLUXDB_RETENTION:-0}"
INFLUXDB_LIVE_BUCKET="${INFLUXDB_LIVE_BUCKET:-environment_live}"
INFLUXDB_LIVE_RETENTION="${INFLUXDB_LIVE_RETENTION:-72h}"
INFLUXDB_ADMIN_USERNAME="${INFLUXDB_ADMIN_USERNAME:-admin}"
INFLUXDB_ADMIN_PASSWORD="${INFLUXDB_ADMIN_PASSWORD:-}"
INFLUXDB_ADMIN_TOKEN="${INFLUXDB_ADMIN_TOKEN:-}"

usage() {
  cat <<USAGE
Usage: sudo scripts/setup_influxdb.sh [options]

Initialize InfluxDB OSS v2 and create scoped application tokens.

Options:
  --url URL                InfluxDB URL. Default: http://127.0.0.1:8086
  --org ORG                Organization. Default: home
  --bucket BUCKET          Bucket. Default: environment
  --retention DURATION     Retention duration. Default: 0 (infinite)
  --live-bucket BUCKET     High-resolution SEN66 bucket. Default: environment_live
  --live-retention PERIOD  High-resolution retention. Default: 72h
  --admin-user USER        Initial admin username. Default: admin
  --env-file PATH          Backend environment file to update
  -h, --help               Show this help

Set INFLUXDB_ADMIN_PASSWORD and INFLUXDB_ADMIN_TOKEN in the shell to avoid
interactive prompts. For an initialized instance, the token must have
organization all-access or operator permissions. It is used during setup only
and is not stored in backend/.env.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      INFLUXDB_URL="${2:-}"
      [[ -n "${INFLUXDB_URL}" ]] || die "--url requires a value"
      shift 2
      ;;
    --org)
      INFLUXDB_ORG="${2:-}"
      [[ -n "${INFLUXDB_ORG}" ]] || die "--org requires a value"
      shift 2
      ;;
    --bucket)
      INFLUXDB_BUCKET="${2:-}"
      [[ -n "${INFLUXDB_BUCKET}" ]] || die "--bucket requires a value"
      shift 2
      ;;
    --retention)
      INFLUXDB_RETENTION="${2:-}"
      [[ -n "${INFLUXDB_RETENTION}" ]] || die "--retention requires a value"
      shift 2
      ;;
    --live-bucket)
      INFLUXDB_LIVE_BUCKET="${2:-}"
      [[ -n "${INFLUXDB_LIVE_BUCKET}" ]] || die "--live-bucket requires a value"
      shift 2
      ;;
    --live-retention)
      INFLUXDB_LIVE_RETENTION="${2:-}"
      [[ -n "${INFLUXDB_LIVE_RETENTION}" ]] || die "--live-retention requires a value"
      shift 2
      ;;
    --admin-user)
      INFLUXDB_ADMIN_USERNAME="${2:-}"
      [[ -n "${INFLUXDB_ADMIN_USERNAME}" ]] || die "--admin-user requires a value"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      [[ -n "${ENV_FILE}" ]] || die "--env-file requires a path"
      shift 2
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
require_command influx
require_command python3

prompt_secret() {
  local prompt="$1"
  local value
  read -r -s -p "${prompt}" value
  printf '\n' >&2
  printf '%s' "${value}"
}

generate_token() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

ensure_admin_credentials() {
  if influxdb_is_setup; then
    if [[ -z "${INFLUXDB_ADMIN_TOKEN}" ]]; then
      die "InfluxDB is already initialized; set INFLUXDB_ADMIN_TOKEN to an existing organization all-access or operator token"
    fi
    if ! admin_token_works; then
      die "InfluxDB is already initialized, but INFLUXDB_ADMIN_TOKEN cannot list buckets in organization ${INFLUXDB_ORG}; supply an organization all-access or operator token"
    fi
    return
  fi

  if [[ -z "${INFLUXDB_ADMIN_PASSWORD}" ]]; then
    INFLUXDB_ADMIN_PASSWORD="$(prompt_secret "InfluxDB admin password: ")"
  fi

  [[ -n "${INFLUXDB_ADMIN_PASSWORD}" ]] || die "InfluxDB admin password cannot be empty"

  if [[ -z "${INFLUXDB_ADMIN_TOKEN}" ]]; then
    INFLUXDB_ADMIN_TOKEN="$(generate_token)"
    warn "Generated an admin token for setup. Store it in a password manager now."
    printf '%s\n' "${INFLUXDB_ADMIN_TOKEN}"
  fi
}

influx_cli_ready() {
  influx ping --host "${INFLUXDB_URL}" >/dev/null 2>&1
}

influxdb_is_setup() {
  python3 - "${INFLUXDB_URL}" <<'PY'
import json
import sys
from urllib.request import urlopen

url = sys.argv[1].rstrip("/") + "/api/v2/setup"
with urlopen(url, timeout=5) as response:
    payload = json.load(response)
raise SystemExit(0 if payload.get("allowed") is False else 1)
PY
}

admin_token_works() {
  influx bucket list \
    --host "${INFLUXDB_URL}" \
    --org "${INFLUXDB_ORG}" \
    --token "${INFLUXDB_ADMIN_TOKEN}" >/dev/null 2>&1
}

setup_influxdb_if_needed() {
  if admin_token_works; then
    log "InfluxDB already accepts the provided admin token"
    return
  fi

  log "Running initial InfluxDB setup"
  influx setup \
    --host "${INFLUXDB_URL}" \
    --org "${INFLUXDB_ORG}" \
    --bucket "${INFLUXDB_BUCKET}" \
    --retention "${INFLUXDB_RETENTION}" \
    --username "${INFLUXDB_ADMIN_USERNAME}" \
    --password "${INFLUXDB_ADMIN_PASSWORD}" \
    --token "${INFLUXDB_ADMIN_TOKEN}" \
    --force
}

bucket_id() {
  local bucket_name="$1"
  influx bucket list \
    --host "${INFLUXDB_URL}" \
    --org "${INFLUXDB_ORG}" \
    --token "${INFLUXDB_ADMIN_TOKEN}" \
    --name "${bucket_name}" \
    --json \
    | python3 -c '
import json
import sys

data = json.load(sys.stdin)
if isinstance(data, dict):
    data = data.get("buckets", [])
for bucket in data:
    if bucket.get("name"):
        print(bucket["id"])
        raise SystemExit(0)
raise SystemExit("bucket not found")
'
}

ensure_bucket() {
  local bucket_name="$1"
  local retention="$2"
  if bucket_id "${bucket_name}" >/dev/null 2>&1; then
    return
  fi

  log "Creating bucket ${bucket_name} with retention ${retention}"
  influx bucket create \
    --host "${INFLUXDB_URL}" \
    --org "${INFLUXDB_ORG}" \
    --token "${INFLUXDB_ADMIN_TOKEN}" \
    --name "${bucket_name}" \
    --retention "${retention}" >/dev/null
}

create_scoped_token() {
  local description="$1"
  shift

  influx auth create \
    --host "${INFLUXDB_URL}" \
    --org "${INFLUXDB_ORG}" \
    --token "${INFLUXDB_ADMIN_TOKEN}" \
    --description "${description}" \
    --json \
    "$@" \
    | python3 -c '
import json
import sys

data = json.load(sys.stdin)
if isinstance(data, list):
    data = data[0] if data else {}
token = data.get("token") or data.get("Token")
if not token:
    raise SystemExit("token not found in influx auth create output")
print(token)
'
}

set_env_value() {
  local key="$1"
  local value="$2"

  install -d -m 0750 -o root -g "${SERVICE_USER}" "$(dirname "${ENV_FILE}")"
  if [[ ! -f "${ENV_FILE}" ]]; then
    install -m 0640 -o root -g "${SERVICE_USER}" "${PROJECT_ROOT}/backend/.env.example" "${ENV_FILE}"
  fi

  python3 - "${ENV_FILE}" "${key}" "${value}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

lines = path.read_text().splitlines() if path.exists() else []
prefix = f"{key}="
updated = False
next_lines = []

for line in lines:
    if line.startswith(prefix):
        next_lines.append(f"{key}={value}")
        updated = True
    else:
        next_lines.append(line)

if not updated:
    next_lines.append(f"{key}={value}")

path.write_text("\n".join(next_lines) + "\n")
PY

  chown root:"${SERVICE_USER}" "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
}

log "Waiting for InfluxDB at ${INFLUXDB_URL}"
for _ in $(seq 1 30); do
  if influx_cli_ready; then
    break
  fi
  sleep 2
done
influx_cli_ready || die "InfluxDB did not respond to influx ping"

ensure_admin_credentials
setup_influxdb_if_needed

admin_token_works || die "InfluxDB admin token could not list buckets after setup"
ensure_bucket "${INFLUXDB_BUCKET}" "${INFLUXDB_RETENTION}"
ensure_bucket "${INFLUXDB_LIVE_BUCKET}" "${INFLUXDB_LIVE_RETENTION}"
BUCKET_ID="$(bucket_id "${INFLUXDB_BUCKET}")"
LIVE_BUCKET_ID="$(bucket_id "${INFLUXDB_LIVE_BUCKET}")"
[[ -n "${BUCKET_ID}" ]] || die "Unable to determine bucket ID for ${INFLUXDB_BUCKET}"
[[ -n "${LIVE_BUCKET_ID}" ]] || die "Unable to determine bucket ID for ${INFLUXDB_LIVE_BUCKET}"
log "Using bucket ${INFLUXDB_BUCKET} (${BUCKET_ID})"
log "Using live bucket ${INFLUXDB_LIVE_BUCKET} (${LIVE_BUCKET_ID})"

log "Creating scoped application tokens"
WRITE_TOKEN="$(create_scoped_token "home-sensor bridge tiered write/recovery" \
  --write-bucket "${BUCKET_ID}" \
  --write-bucket "${LIVE_BUCKET_ID}" \
  --read-bucket "${LIVE_BUCKET_ID}")"
READ_TOKEN="$(create_scoped_token "home-sensor dashboard tiered read" \
  --read-bucket "${BUCKET_ID}" \
  --read-bucket "${LIVE_BUCKET_ID}")"

set_env_value INFLUXDB_URL "${INFLUXDB_URL}"
set_env_value INFLUXDB_ORG "${INFLUXDB_ORG}"
set_env_value INFLUXDB_BUCKET "${INFLUXDB_BUCKET}"
set_env_value INFLUXDB_LIVE_BUCKET "${INFLUXDB_LIVE_BUCKET}"
set_env_value INFLUXDB_LIVE_RETENTION "${INFLUXDB_LIVE_RETENTION}"
set_env_value INFLUXDB_TOKEN "${WRITE_TOKEN}"
set_env_value INFLUXDB_WRITE_TOKEN "${WRITE_TOKEN}"
set_env_value INFLUXDB_READ_TOKEN "${READ_TOKEN}"

log "InfluxDB setup complete"
log "backend/.env now contains scoped application tokens"
