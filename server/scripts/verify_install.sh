#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

SERVICE_USER="${SERVICE_USER:-home-sensor}"
PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
VENV_DIR="${PROJECT_ROOT}/backend/.venv"
ENV_FILE="${PROJECT_ROOT}/backend/.env"
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

dir_exists() {
  [[ -d "$1" ]]
}

user_exists() {
  id -u "$1" >/dev/null 2>&1
}

unit_installed() {
  [[ -f "/etc/systemd/system/$1" ]]
}

env_permissions_restricted() {
  [[ -f "${ENV_FILE}" ]] || return 1
  local mode owner group
  mode="$(stat -c '%a' "${ENV_FILE}")"
  owner="$(stat -c '%U' "${ENV_FILE}")"
  group="$(stat -c '%G' "${ENV_FILE}")"
  [[ "${mode}" == "640" && "${owner}" == "root" && "${group}" == "${SERVICE_USER}" ]]
}

require_linux

check "service user exists (${SERVICE_USER})" user_exists "${SERVICE_USER}"
check "project root exists (${PROJECT_ROOT})" dir_exists "${PROJECT_ROOT}"
check "backend env file exists" file_exists "${ENV_FILE}"
check "backend env permissions are root:${SERVICE_USER} 0640" env_permissions_restricted
check "virtual environment exists" dir_exists "${VENV_DIR}"
check "virtual environment python exists" file_exists "${VENV_DIR}/bin/python"
check "requirements file exists" file_exists "${PROJECT_ROOT}/requirements.txt"
check "bridge unit source exists" file_exists "${PROJECT_ROOT}/systemd/home-sensor-bridge.service"
check "dashboard unit source exists" file_exists "${PROJECT_ROOT}/systemd/home-sensor-dashboard.service"
check "bridge unit is installed" unit_installed home-sensor-bridge.service
check "dashboard unit is installed" unit_installed home-sensor-dashboard.service

if command -v systemctl >/dev/null 2>&1; then
  systemctl --no-pager status home-sensor-bridge.service >/dev/null 2>&1 || true
  systemctl --no-pager status home-sensor-dashboard.service >/dev/null 2>&1 || true
fi

if [[ "${FAILED}" == "0" ]]; then
  log "Base installation verification passed"
else
  die "Base installation verification failed"
fi
