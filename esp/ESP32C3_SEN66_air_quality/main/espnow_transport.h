#ifndef ESPNOW_TRANSPORT_H
#define ESPNOW_TRANSPORT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_now.h"

typedef struct {
    const uint8_t *peer_mac;
    uint8_t channel;
    uint32_t send_timeout_ms;
} espnow_transport_config_t;

esp_err_t espnow_transport_init(const espnow_transport_config_t *config);
esp_err_t espnow_transport_send(const void *payload, size_t payload_len, bool *send_confirmed);
bool espnow_transport_is_ready(void);

#endif

