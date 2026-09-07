#ifndef ESPNOW_CHANNEL_RECOVERY_H
#define ESPNOW_CHANNEL_RECOVERY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ESPNOW_DISCOVERY_MAGIC UINT32_C(0x45534e43)
#define ESPNOW_DISCOVERY_VERSION UINT8_C(1)
#define ESPNOW_US_CHANNEL_MIN UINT8_C(1)
#define ESPNOW_US_CHANNEL_MAX UINT8_C(11)
#define ESPNOW_US_CHANNEL_COUNT 11U

typedef enum {
    ESPNOW_DISCOVERY_REQUEST = 1,
    ESPNOW_DISCOVERY_RESPONSE = 2,
} espnow_discovery_packet_type_t;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t version;
    uint8_t packet_type;
    uint16_t reserved;
    uint32_t node_id;
    uint32_t nonce;
} espnow_discovery_request_t;

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t version;
    uint8_t packet_type;
    uint16_t reserved;
    uint32_t node_id;
    uint32_t nonce;
    uint8_t channel;
    uint8_t reserved_tail[3];
} espnow_discovery_response_t;

_Static_assert(sizeof(espnow_discovery_request_t) == 16,
               "discovery request wire size changed");
_Static_assert(sizeof(espnow_discovery_response_t) == 20,
               "discovery response wire size changed");

bool espnow_channel_is_valid(uint8_t channel);
bool espnow_discovery_has_magic(const uint8_t *data, size_t len);
bool espnow_discovery_parse_request(const uint8_t *data, size_t len,
                                    espnow_discovery_request_t *request);
bool espnow_discovery_response_matches(const uint8_t *data, size_t len,
                                       uint32_t expected_node_id,
                                       uint32_t expected_nonce,
                                       espnow_discovery_response_t *response);
size_t espnow_build_channel_search_order(uint8_t cached_channel,
                                         uint8_t *channels,
                                         size_t channel_capacity);
uint8_t espnow_recovery_failure_count(uint8_t previous_count, bool success);
uint32_t espnow_recovery_sleep_seconds(uint8_t failure_count);
bool espnow_channel_cache_needs_write(uint8_t persisted_channel,
                                      uint8_t working_channel);

#endif
