#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

require_linux
require_root
require_command apt-get

log "Installing base Raspberry Pi OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  python3 \
  python3-pip \
  python3-venv \
  rsync

log "Base Raspberry Pi OS packages are installed"
