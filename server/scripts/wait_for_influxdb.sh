#!/usr/bin/env bash
set -Eeuo pipefail

INFLUXDB_URL="${INFLUXDB_URL:-http://127.0.0.1:8086}"
INFLUXDB_ORG="${INFLUXDB_ORG:-}"
INFLUXDB_READY_TOKEN="${INFLUXDB_READ_TOKEN:-${INFLUXDB_TOKEN:-}}"
INFLUXDB_READY_TIMEOUT_SECONDS="${INFLUXDB_READY_TIMEOUT_SECONDS:-30}"
INFLUXDB_READY_RETRY_SECONDS="${INFLUXDB_READY_RETRY_SECONDS:-1}"

if [[ ! "${INFLUXDB_READY_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'Invalid InfluxDB readiness timeout: %s\n' "${INFLUXDB_READY_TIMEOUT_SECONDS}" >&2
  exit 2
fi

if [[ -z "${INFLUXDB_ORG}" || -z "${INFLUXDB_READY_TOKEN}" ]]; then
  printf 'InfluxDB readiness requires INFLUXDB_ORG and INFLUXDB_READ_TOKEN (or INFLUXDB_TOKEN)\n' >&2
  exit 2
fi

health_url="${INFLUXDB_URL%/}/health"
deadline=$((SECONDS + INFLUXDB_READY_TIMEOUT_SECONDS))
attempt=0
last_error="no response"

printf 'Waiting up to %ss for InfluxDB health and query readiness at %s\n' \
  "${INFLUXDB_READY_TIMEOUT_SECONDS}" "${INFLUXDB_URL}"

while (( SECONDS < deadline )); do
  attempt=$((attempt + 1))
  if last_error="$(curl --fail --silent --show-error --max-time 2 \
    --output /dev/null "${health_url}" 2>&1)"; then
    if last_error="$(timeout --foreground 5s env \
      INFLUX_HOST="${INFLUXDB_URL}" \
      INFLUX_ORG="${INFLUXDB_ORG}" \
      INFLUX_TOKEN="${INFLUXDB_READY_TOKEN}" \
      influx query 'buckets() |> limit(n: 1)' 2>&1 >/dev/null)"; then
      printf 'InfluxDB accepted a query after %s attempt(s)\n' "${attempt}"
      exit 0
    fi
    [[ -n "${last_error}" ]] || last_error="Flux query failed or exceeded 5s"
  fi

  if (( attempt == 1 || attempt % 5 == 0 )); then
    printf 'InfluxDB not ready (attempt %s): %s\n' "${attempt}" "${last_error}" >&2
  fi

  (( SECONDS < deadline )) && sleep "${INFLUXDB_READY_RETRY_SECONDS}"
done

printf 'InfluxDB readiness timed out after %ss (%s attempts): %s\n' \
  "${INFLUXDB_READY_TIMEOUT_SECONDS}" "${attempt}" "${last_error}" >&2
exit 1
