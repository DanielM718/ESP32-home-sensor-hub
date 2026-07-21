#!/usr/bin/env bash

HA_ROOT="/opt/home-assistant"
HA_CONFIG_DIR="${HA_ROOT}/config"
HA_COMPOSE_FILE="${HA_ROOT}/compose.yaml"
HA_ENV_FILE="${HA_ROOT}/.env"

log() {
  printf '[home-assistant] %s\n' "$*"
}

warn() {
  printf '[home-assistant] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[home-assistant] ERROR: %s\n' "$*" >&2
  exit 1
}

require_linux() {
  [[ "$(uname -s)" == "Linux" ]] || die "This script must run on Raspberry Pi OS/Linux"
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script with sudo"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

require_docker_compose() {
  require_command docker
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (docker compose)"
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable; start or enable Docker first"
}

compose() {
  docker compose \
    --project-directory "${HA_ROOT}" \
    --env-file "${HA_ENV_FILE}" \
    --file "${HA_COMPOSE_FILE}" \
    "$@"
}

source_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
  cd "${script_dir}/.." && pwd
}

validate_source_tree() {
  local root="$1"
  local required
  for required in \
    compose.yaml \
    .env.example \
    configuration/configuration.yaml \
    configuration/automations.yaml \
    configuration/scripts.yaml \
    configuration/scenes.yaml \
    discovery/Dockerfile \
    discovery/home_sensor_discovery/main.py; do
    [[ -f "${root}/${required}" ]] || die "Missing source file: ${root}/${required}"
  done
}

copy_runtime_files() {
  local root="$1"
  install -d -m 0755 "${HA_ROOT}" "${HA_ROOT}/scripts" "${HA_ROOT}/discovery"
  if [[ "${root}" != "${HA_ROOT}" ]]; then
    install -m 0644 "${root}/compose.yaml" "${HA_COMPOSE_FILE}"
    install -m 0644 "${root}/.env.example" "${HA_ROOT}/.env.example"
    rsync -a --delete "${root}/discovery/" "${HA_ROOT}/discovery/"
    rsync -a --delete "${root}/scripts/" "${HA_ROOT}/scripts/"
  fi
  chmod 0755 "${HA_ROOT}"/scripts/*.sh
}

copy_missing_configuration() {
  local root="$1"
  local source_file relative target_file
  install -d -m 0750 "${HA_CONFIG_DIR}"
  while IFS= read -r -d '' source_file; do
    relative="${source_file#"${root}/configuration/"}"
    target_file="${HA_CONFIG_DIR}/${relative}"
    install -d -m 0750 "$(dirname "${target_file}")"
    if [[ -e "${target_file}" ]]; then
      log "Preserving existing configuration: ${target_file}"
    else
      install -m 0640 "${source_file}" "${target_file}"
      log "Installed configuration template: ${target_file}"
    fi
  done < <(find "${root}/configuration" -type f -print0)
}

backup_and_update_configuration() {
  local root="$1"
  local backup_root="$2"
  local source_file relative target_file backup_file
  install -d -m 0700 "${backup_root}"
  while IFS= read -r -d '' source_file; do
    relative="${source_file#"${root}/configuration/"}"
    target_file="${HA_CONFIG_DIR}/${relative}"
    backup_file="${backup_root}/${relative}"
    install -d -m 0750 "$(dirname "${target_file}")"
    if [[ -f "${target_file}" ]] && ! cmp -s "${source_file}" "${target_file}"; then
      install -D -m 0600 "${target_file}" "${backup_file}"
    fi
    install -m 0640 "${source_file}" "${target_file}"
  done < <(find "${root}/configuration" -type f -print0)
}

env_has_real_mqtt_password() {
  [[ -f "${HA_ENV_FILE}" ]] || return 1
  grep -Eq '^MQTT_PASSWORD=.+$' "${HA_ENV_FILE}" || return 1
  ! grep -Eq '^MQTT_PASSWORD=(change_me|change-this.*|)$' "${HA_ENV_FILE}"
}
