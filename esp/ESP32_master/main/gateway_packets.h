#ifndef GATEWAY_PACKETS_H
#define GATEWAY_PACKETS_H

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define GATEWAY_MQTT_TOPIC_MAX_LEN 64
#define GATEWAY_MQTT_PAYLOAD_MAX_LEN 256

typedef struct __attribute__((packed)) {
    uint32_t node_id;
    uint32_t sequence;
    float temp_c;
    float rh;
    uint16_t battery_mv;
    uint32_t status_flags;
} sensor_packet_t;

typedef struct {
    char topic[GATEWAY_MQTT_TOPIC_MAX_LEN];
    char payload[GATEWAY_MQTT_PAYLOAD_MAX_LEN];
} gateway_mqtt_message_t;

esp_err_t gateway_packets_build_mqtt_message(const uint8_t *data, size_t len,
                                             gateway_mqtt_message_t *message);

#endif
