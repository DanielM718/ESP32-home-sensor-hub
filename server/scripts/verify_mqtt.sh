#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

MOSQUITTO_CONF_TARGET="${MOSQUITTO_CONF_TARGET:-/etc/mosquitto/conf.d/home-sensor.conf}"
MOSQUITTO_ACL_TARGET="${MOSQUITTO_ACL_TARGET:-/etc/mosquitto/acl.d/home-sensor.acl}"
MOSQUITTO_PASSWORD_FILE="${MOSQUITTO_PASSWORD_FILE:-/etc/mosquitto/passwd}"
GATEWAY_USER="${MQTT_GATEWAY_USERNAME:-home_sensor_gateway}"
BRIDGE_USER="${MQTT_USERNAME:-home_sensor_bridge}"
HOME_ASSISTANT_USER="${HOME_ASSISTANT_MQTT_USERNAME:-home_assistant}"
VERIFY_HOME_ASSISTANT_MQTT="${VERIFY_HOME_ASSISTANT_MQTT:-0}"
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

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

acl_has_user() {
  grep -qx "user $1" "${MOSQUITTO_ACL_TARGET}"
}

password_file_restricted() {
  [[ -f "${MOSQUITTO_PASSWORD_FILE}" ]] || return 1
  local mode owner
  mode="$(stat -c '%a' "${MOSQUITTO_PASSWORD_FILE}")"
  owner="$(stat -c '%U' "${MOSQUITTO_PASSWORD_FILE}")"
  [[ "${mode}" == "640" && "${owner}" == "root" ]]
}

mosquitto_service_known() {
  systemctl cat mosquitto.service >/dev/null 2>&1
}

require_linux

check "mosquitto command exists" command_exists mosquitto
check "mosquitto_pub command exists" command_exists mosquitto_pub
check "mosquitto_sub command exists" command_exists mosquitto_sub
check "home sensor Mosquitto config installed" file_exists "${MOSQUITTO_CONF_TARGET}"
check "home sensor Mosquitto ACL installed" file_exists "${MOSQUITTO_ACL_TARGET}"
check "Mosquitto password file exists" file_exists "${MOSQUITTO_PASSWORD_FILE}"
check "Mosquitto password file has restricted permissions" password_file_restricted
check "ACL contains gateway user (${GATEWAY_USER})" acl_has_user "${GATEWAY_USER}"
check "ACL contains bridge user (${BRIDGE_USER})" acl_has_user "${BRIDGE_USER}"
if [[ "${VERIFY_HOME_ASSISTANT_MQTT}" == "1" ]]; then
  check "ACL contains Home Assistant user (${HOME_ASSISTANT_USER})" acl_has_user "${HOME_ASSISTANT_USER}"
fi

if command -v systemctl >/dev/null 2>&1; then
  check "mosquitto service is known to systemd" mosquitto_service_known
fi

if [[ "${FAILED}" == "0" ]]; then
  log "MQTT verification passed"
else
  die "MQTT verification failed"
fi
