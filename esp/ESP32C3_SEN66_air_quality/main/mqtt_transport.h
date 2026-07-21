#ifndef MQTT_TRANSPORT_H
#define MQTT_TRANSPORT_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    const char *wifi_ssid;
    const char *wifi_password;
    const char *broker_host;
    uint16_t broker_port;
    const char *client_id;
    const char *username;
    const char *password;
    int qos;
    uint32_t network_timeout_ms;
    uint32_t reconnect_timeout_ms;
} mqtt_transport_config_t;

esp_err_t mqtt_transport_start(const mqtt_transport_config_t *config);
bool mqtt_transport_wifi_is_connected(void);
bool mqtt_transport_is_connected(void);
esp_err_t mqtt_transport_publish(const char *topic, const char *payload);

#endif
