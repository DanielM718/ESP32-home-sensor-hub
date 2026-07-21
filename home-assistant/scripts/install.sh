#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"
SOURCE_ROOT="$(source_root)"

require_linux
require_root
require_command rsync
require_docker_compose
validate_source_tree "${SOURCE_ROOT}"

log "Installing the isolated Home Assistant runtime in ${HA_ROOT}"
copy_runtime_files "${SOURCE_ROOT}"
copy_missing_configuration "${SOURCE_ROOT}"
install -d -m 0750 "${HA_ROOT}/backups"
install -d -m 0750 -o 10001 -g 10001 "${HA_ROOT}/discovery-data"

if [[ ! -f "${HA_ENV_FILE}" ]]; then
  install -m 0600 "${SOURCE_ROOT}/.env.example" "${HA_ENV_FILE}"
  warn "Created ${HA_ENV_FILE}; replace MQTT_PASSWORD=change_me before starting discovery"
else
  log "Preserving existing ${HA_ENV_FILE}"
  chmod 0600 "${HA_ENV_FILE}"
fi

compose config --quiet
compose pull homeassistant
compose up --detach homeassistant

if env_has_real_mqtt_password; then
  compose up --detach --build mqtt-discovery
else
  warn "MQTT discovery is not started because ${HA_ENV_FILE} still contains a placeholder password"
  warn "Edit the file, then run ${HA_ROOT}/scripts/deploy.sh from the repository source"
fi

log "Home Assistant is starting at http://$(hostname -I | awk '{print $1}'):8123"
log "No Mosquitto, InfluxDB, Grafana, bridge, dashboard, or systemd service was changed"
compose ps
