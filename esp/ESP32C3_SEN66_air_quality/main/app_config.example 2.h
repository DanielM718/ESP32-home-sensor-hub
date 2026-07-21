#ifndef APP_CONFIG_H
#define APP_CONFIG_H

#include "driver/gpio.h"

#define APP_NODE_ID 100U

// Must match the gateway/router 2.4 GHz channel used by ESP-NOW.
#define APP_ESPNOW_CHANNEL 6

// Replace with the ESP32 master gateway STA MAC address in main/app_config.h.
#define APP_ESPNOW_PEER_MAC { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 }

// Seeed XIAO ESP32-C3 common I2C pins: D4/SDA = GPIO6, D5/SCL = GPIO7.
#define APP_I2C_SDA_GPIO GPIO_NUM_6
#define APP_I2C_SCL_GPIO GPIO_NUM_7
#define APP_I2C_FREQ_HZ 100000U

#define APP_MEASUREMENT_INTERVAL_MS 5000U
#define APP_MEASUREMENT_TASK_STACK_SIZE 4096U
#define APP_MEASUREMENT_TASK_PRIORITY 5U
#define APP_ESPNOW_SEND_TIMEOUT_MS 500U

#endif
