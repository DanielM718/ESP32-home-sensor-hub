#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

require_linux
require_docker_compose
[[ -f "${HA_COMPOSE_FILE}" ]] || die "Missing ${HA_COMPOSE_FILE}; Home Assistant is not installed"
[[ -f "${HA_ENV_FILE}" ]] || die "Missing ${HA_ENV_FILE}"

if [[ $# -eq 0 ]]; then
  compose logs --tail 100 --follow homeassistant mqtt-discovery
else
  compose logs --tail 100 --follow "$@"
fi
