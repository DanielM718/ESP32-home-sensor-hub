#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

INFLUXDATA_KEY_URL="${INFLUXDATA_KEY_URL:-https://repos.influxdata.com/influxdata-archive.key}"
INFLUXDATA_KEY_FINGERPRINT="${INFLUXDATA_KEY_FINGERPRINT:-24C975CBA61A024EE1B631787C3D57159FC2F927}"
INFLUX_APT_SOURCE="${INFLUX_APT_SOURCE:-deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main}"
INFLUX_CLI_VERSION="${INFLUX_CLI_VERSION:-2.8.0}"

require_linux
require_root
require_command apt-get

detect_influx_cli_arch() {
  case "$(uname -m)" in
    aarch64|arm64)
      printf 'arm64'
      ;;
    x86_64|amd64)
      printf 'amd64'
      ;;
    *)
      die "Unsupported architecture for influx CLI: $(uname -m)"
      ;;
  esac
}

install_influxdb_server() {
  local key_tmp
  key_tmp="$(mktemp)"

  log "Installing InfluxDB installer prerequisites"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl gpg tar
  require_command curl
  require_command gpg
  require_command tar

  log "Adding InfluxData apt repository"
  install -d -m 0755 -o root -g root /etc/apt/keyrings
  curl --fail --silent --show-error --location --output "${key_tmp}" "${INFLUXDATA_KEY_URL}"

  gpg --show-keys --with-fingerprint --with-colons "${key_tmp}" 2>&1 \
    | grep -q "${INFLUXDATA_KEY_FINGERPRINT}" \
    || die "InfluxData repository key fingerprint did not match expected value"

  gpg --dearmor --yes --output /etc/apt/keyrings/influxdata-archive.gpg "${key_tmp}"
  chmod 0644 /etc/apt/keyrings/influxdata-archive.gpg
  printf '%s\n' "${INFLUX_APT_SOURCE}" > /etc/apt/sources.list.d/influxdata.list
  chmod 0644 /etc/apt/sources.list.d/influxdata.list

  log "Installing InfluxDB OSS v2 server package"
  apt-get update
  apt-get install -y influxdb2

  if command -v systemctl >/dev/null 2>&1; then
    log "Enabling and starting influxdb.service"
    systemctl enable --now influxdb.service
  else
    warn "systemctl not available; start InfluxDB manually"
  fi
}

install_influx_cli() {
  local arch archive_url tmp_dir archive_path
  arch="$(detect_influx_cli_arch)"
  archive_url="https://dl.influxdata.com/influxdb/releases/influxdb2-client-${INFLUX_CLI_VERSION}-linux-${arch}.tar.gz"
  tmp_dir="$(mktemp -d)"
  archive_path="${tmp_dir}/influxdb2-client.tar.gz"

  log "Installing influx CLI ${INFLUX_CLI_VERSION} for linux-${arch}"
  curl --fail --silent --show-error --location --output "${archive_path}" "${archive_url}"
  tar xzf "${archive_path}" --directory "${tmp_dir}"

  local influx_binary
  influx_binary="$(find "${tmp_dir}" -type f -name influx -perm -111 | head -n 1)"
  [[ -n "${influx_binary}" ]] || die "Downloaded influx CLI archive did not contain an executable named influx"

  install -m 0755 -o root -g root "${influx_binary}" /usr/local/bin/influx
}

install_influxdb_server
install_influx_cli

log "InfluxDB server and CLI installation step complete"
