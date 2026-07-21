#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

GATEWAY_USER="${MQTT_GATEWAY_USERNAME:-home_sensor_gateway}"
BRIDGE_USER="${MQTT_USERNAME:-home_sensor_bridge}"
HOME_ASSISTANT_USER="${HOME_ASSISTANT_MQTT_USERNAME:-home_assistant}"
MOSQUITTO_PASSWORD_FILE="${MOSQUITTO_PASSWORD_FILE:-/etc/mosquitto/passwd}"
HOME_ASSISTANT_ONLY=0
INCLUDE_HOME_ASSISTANT=0

usage() {
  cat <<USAGE
Usage: sudo scripts/create_mqtt_users.sh [options]

Create or update Mosquitto users for the ESP32 gateway and Python bridge, with
optional Home Assistant account flags. Passwords are entered interactively and
are not committed.

Options:
  --gateway-user USER      Gateway MQTT username. Default: home_sensor_gateway
  --bridge-user USER       Bridge MQTT username. Default: home_sensor_bridge
  --home-assistant-user USER
                           Home Assistant username. Default: home_assistant
  --include-home-assistant Create/update the Home Assistant user in addition to
                           the existing gateway and bridge users
  --home-assistant-only    Create/update only the Home Assistant user; preserve
                           existing gateway and bridge passwords
  --password-file PATH     Password file. Default: /etc/mosquitto/passwd
  -h, --help               Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gateway-user)
      GATEWAY_USER="${2:-}"
      [[ -n "${GATEWAY_USER}" ]] || die "--gateway-user requires a value"
      shift 2
      ;;
    --bridge-user)
      BRIDGE_USER="${2:-}"
      [[ -n "${BRIDGE_USER}" ]] || die "--bridge-user requires a value"
      shift 2
      ;;
    --home-assistant-user)
      HOME_ASSISTANT_USER="${2:-}"
      [[ -n "${HOME_ASSISTANT_USER}" ]] || die "--home-assistant-user requires a value"
      INCLUDE_HOME_ASSISTANT=1
      shift 2
      ;;
    --home-assistant-only)
      HOME_ASSISTANT_ONLY=1
      INCLUDE_HOME_ASSISTANT=1
      shift
      ;;
    --include-home-assistant)
      INCLUDE_HOME_ASSISTANT=1
      shift
      ;;
    --password-file)
      MOSQUITTO_PASSWORD_FILE="${2:-}"
      [[ -n "${MOSQUITTO_PASSWORD_FILE}" ]] || die "--password-file requires a value"
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
require_command mosquitto_passwd

users_to_validate=("${GATEWAY_USER}" "${BRIDGE_USER}")
if [[ "${INCLUDE_HOME_ASSISTANT}" == "1" ]]; then
  users_to_validate+=("${HOME_ASSISTANT_USER}")
fi
if [[ "${HOME_ASSISTANT_ONLY}" == "1" ]]; then
  users_to_validate=("${HOME_ASSISTANT_USER}")
fi
for username in "${users_to_validate[@]}"; do
  [[ "${username}" != *:* ]] || die "MQTT usernames must not contain ':'"
  [[ "${username}" != *[[:space:]]* ]] || die "MQTT usernames must not contain whitespace"
done

install -d -m 0755 -o root -g root "$(dirname "${MOSQUITTO_PASSWORD_FILE}")"

if [[ "${HOME_ASSISTANT_ONLY}" == "0" ]]; then
  if [[ ! -f "${MOSQUITTO_PASSWORD_FILE}" ]]; then
    log "Creating Mosquitto password file and user: ${GATEWAY_USER}"
    mosquitto_passwd -c "${MOSQUITTO_PASSWORD_FILE}" "${GATEWAY_USER}"
  else
    log "Creating/updating Mosquitto user: ${GATEWAY_USER}"
    mosquitto_passwd "${MOSQUITTO_PASSWORD_FILE}" "${GATEWAY_USER}"
  fi

  log "Creating/updating Mosquitto user: ${BRIDGE_USER}"
  mosquitto_passwd "${MOSQUITTO_PASSWORD_FILE}" "${BRIDGE_USER}"
elif [[ ! -f "${MOSQUITTO_PASSWORD_FILE}" ]]; then
  die "${MOSQUITTO_PASSWORD_FILE} does not exist; cannot use --home-assistant-only"
fi

if [[ "${INCLUDE_HOME_ASSISTANT}" == "1" ]]; then
  log "Creating/updating Mosquitto user: ${HOME_ASSISTANT_USER}"
  mosquitto_passwd "${MOSQUITTO_PASSWORD_FILE}" "${HOME_ASSISTANT_USER}"
fi

if getent group mosquitto >/dev/null 2>&1; then
  chown root:mosquitto "${MOSQUITTO_PASSWORD_FILE}"
else
  chown root:root "${MOSQUITTO_PASSWORD_FILE}"
fi
chmod 0640 "${MOSQUITTO_PASSWORD_FILE}"

if [[ "${INCLUDE_HOME_ASSISTANT}" == "1" && -f /etc/mosquitto/acl.d/home-sensor.acl ]]; then
  if ! grep -qx "user ${HOME_ASSISTANT_USER}" /etc/mosquitto/acl.d/home-sensor.acl; then
    warn "Installed ACL does not contain all selected users"
    warn "Re-run install_mosquitto.sh with matching user options"
  fi
fi

log "Mosquitto users are ready"
if [[ "${HOME_ASSISTANT_ONLY}" == "0" ]]; then
  log "Set MQTT_USERNAME=${BRIDGE_USER} and the matching MQTT_PASSWORD in backend/.env"
  log "Configure the gateway to publish as ${GATEWAY_USER}"
fi
if [[ "${INCLUDE_HOME_ASSISTANT}" == "1" ]]; then
  log "Configure Home Assistant and /opt/home-assistant/.env as ${HOME_ASSISTANT_USER}"
fi
