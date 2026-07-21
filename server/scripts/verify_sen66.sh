#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT_ROOT="${PROJECT_ROOT:-/opt/home-sensor/server}"
MQTT_HOST="${MQTT_HOST:-127.0.0.1}"
MQTT_PORT="${MQTT_PORT:-1883}"
MQTT_PUBLISH_USERNAME="${MQTT_PUBLISH_USERNAME:-home_sensor_gateway}"
MQTT_PUBLISH_PASSWORD="${MQTT_PUBLISH_PASSWORD:-}"
SEN66_TEST_LOCATION="${SEN66_TEST_LOCATION:-sen66_test}"
API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8080}"
PAYLOAD_FILE="${PROJECT_ROOT}/examples/sen66-full.json"
RESPONSE_FILE="$(mktemp)"

cleanup() {
  rm -f "${RESPONSE_FILE}"
}
trap cleanup EXIT

require_linux
require_command curl
require_command mosquitto_pub
PYTHON_BIN="$(detect_python)"

[[ -n "${MQTT_PUBLISH_PASSWORD}" ]] || die "Set MQTT_PUBLISH_PASSWORD to the gateway/publisher password"
[[ "${SEN66_TEST_LOCATION}" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "SEN66_TEST_LOCATION must be a stable topic slug"
[[ -f "${PAYLOAD_FILE}" ]] || die "Missing SEN66 sample payload: ${PAYLOAD_FILE}"

topic="home/air/${SEN66_TEST_LOCATION}"
log "Publishing the full SEN66 fixture to ${topic}"
mosquitto_pub \
  -h "${MQTT_HOST}" \
  -p "${MQTT_PORT}" \
  -u "${MQTT_PUBLISH_USERNAME}" \
  -P "${MQTT_PUBLISH_PASSWORD}" \
  -t "${topic}" \
  -q 1 \
  -f "${PAYLOAD_FILE}"

log "Waiting for all nine values to reach ${API_BASE_URL}/api/latest"
for attempt in $(seq 1 15); do
  if curl --fail --silent --show-error \
      "${API_BASE_URL}/api/latest" \
      --output "${RESPONSE_FILE}"; then
    if "${PYTHON_BIN}" - "${PAYLOAD_FILE}" "${RESPONSE_FILE}" "${SEN66_TEST_LOCATION}" <<'PY'
import json
import math
import sys

payload_path, response_path, location = sys.argv[1:]
with open(payload_path, encoding="utf-8") as payload_file:
    expected = json.load(payload_file)
with open(response_path, encoding="utf-8") as response_file:
    response = json.load(response_file)

station = next(
    (item for item in response.get("air_quality", []) if item.get("location") == location),
    None,
)
if station is None:
    raise SystemExit(1)

fields = (
    "temperature_c",
    "humidity",
    "co2",
    "pm1",
    "pm25",
    "pm4",
    "pm10",
    "voc_index",
    "nox_index",
)
for field in fields:
    actual = station.get(field)
    wanted = expected[field]
    if not isinstance(actual, (int, float)) or not math.isclose(
        actual,
        wanted,
        rel_tol=0,
        abs_tol=1e-6,
    ):
        raise SystemExit(1)
PY
    then
      log "SEN66 MQTT-to-dashboard verification passed for ${topic}"
      exit 0
    fi
  fi

  sleep 2
done

die "SEN66 data did not reach the API with all expected fields; check bridge and dashboard logs"
