#ifndef APP_CONFIG_H
#define APP_CONFIG_H

#include "driver/gpio.h"

// Copy this file to app_config.h and edit the copy. app_config.h is ignored by
// git so Wi-Fi and MQTT credentials stay local.

#define APP_NODE_ID 100U
#define APP_FIRMWARE_VERSION "2.0.0"

// APP_LOCATION becomes the final topic segment: home/air/<location>.
// The server accepts 1-64 letters, digits, underscores, or hyphens.
#define APP_LOCATION "office"

#define APP_WIFI_SSID "replace-with-wifi-ssid"
#define APP_WIFI_PASSWORD "replace-with-wifi-password"

#define APP_MQTT_BROKER_HOST "192.168.1.10"
#define APP_MQTT_BROKER_PORT 1883U
#define APP_MQTT_CLIENT_ID "sen66-office"
#define APP_MQTT_USERNAME "home_sensor_gateway"
#define APP_MQTT_PASSWORD "replace-with-mqtt-password"
#define APP_MQTT_QOS 1
#define APP_MQTT_NETWORK_TIMEOUT_MS 10000U
#define APP_MQTT_RECONNECT_TIMEOUT_MS 5000U

// Seeed XIAO ESP32-C3: D4/SDA = GPIO6, D5/SCL = GPIO7.
#define APP_I2C_SDA_GPIO GPIO_NUM_6
#define APP_I2C_SCL_GPIO GPIO_NUM_7
#define APP_I2C_FREQ_HZ 100000U

// SEN66 produces a new sample once per second. Publishing every five seconds
// keeps traffic modest while maintaining useful air-quality resolution.
#define APP_MEASUREMENT_INTERVAL_MS 5000U
#define APP_MEASUREMENT_TASK_STACK_SIZE 4096U
#define APP_MEASUREMENT_TASK_PRIORITY 5U
#define APP_SENSOR_INIT_RETRY_MS 5000U
#define APP_SENSOR_REINIT_AFTER_ERRORS 3U

#endif
