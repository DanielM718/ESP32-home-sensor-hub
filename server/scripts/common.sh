#!/usr/bin/env bash

log() {
  printf '[home-sensor] %s\n' "$*"
}

warn() {
  printf '[home-sensor] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[home-sensor] ERROR: %s\n' "$*" >&2
  exit 1
}

require_linux() {
  local kernel
  kernel="$(uname -s)"
  [[ "${kernel}" == "Linux" ]] || die "This script must run on Raspberry Pi OS/Linux, not ${kernel}"
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this script as root, for example with sudo"
}

require_command() {
  local command_name="$1"
  command -v "${command_name}" >/dev/null 2>&1 || die "Required command not found: ${command_name}"
}

detect_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi

  die "python3 is required"
}

safe_systemctl() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl "$@"
  else
    die "systemctl is required on the Raspberry Pi deployment target"
  fi
}
