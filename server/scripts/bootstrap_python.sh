#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

SERVICE_USER="${SERVICE_USER:-home-sensor}"
PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
BACKEND_DIR="${PROJECT_ROOT}/backend"
VENV_DIR="${BACKEND_DIR}/.venv"
REQUIREMENTS_FILE="${PROJECT_ROOT}/requirements.txt"
PYTHON_BIN="${PYTHON_BIN:-$(detect_python)}"

require_linux
require_root
require_command runuser

[[ -f "${REQUIREMENTS_FILE}" ]] || die "Missing requirements file: ${REQUIREMENTS_FILE}"
id -u "${SERVICE_USER}" >/dev/null 2>&1 || die "Missing service user: ${SERVICE_USER}"

install -d -m 0750 -o root -g "${SERVICE_USER}" "${BACKEND_DIR}"

if [[ ! -d "${VENV_DIR}" ]]; then
  log "Creating Python virtual environment at ${VENV_DIR}"
  install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${VENV_DIR}"
  runuser -u "${SERVICE_USER}" -- "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
  log "Python virtual environment already exists at ${VENV_DIR}"
fi

[[ -x "${VENV_DIR}/bin/python" ]] || die "Virtual environment Python is missing: ${VENV_DIR}/bin/python"

log "Installing Python dependencies into the project virtual environment"
runuser -u "${SERVICE_USER}" -- "${VENV_DIR}/bin/python" -m pip install --upgrade pip
runuser -u "${SERVICE_USER}" -- "${VENV_DIR}/bin/python" -m pip install -r "${REQUIREMENTS_FILE}"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${VENV_DIR}"
chmod 0750 "${VENV_DIR}"

log "Python virtual environment is ready"
