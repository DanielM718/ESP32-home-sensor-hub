#!/usr/bin/env bash
set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
esp_dir=$(cd "$test_dir/.." && pwd)
test_build_dir=$(mktemp -d)
trap 'rm -rf "$test_build_dir"' EXIT

cc -std=c11 -Wall -Wextra -Werror -pedantic \
    -I"$test_dir/fakes" \
    -I"$esp_dir/components/espnow_channel_recovery/include" \
    -I"$esp_dir/ESP32_master/main" \
    "$test_dir/test_channel_recovery.c" \
    "$esp_dir/components/espnow_channel_recovery/espnow_channel_recovery.c" \
    "$esp_dir/ESP32_master/main/gateway_packets.c" \
    -o "$test_build_dir/test_channel_recovery"

"$test_build_dir/test_channel_recovery"
