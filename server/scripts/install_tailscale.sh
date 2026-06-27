#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-sensor-pi}"
TAILSCALE_AUTHKEY="${TAILSCALE_AUTHKEY:-}"
TAILSCALE_ADVERTISE_TAGS="${TAILSCALE_ADVERTISE_TAGS:-}"
TAILSCALE_ENABLE_SSH="${TAILSCALE_ENABLE_SSH:-0}"
TAILSCALE_INSTALLER_URL="${TAILSCALE_INSTALLER_URL:-https://tailscale.com/install.sh}"

usage() {
  cat <<USAGE
Usage: sudo scripts/install_tailscale.sh [options]

Install and authenticate Tailscale on the Raspberry Pi.

Options:
  --hostname NAME          Tailscale device hostname. Default: sensor-pi
  --auth-key KEY           Optional auth key for non-interactive setup
  --advertise-tags TAGS    Optional comma-separated tags, for example tag:home-sensor
  --enable-ssh             Enable Tailscale SSH during tailscale up
  -h, --help               Show this help

This script does not configure Tailscale Funnel, Tailscale Serve, subnet routes,
or exit-node behavior.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname)
      TAILSCALE_HOSTNAME="${2:-}"
      [[ -n "${TAILSCALE_HOSTNAME}" ]] || die "--hostname requires a value"
      shift 2
      ;;
    --auth-key)
      TAILSCALE_AUTHKEY="${2:-}"
      [[ -n "${TAILSCALE_AUTHKEY}" ]] || die "--auth-key requires a value"
      shift 2
      ;;
    --advertise-tags)
      TAILSCALE_ADVERTISE_TAGS="${2:-}"
      [[ -n "${TAILSCALE_ADVERTISE_TAGS}" ]] || die "--advertise-tags requires a value"
      shift 2
      ;;
    --enable-ssh)
      TAILSCALE_ENABLE_SSH=1
      shift
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
require_command curl

if ! command -v tailscale >/dev/null 2>&1; then
  log "Installing Tailscale using the official Linux installer"
  installer="$(mktemp)"
  trap 'rm -f "${installer}"' EXIT
  curl --fail --silent --show-error --location --output "${installer}" "${TAILSCALE_INSTALLER_URL}"
  sh "${installer}"
else
  log "Tailscale is already installed"
fi

require_command tailscale

if command -v systemctl >/dev/null 2>&1; then
  log "Enabling and starting tailscaled"
  systemctl enable --now tailscaled.service
fi

up_args=("--hostname=${TAILSCALE_HOSTNAME}")

if [[ -n "${TAILSCALE_AUTHKEY}" ]]; then
  up_args+=("--auth-key=${TAILSCALE_AUTHKEY}")
fi

if [[ -n "${TAILSCALE_ADVERTISE_TAGS}" ]]; then
  up_args+=("--advertise-tags=${TAILSCALE_ADVERTISE_TAGS}")
fi

if [[ "${TAILSCALE_ENABLE_SSH}" == "1" ]]; then
  up_args+=("--ssh")
fi

log "Authenticating this Raspberry Pi with Tailscale"
tailscale up "${up_args[@]}"

log "Tailscale setup complete"
tailscale status
tailscale ip -4 || true
