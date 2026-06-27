#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

GATEWAY_USER="${MQTT_GATEWAY_USERNAME:-home_sensor_gateway}"
BRIDGE_USER="${MQTT_USERNAME:-home_sensor_bridge}"
MOSQUITTO_PASSWORD_FILE="${MOSQUITTO_PASSWORD_FILE:-/etc/mosquitto/passwd}"

usage() {
  cat <<USAGE
Usage: sudo scripts/create_mqtt_users.sh [options]

Create or update Mosquitto users for the ESP32 gateway and Python bridge.
Passwords are entered interactively by mosquitto_passwd and are not committed.

Options:
  --gateway-user USER      Gateway MQTT username. Default: home_sensor_gateway
  --bridge-user USER       Bridge MQTT username. Default: home_sensor_bridge
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

for username in "${GATEWAY_USER}" "${BRIDGE_USER}"; do
  [[ "${username}" != *:* ]] || die "MQTT usernames must not contain ':'"
  [[ "${username}" != *[[:space:]]* ]] || die "MQTT usernames must not contain whitespace"
done

install -d -m 0755 -o root -g root "$(dirname "${MOSQUITTO_PASSWORD_FILE}")"

if [[ ! -f "${MOSQUITTO_PASSWORD_FILE}" ]]; then
  log "Creating Mosquitto password file and user: ${GATEWAY_USER}"
  mosquitto_passwd -c "${MOSQUITTO_PASSWORD_FILE}" "${GATEWAY_USER}"
else
  log "Creating/updating Mosquitto user: ${GATEWAY_USER}"
  mosquitto_passwd "${MOSQUITTO_PASSWORD_FILE}" "${GATEWAY_USER}"
fi

log "Creating/updating Mosquitto user: ${BRIDGE_USER}"
mosquitto_passwd "${MOSQUITTO_PASSWORD_FILE}" "${BRIDGE_USER}"

if getent group mosquitto >/dev/null 2>&1; then
  chown root:mosquitto "${MOSQUITTO_PASSWORD_FILE}"
else
  chown root:root "${MOSQUITTO_PASSWORD_FILE}"
fi
chmod 0640 "${MOSQUITTO_PASSWORD_FILE}"

if [[ -f /etc/mosquitto/acl.d/home-sensor.acl ]]; then
  if ! grep -qx "user ${GATEWAY_USER}" /etc/mosquitto/acl.d/home-sensor.acl ||
     ! grep -qx "user ${BRIDGE_USER}" /etc/mosquitto/acl.d/home-sensor.acl; then
    warn "Installed ACL does not contain both selected users"
    warn "Re-run install_mosquitto.sh with matching --gateway-user and --bridge-user values"
  fi
fi

log "Mosquitto users are ready"
log "Set MQTT_USERNAME=${BRIDGE_USER} and the matching MQTT_PASSWORD in backend/.env"
log "Configure the gateway to publish as ${GATEWAY_USER}"
