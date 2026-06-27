#include "gateway_packets.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

_Static_assert(sizeof(sensor_packet_t) == 22, "sensor_packet_t wire size changed");

typedef bool (*gateway_packet_match_fn_t)(const uint8_t *data, size_t len);
typedef esp_err_t (*gateway_packet_build_fn_t)(const uint8_t *data, size_t len,
                                               gateway_mqtt_message_t *message);

typedef struct {
    const char *name;
    gateway_packet_match_fn_t matches;
    gateway_packet_build_fn_t build_message;
} gateway_packet_handler_t;

static esp_err_t check_snprintf_result(int written, size_t buffer_len)
{
    if (written < 0) {
        return ESP_FAIL;
    }

    if ((size_t)written >= buffer_len) {
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}

static bool matches_sht41_packet(const uint8_t *data, size_t len)
{
    (void)data;

    return len == sizeof(sensor_packet_t);
}

static esp_err_t build_sht41_message(const uint8_t *data, size_t len,
                                     gateway_mqtt_message_t *message)
{
    if (data == NULL || message == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    if (len != sizeof(sensor_packet_t)) {
        return ESP_ERR_INVALID_SIZE;
    }

    sensor_packet_t packet;
    memcpy(&packet, data, sizeof(packet));

    memset(message, 0, sizeof(*message));

    int written = snprintf(message->topic, sizeof(message->topic),
                           "home/sensors/%" PRIu32, packet.node_id);
    esp_err_t err = check_snprintf_result(written, sizeof(message->topic));
    if (err != ESP_OK) {
        return err;
    }

    written = snprintf(message->payload, sizeof(message->payload),
                       "{\"packet_type\":\"sht41\",\"node_id\":%" PRIu32
                       ",\"sequence\":%" PRIu32
                       ",\"temperature_c\":%.2f"
                       ",\"humidity\":%.2f"
                       ",\"battery_mv\":%" PRIu32
                       ",\"status_flags\":%" PRIu32 "}",
                       packet.node_id,
                       packet.sequence,
                       (double)packet.temp_c,
                       (double)packet.rh,
                       (uint32_t)packet.battery_mv,
                       packet.status_flags);
    return check_snprintf_result(written, sizeof(message->payload));
}

/*
 * SHT41 packets currently have no packet type/version metadata, so the v1
 * handler matches by exact wire length. Future packet formats should include
 * explicit type metadata and add a handler here instead of changing main.c.
 */
static const gateway_packet_handler_t PACKET_HANDLERS[] = {
    {
        .name = "sht41",
        .matches = matches_sht41_packet,
        .build_message = build_sht41_message,
    },
};

esp_err_t gateway_packets_build_mqtt_message(const uint8_t *data, size_t len,
                                             gateway_mqtt_message_t *message)
{
    if (data == NULL || message == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    for (size_t i = 0; i < sizeof(PACKET_HANDLERS) / sizeof(PACKET_HANDLERS[0]); i++) {
        const gateway_packet_handler_t *handler = &PACKET_HANDLERS[i];
        if (handler->matches(data, len)) {
            return handler->build_message(data, len, message);
        }
    }

    return ESP_ERR_NOT_SUPPORTED;
}
