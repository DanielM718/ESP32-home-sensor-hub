#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "espnow_channel_recovery.h"
#include "gateway_packets.h"

static espnow_discovery_request_t valid_request(void)
{
    const espnow_discovery_request_t request = {
        .magic = ESPNOW_DISCOVERY_MAGIC,
        .version = ESPNOW_DISCOVERY_VERSION,
        .packet_type = ESPNOW_DISCOVERY_REQUEST,
        .reserved = 0,
        .node_id = 3,
        .nonce = UINT32_C(0x12345678),
    };
    return request;
}

static espnow_discovery_response_t valid_response(void)
{
    const espnow_discovery_response_t response = {
        .magic = ESPNOW_DISCOVERY_MAGIC,
        .version = ESPNOW_DISCOVERY_VERSION,
        .packet_type = ESPNOW_DISCOVERY_RESPONSE,
        .reserved = 0,
        .node_id = 3,
        .nonce = UINT32_C(0x12345678),
        .channel = 6,
        .reserved_tail = {0, 0, 0},
    };
    return response;
}

static void test_discovery_request_validation(void)
{
    espnow_discovery_request_t request = valid_request();
    espnow_discovery_request_t decoded;

    assert(espnow_discovery_parse_request((const uint8_t *)&request,
                                          sizeof(request), &decoded));
    assert(decoded.node_id == request.node_id);
    assert(decoded.nonce == request.nonce);

    request.magic ^= 1U;
    assert(!espnow_discovery_parse_request((const uint8_t *)&request,
                                           sizeof(request), &decoded));
    request = valid_request();
    request.version++;
    assert(!espnow_discovery_parse_request((const uint8_t *)&request,
                                           sizeof(request), &decoded));
    request = valid_request();
    request.packet_type = ESPNOW_DISCOVERY_RESPONSE;
    assert(!espnow_discovery_parse_request((const uint8_t *)&request,
                                           sizeof(request), &decoded));
    request = valid_request();
    request.reserved = 1;
    assert(!espnow_discovery_parse_request((const uint8_t *)&request,
                                           sizeof(request), &decoded));
    request = valid_request();
    assert(!espnow_discovery_parse_request((const uint8_t *)&request,
                                           sizeof(request) - 1U, &decoded));
}

static void test_discovery_response_validation(void)
{
    espnow_discovery_response_t response = valid_response();
    espnow_discovery_response_t decoded;

    assert(espnow_discovery_response_matches((const uint8_t *)&response,
                                             sizeof(response), 3,
                                             UINT32_C(0x12345678), &decoded));
    assert(decoded.channel == 6);
    assert(!espnow_discovery_response_matches((const uint8_t *)&response,
                                              sizeof(response), 2,
                                              UINT32_C(0x12345678), &decoded));
    assert(!espnow_discovery_response_matches((const uint8_t *)&response,
                                              sizeof(response), 3,
                                              UINT32_C(0x87654321), &decoded));

    response.magic ^= 1U;
    assert(!espnow_discovery_response_matches((const uint8_t *)&response,
                                              sizeof(response), 3,
                                              UINT32_C(0x12345678), &decoded));
    response = valid_response();
    response.version++;
    assert(!espnow_discovery_response_matches((const uint8_t *)&response,
                                              sizeof(response), 3,
                                              UINT32_C(0x12345678), &decoded));
    response = valid_response();
    assert(!espnow_discovery_response_matches((const uint8_t *)&response,
                                              sizeof(response) - 1U, 3,
                                              UINT32_C(0x12345678), &decoded));
    response = valid_response();
    response.reserved_tail[0] = 1;
    assert(!espnow_discovery_response_matches((const uint8_t *)&response,
                                              sizeof(response), 3,
                                              UINT32_C(0x12345678), &decoded));
    response = valid_response();
    response.channel = 0;
    assert(!espnow_discovery_response_matches((const uint8_t *)&response,
                                              sizeof(response), 3,
                                              UINT32_C(0x12345678), &decoded));
    response = valid_response();
    response.channel = 12;
    assert(!espnow_discovery_response_matches((const uint8_t *)&response,
                                              sizeof(response), 3,
                                              UINT32_C(0x12345678), &decoded));
}

static void test_channel_search_is_complete_and_bounded(void)
{
    uint8_t channels[ESPNOW_US_CHANNEL_COUNT] = {0};
    bool seen[ESPNOW_US_CHANNEL_MAX + 1U] = {false};
    size_t count = espnow_build_channel_search_order(
        6, channels, sizeof(channels));

    assert(count == ESPNOW_US_CHANNEL_COUNT);
    assert(channels[0] == 6);
    assert(channels[1] == 1);
    assert(channels[2] == 11);
    for (size_t i = 0; i < count; ++i) {
        assert(espnow_channel_is_valid(channels[i]));
        assert(!seen[channels[i]]);
        seen[channels[i]] = true;
    }
    for (uint8_t channel = ESPNOW_US_CHANNEL_MIN;
         channel <= ESPNOW_US_CHANNEL_MAX; ++channel) {
        assert(seen[channel]);
    }

    count = espnow_build_channel_search_order(0, channels, sizeof(channels));
    assert(count == ESPNOW_US_CHANNEL_COUNT);
    assert(channels[0] == 1);
    assert(channels[1] == 6);
    assert(channels[2] == 11);

    uint8_t short_order[3] = {0};
    assert(espnow_build_channel_search_order(9, short_order,
                                             sizeof(short_order)) == 3);
    assert(short_order[0] == 9);
}

static void test_cache_and_backoff_policy(void)
{
    assert(!espnow_channel_is_valid(0));
    assert(espnow_channel_is_valid(1));
    assert(espnow_channel_is_valid(11));
    assert(!espnow_channel_is_valid(12));

    assert(espnow_channel_cache_needs_write(0, 6));
    assert(!espnow_channel_cache_needs_write(6, 6));
    assert(!espnow_channel_cache_needs_write(6, 12));

    uint8_t failures = 0;
    failures = espnow_recovery_failure_count(failures, false);
    assert(failures == 1);
    assert(espnow_recovery_sleep_seconds(failures) == 60);
    failures = espnow_recovery_failure_count(failures, false);
    assert(failures == 2);
    assert(espnow_recovery_sleep_seconds(failures) == 120);
    failures = espnow_recovery_failure_count(failures, false);
    assert(failures == 3);
    assert(espnow_recovery_sleep_seconds(failures) == 300);
    failures = espnow_recovery_failure_count(failures, false);
    assert(failures == 4);
    assert(espnow_recovery_sleep_seconds(failures) == 900);
    assert(espnow_recovery_failure_count(failures, false) == 4);
    assert(espnow_recovery_failure_count(failures, true) == 0);
    assert(espnow_recovery_sleep_seconds(0) == 900);
}

static void test_gateway_legacy_and_control_routing(void)
{
    const sensor_packet_t sensor = {
        .node_id = 3,
        .sequence = 42,
        .temp_c = 21.5f,
        .rh = 48.25f,
        .battery_mv = 3999,
        .status_flags = 7,
    };
    gateway_mqtt_message_t message;

    assert(gateway_packets_build_mqtt_message((const uint8_t *)&sensor,
                                              sizeof(sensor), &message) == ESP_OK);
    assert(strcmp(message.topic, "home/sensors/3") == 0);
    assert(strstr(message.payload, "\"sequence\":42") != NULL);

    const espnow_discovery_request_t request = valid_request();
    assert(gateway_packets_build_mqtt_message((const uint8_t *)&request,
                                              sizeof(request), &message) ==
           ESP_ERR_NOT_SUPPORTED);

    uint8_t malformed_control[sizeof(sensor_packet_t)] = {0};
    memcpy(malformed_control, &request.magic, sizeof(request.magic));
    assert(gateway_packets_build_mqtt_message(malformed_control,
                                              sizeof(malformed_control),
                                              &message) ==
           ESP_ERR_NOT_SUPPORTED);
}

int main(void)
{
    test_discovery_request_validation();
    test_discovery_response_validation();
    test_channel_search_is_complete_and_bounded();
    test_cache_and_backoff_policy();
    test_gateway_legacy_and_control_routing();
    puts("channel recovery host tests: PASS");
    return 0;
}
