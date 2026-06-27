#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

SERVICE_USER="${SERVICE_USER:-home-sensor}"
SERVICE_HOME="${SERVICE_HOME:-/var/lib/home-sensor}"
PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"

require_linux
require_root
require_command useradd

if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  log "Service user ${SERVICE_USER} already exists"
else
  log "Creating system user ${SERVICE_USER}"
  useradd \
    --system \
    --home-dir "${SERVICE_HOME}" \
    --create-home \
    --shell /usr/sbin/nologin \
    --user-group \
    "${SERVICE_USER}"
fi

install -d -m 0755 -o root -g root "$(dirname "${PROJECT_ROOT}")"
install -d -m 0755 -o root -g root "${PROJECT_ROOT}"
install -d -m 0750 -o root -g "${SERVICE_USER}" "${PROJECT_ROOT}/backend"
install -d -m 0755 -o root -g root "${PROJECT_ROOT}/systemd"
install -d -m 0755 -o root -g root "${PROJECT_ROOT}/scripts"

log "Service user and deployment directories are ready"
