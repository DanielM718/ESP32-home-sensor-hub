#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

SERVICE_USER="${SERVICE_USER:-home-sensor}"
PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
INSTALL_ENABLE_SERVICES="${INSTALL_ENABLE_SERVICES:-1}"
INSTALL_START_SERVICES="${INSTALL_START_SERVICES:-0}"
INSTALL_FRONTEND_ASSETS="${INSTALL_FRONTEND_ASSETS:-1}"

usage() {
  cat <<USAGE
Usage: sudo ./install.sh [options]

Prepare the Raspberry Pi native deployment for the home sensor backend.

Options:
  --project-root PATH      Deployment root. Default: /opt/home-sensor/server
  --service-user USER      Linux service user. Default: home-sensor
  --no-enable-services     Install unit files but do not enable them
  --no-frontend-assets     Skip Chart.js browser bundle download
  --start-services         Start services after installing unit files
  -h, --help               Show this help

This script is intended for Raspberry Pi OS Lite 64-bit. Do not run it on macOS.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root)
      PROJECT_ROOT="${2:-}"
      [[ -n "${PROJECT_ROOT}" ]] || die "--project-root requires a path"
      shift 2
      ;;
    --service-user)
      SERVICE_USER="${2:-}"
      [[ -n "${SERVICE_USER}" ]] || die "--service-user requires a username"
      shift 2
      ;;
    --no-enable-services)
      INSTALL_ENABLE_SERVICES=0
      shift
      ;;
    --no-frontend-assets)
      INSTALL_FRONTEND_ASSETS=0
      shift
      ;;
    --start-services)
      INSTALL_START_SERVICES=1
      shift
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

export SERVICE_USER PROJECT_ROOT

require_linux
require_root

log "Preparing native Raspberry Pi deployment"
log "Source directory: ${SCRIPT_DIR}"
log "Project root: ${PROJECT_ROOT}"
log "Service user: ${SERVICE_USER}"

"${SCRIPT_DIR}/scripts/install_base_packages.sh"
"${SCRIPT_DIR}/scripts/create_service_user.sh"

if [[ "${SCRIPT_DIR}" != "${PROJECT_ROOT}" ]]; then
  require_command rsync
  log "Copying project files into ${PROJECT_ROOT}"
  install -d -m 0755 "$(dirname "${PROJECT_ROOT}")"
  install -d -m 0755 "${PROJECT_ROOT}"
  rsync -a \
    --exclude 'backend/.env' \
    --exclude 'backend/.venv' \
    --exclude '__pycache__/' \
    "${SCRIPT_DIR}/" "${PROJECT_ROOT}/"
else
  log "Source directory already matches deployment root; skipping project copy"
fi

log "Setting deployment file ownership and baseline permissions"
find "${PROJECT_ROOT}" -path "${PROJECT_ROOT}/backend/.venv" -prune -o -exec chown root:root {} +
find "${PROJECT_ROOT}" -path "${PROJECT_ROOT}/backend/.venv" -prune -o -type d -exec chmod 0755 {} +
find "${PROJECT_ROOT}" -path "${PROJECT_ROOT}/backend/.venv" -prune -o -type f -exec chmod 0644 {} +
chmod 0755 "${PROJECT_ROOT}/install.sh" "${PROJECT_ROOT}"/scripts/*.sh
install -d -m 0750 -o root -g "${SERVICE_USER}" "${PROJECT_ROOT}/backend"

"${PROJECT_ROOT}/scripts/bootstrap_python.sh"

if [[ "${INSTALL_FRONTEND_ASSETS}" == "1" ]]; then
  "${PROJECT_ROOT}/scripts/install_frontend_assets.sh"
else
  warn "Skipping frontend asset installation; run scripts/install_frontend_assets.sh before using the dashboard"
fi

if [[ ! -f "${PROJECT_ROOT}/backend/.env" ]]; then
  log "Creating backend/.env from backend/.env.example"
  install -m 0640 -o root -g "${SERVICE_USER}" \
    "${PROJECT_ROOT}/backend/.env.example" \
    "${PROJECT_ROOT}/backend/.env"
  warn "Edit ${PROJECT_ROOT}/backend/.env before starting services"
else
  log "Preserving existing backend/.env"
  chown root:"${SERVICE_USER}" "${PROJECT_ROOT}/backend/.env"
  chmod 0640 "${PROJECT_ROOT}/backend/.env"
fi

systemd_args=()
if [[ "${INSTALL_ENABLE_SERVICES}" == "1" ]]; then
  systemd_args+=(--enable)
fi
if [[ "${INSTALL_START_SERVICES}" == "1" ]]; then
  systemd_args+=(--start)
fi

"${PROJECT_ROOT}/scripts/install_systemd_units.sh" "${systemd_args[@]}"

log "Base deployment setup complete"
log "Next: configure Mosquitto, InfluxDB, Grafana, and Tailscale before starting services"
