#include "espnow_channel_recovery.h"

#include <string.h>

static bool reserved_bytes_are_zero(const uint8_t *bytes, size_t len)
{
    for (size_t i = 0; i < len; ++i) {
        if (bytes[i] != 0) {
            return false;
        }
    }
    return true;
}

bool espnow_channel_is_valid(uint8_t channel)
{
    return channel >= ESPNOW_US_CHANNEL_MIN &&
           channel <= ESPNOW_US_CHANNEL_MAX;
}

bool espnow_discovery_has_magic(const uint8_t *data, size_t len)
{
    uint32_t magic;

    if (data == NULL || len < sizeof(magic)) {
        return false;
    }

    memcpy(&magic, data, sizeof(magic));
    return magic == ESPNOW_DISCOVERY_MAGIC;
}

bool espnow_discovery_parse_request(const uint8_t *data, size_t len,
                                    espnow_discovery_request_t *request)
{
    espnow_discovery_request_t decoded;

    if (data == NULL || request == NULL || len != sizeof(decoded)) {
        return false;
    }

    memcpy(&decoded, data, sizeof(decoded));
    if (decoded.magic != ESPNOW_DISCOVERY_MAGIC ||
        decoded.version != ESPNOW_DISCOVERY_VERSION ||
        decoded.packet_type != ESPNOW_DISCOVERY_REQUEST ||
        decoded.reserved != 0) {
        return false;
    }

    *request = decoded;
    return true;
}

bool espnow_discovery_response_matches(const uint8_t *data, size_t len,
                                       uint32_t expected_node_id,
                                       uint32_t expected_nonce,
                                       espnow_discovery_response_t *response)
{
    espnow_discovery_response_t decoded;

    if (data == NULL || response == NULL || len != sizeof(decoded)) {
        return false;
    }

    memcpy(&decoded, data, sizeof(decoded));
    if (decoded.magic != ESPNOW_DISCOVERY_MAGIC ||
        decoded.version != ESPNOW_DISCOVERY_VERSION ||
        decoded.packet_type != ESPNOW_DISCOVERY_RESPONSE ||
        decoded.reserved != 0 ||
        !reserved_bytes_are_zero(decoded.reserved_tail,
                                 sizeof(decoded.reserved_tail)) ||
        decoded.node_id != expected_node_id ||
        decoded.nonce != expected_nonce ||
        !espnow_channel_is_valid(decoded.channel)) {
        return false;
    }

    *response = decoded;
    return true;
}

static void append_channel(uint8_t channel, uint8_t *channels,
                           size_t channel_capacity, size_t *count)
{
    if (!espnow_channel_is_valid(channel) || *count >= channel_capacity) {
        return;
    }

    for (size_t i = 0; i < *count; ++i) {
        if (channels[i] == channel) {
            return;
        }
    }

    channels[(*count)++] = channel;
}

size_t espnow_build_channel_search_order(uint8_t cached_channel,
                                         uint8_t *channels,
                                         size_t channel_capacity)
{
    static const uint8_t common_channels[] = {1, 6, 11};
    size_t count = 0;

    if (channels == NULL || channel_capacity == 0) {
        return 0;
    }

    append_channel(cached_channel, channels, channel_capacity, &count);
    for (size_t i = 0; i < sizeof(common_channels); ++i) {
        append_channel(common_channels[i], channels, channel_capacity, &count);
    }
    for (uint8_t channel = ESPNOW_US_CHANNEL_MIN;
         channel <= ESPNOW_US_CHANNEL_MAX; ++channel) {
        append_channel(channel, channels, channel_capacity, &count);
    }

    return count;
}

uint8_t espnow_recovery_failure_count(uint8_t previous_count, bool success)
{
    if (success) {
        return 0;
    }
    return previous_count < 4 ? (uint8_t)(previous_count + 1) : 4;
}

uint32_t espnow_recovery_sleep_seconds(uint8_t failure_count)
{
    static const uint32_t backoff_seconds[] = {900, 60, 120, 300, 900};

    return failure_count < 4 ? backoff_seconds[failure_count]
                             : backoff_seconds[4];
}

bool espnow_channel_cache_needs_write(uint8_t persisted_channel,
                                      uint8_t working_channel)
{
    return espnow_channel_is_valid(working_channel) &&
           persisted_channel != working_channel;
}
