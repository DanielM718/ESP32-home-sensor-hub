#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

require_linux
require_docker_compose
[[ -f "${HA_COMPOSE_FILE}" ]] || die "Missing ${HA_COMPOSE_FILE}; Home Assistant is not installed"
[[ -f "${HA_ENV_FILE}" ]] || die "Missing ${HA_ENV_FILE}"

printf '\nContainer state\n'
compose ps

printf '\nHome Assistant inspect state\n'
docker inspect --format 'status={{.State.Status}} running={{.State.Running}} restart_count={{.RestartCount}} started={{.State.StartedAt}}' homeassistant 2>/dev/null || true

printf '\nConfiguration directory\n'
if [[ -d "${HA_CONFIG_DIR}" ]]; then
  printf '[ok] %s exists\n' "${HA_CONFIG_DIR}"
else
  printf '[fail] %s does not exist\n' "${HA_CONFIG_DIR}"
fi

printf '\nPort 8123\n'
if curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8123/ >/dev/null; then
  printf '[ok] http://127.0.0.1:8123 is reachable\n'
else
  printf '[fail] http://127.0.0.1:8123 is not reachable\n'
fi

printf '\nMQTT broker TCP reachability\n'
if timeout 3 bash -c '</dev/tcp/127.0.0.1/1883' 2>/dev/null; then
  printf '[ok] 127.0.0.1:1883 accepts TCP connections\n'
else
  printf '[fail] 127.0.0.1:1883 is not reachable\n'
fi

printf '\nRecent logs\n'
compose logs --tail 30 homeassistant mqtt-discovery
