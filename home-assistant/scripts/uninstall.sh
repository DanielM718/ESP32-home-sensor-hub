#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"
DELETE_DATA=0

usage() {
  cat <<'USAGE'
Usage: sudo uninstall.sh [--delete-data]

Stop and remove only the Home Assistant and MQTT discovery containers.
Persistent configuration, secrets, device registry, and backups are preserved
unless --delete-data is explicitly supplied.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --delete-data)
      DELETE_DATA=1
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

require_linux
require_root
require_docker_compose
[[ "${HA_ROOT}" == "/opt/home-assistant" ]] || die "Refusing unexpected uninstall target: ${HA_ROOT}"

if [[ -f "${HA_COMPOSE_FILE}" && -f "${HA_ENV_FILE}" ]]; then
  compose down --remove-orphans
else
  warn "Compose or environment file is missing; no container removal was attempted"
fi

if [[ "${DELETE_DATA}" == "1" ]]; then
  warn "Deleting persistent Home Assistant data under ${HA_ROOT}"
  rm -rf -- "${HA_CONFIG_DIR}" "${HA_ROOT}/discovery-data" "${HA_ROOT}/backups" "${HA_ENV_FILE}"
  log "Persistent Home Assistant data was deleted and is not recoverable without another backup"
else
  log "Preserved ${HA_CONFIG_DIR}, ${HA_ROOT}/discovery-data, ${HA_ROOT}/backups, and ${HA_ENV_FILE}"
fi

log "Existing Mosquitto, InfluxDB, Grafana, Flask, bridge, and systemd services were not touched"
