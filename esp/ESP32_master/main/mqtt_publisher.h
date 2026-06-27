#ifndef MQTT_PUBLISHER_H
#define MQTT_PUBLISHER_H

#include <stdbool.h>

#include "esp_err.h"

esp_err_t mqtt_publisher_start(void);
bool mqtt_publisher_is_connected(void);
esp_err_t mqtt_publisher_publish(const char *topic, const char *payload, int qos);

#endif
