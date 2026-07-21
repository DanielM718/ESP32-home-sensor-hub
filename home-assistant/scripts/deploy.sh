#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"
SOURCE_ROOT="$(source_root)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${HA_ROOT}/backups/${TIMESTAMP}"
STAGING_ROOT=""

cleanup() {
  if [[ -n "${STAGING_ROOT}" && -d "${STAGING_ROOT}" ]]; then
    rm -rf -- "${STAGING_ROOT}"
  fi
}
trap cleanup EXIT

require_linux
require_root
require_command rsync
require_docker_compose
validate_source_tree "${SOURCE_ROOT}"
[[ -f "${HA_ENV_FILE}" ]] || die "Missing ${HA_ENV_FILE}; run install.sh first"
env_has_real_mqtt_password || die "Set a real MQTT_PASSWORD in ${HA_ENV_FILE} before deployment"

copy_runtime_files "${SOURCE_ROOT}"
chmod 0600 "${HA_ENV_FILE}"
install -d -m 0750 -o 10001 -g 10001 "${HA_ROOT}/discovery-data"

log "Validating Docker Compose"
compose config --quiet

log "Pulling the stable Home Assistant image"
install -d -m 0700 "${BACKUP_ROOT}"
docker inspect --format '{{.Image}}' homeassistant > "${BACKUP_ROOT}/homeassistant-image.txt" 2>/dev/null || true
compose pull homeassistant

log "Validating the candidate Home Assistant configuration"
STAGING_ROOT="$(mktemp -d)"
rsync -a "${SOURCE_ROOT}/configuration/" "${STAGING_ROOT}/"
if ! docker run --rm \
  --env TZ=America/New_York \
  --volume "${STAGING_ROOT}:/config" \
  ghcr.io/home-assistant/home-assistant:stable \
  python -m homeassistant --script check_config --config /config; then
  die "Candidate Home Assistant configuration validation failed; installed configuration is unchanged"
fi

log "Backing up changed repository-managed configuration to ${BACKUP_ROOT}/configuration"
backup_and_update_configuration "${SOURCE_ROOT}" "${BACKUP_ROOT}/configuration"

log "Validating the installed Home Assistant configuration"
if ! compose run --rm --no-deps homeassistant \
  python -m homeassistant --script check_config --config /config; then
  if [[ -d "${BACKUP_ROOT}/configuration" ]]; then
    rsync -a "${BACKUP_ROOT}/configuration/" "${HA_CONFIG_DIR}/"
  fi
  die "Installed configuration validation failed; overwritten files were restored from backup"
fi

log "Updating the discovery companion"
compose up --detach --build mqtt-discovery

log "Recreating only the Home Assistant container"
compose up --detach --force-recreate --no-deps homeassistant

compose ps
compose logs --tail 40 homeassistant mqtt-discovery
log "Deployment complete; rollback inputs (if any) are in ${BACKUP_ROOT}"
