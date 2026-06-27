#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include "esp_now.h"
#include "esp_mac.h"
#include "esp_err.h"
#include "esp_sleep.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"

#define NODE_ID 1
#define ESPNOW_CHANNEL 6
#define SLEEP_INTERVAL_US (15ULL * 60ULL * 1000000ULL)
#define ESPNOW_SEND_TIMEOUT_MS 500

// Seeed XIAO ESP32-C3 common I2C pins are D4/SDA = GPIO6 and D5/SCL = GPIO7.
// Verify these match your wiring before flashing.
#define I2C_SDA_GPIO GPIO_NUM_6
#define I2C_SCL_GPIO GPIO_NUM_7
#define I2C_FREQ_HZ 100000

#define USER_LED_GPIO GPIO_NUM_10
#define SHT41_I2C_ADDR 0x44
#define SHT41_CMD_HIGH_PRECISION_NO_HEATER 0xFD
#define SHT41_MEASUREMENT_DELAY_MS 10

#define STATUS_SHT41_OK BIT0
#define STATUS_ESPNOW_SEND_ATTEMPTED BIT1

static const char *TAG = "ESP32NOW_node";

// Gateway/master ESP32 STA MAC address
static const uint8_t gateway_mac[ESP_NOW_ETH_ALEN] = {
    0xd8, 0xbc, 0x38, 0xe5, 0x78, 0x8c
};

typedef struct __attribute__((packed)) {
    uint32_t node_id;
    uint32_t sequence;
    float temp_c;
    float rh;
    uint16_t battery_mv;
    uint32_t status_flags;
} sensor_packet_t;

RTC_DATA_ATTR static uint32_t sequence_number = 0;

static i2c_master_bus_handle_t i2c_bus;
static i2c_master_dev_handle_t sht41_dev;
static SemaphoreHandle_t espnow_send_sem;
static esp_now_send_status_t last_send_status = ESP_NOW_SEND_FAIL;

static void on_espnow_send(const esp_now_send_info_t *tx_info,
                           esp_now_send_status_t status)
{
    ESP_LOGI(TAG, "Send to " MACSTR " status: %s",
             MAC2STR(tx_info->des_addr),
             status == ESP_NOW_SEND_SUCCESS ? "success" : "fail");

    last_send_status = status;
    if (espnow_send_sem != NULL) {
        xSemaphoreGive(espnow_send_sem);
    }
}

static uint8_t sht41_crc8(const uint8_t *data, size_t len)
{
    uint8_t crc = 0xFF;

    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            if ((crc & 0x80) != 0) {
                crc = (uint8_t)((crc << 1) ^ 0x31);
            } else {
                crc <<= 1;
            }
        }
    }

    return crc;
}

static esp_err_t i2c_init(void)
{
    i2c_master_bus_config_t bus_config = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .i2c_port = I2C_NUM_0,
        .sda_io_num = I2C_SDA_GPIO,
        .scl_io_num = I2C_SCL_GPIO,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&bus_config, &i2c_bus);
    if (err != ESP_OK) {
        return err;
    }

    i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = SHT41_I2C_ADDR,
        .scl_speed_hz = I2C_FREQ_HZ,
    };
    err = i2c_master_bus_add_device(i2c_bus, &dev_config, &sht41_dev);
    if (err != ESP_OK) {
        return err;
    }

    ESP_LOGI(TAG, "I2C initialized: SDA GPIO%d, SCL GPIO%d, %d Hz",
             I2C_SDA_GPIO, I2C_SCL_GPIO, I2C_FREQ_HZ);
    return ESP_OK;
}

static esp_err_t sht41_read(float *temp_c, float *rh)
{
    uint8_t command = SHT41_CMD_HIGH_PRECISION_NO_HEATER;
    uint8_t data[6] = {0};
    esp_err_t err = i2c_master_transmit(sht41_dev, &command, sizeof(command),
                                        pdMS_TO_TICKS(100));
    if (err != ESP_OK) {
        return err;
    }

    vTaskDelay(pdMS_TO_TICKS(SHT41_MEASUREMENT_DELAY_MS));

    err = i2c_master_receive(sht41_dev, data, sizeof(data), pdMS_TO_TICKS(100));
    if (err != ESP_OK) {
        return err;
    }

    if (sht41_crc8(&data[0], 2) != data[2]) {
        ESP_LOGE(TAG, "SHT41 temperature CRC mismatch");
        return ESP_ERR_INVALID_CRC;
    }

    if (sht41_crc8(&data[3], 2) != data[5]) {
        ESP_LOGE(TAG, "SHT41 humidity CRC mismatch");
        return ESP_ERR_INVALID_CRC;
    }

    uint16_t raw_temp = ((uint16_t)data[0] << 8) | data[1];
    uint16_t raw_rh = ((uint16_t)data[3] << 8) | data[4];

    *temp_c = -45.0f + (175.0f * (float)raw_temp / 65535.0f);
    *rh = -6.0f + (125.0f * (float)raw_rh / 65535.0f);

    if (*rh < 0.0f) {
        *rh = 0.0f;
    } else if (*rh > 100.0f) {
        *rh = 100.0f;
    }

    return ESP_OK;
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    // ESP-NOW uses the Wi-Fi radio, but this node does not connect to your router.
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());

    // Must match the gateway/router 2.4 GHz channel.
#if ESPNOW_CHANNEL != 0
    ESP_ERROR_CHECK(esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE));
#else
    ESP_LOGW(TAG, "ESPNOW_CHANNEL is 0, leaving Wi-Fi on its current channel");
#endif

    uint8_t mac[ESP_NOW_ETH_ALEN];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, mac));
    ESP_LOGI(TAG, "Node STA MAC: " MACSTR, MAC2STR(mac));
}

static void espnow_init(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_espnow_send));

    esp_now_peer_info_t peer = {0};
    memcpy(peer.peer_addr, gateway_mac, ESP_NOW_ETH_ALEN);
    peer.channel = ESPNOW_CHANNEL;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;

    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    ESP_LOGI(TAG, "ESP-NOW initialized");
}

static void enter_deep_sleep(void)
{
    ESP_LOGI(TAG, "Entering deep sleep for %llu us", SLEEP_INTERVAL_US);
    ESP_ERROR_CHECK(esp_sleep_enable_timer_wakeup(SLEEP_INTERVAL_US));
    esp_deep_sleep_start();
}

void app_main(void)
{
    gpio_reset_pin(USER_LED_GPIO);
    gpio_set_direction(USER_LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(USER_LED_GPIO, 0); // XIAO ESP32-C3 D10 LED off

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    wifi_init();
    espnow_init();
    err = i2c_init();
    bool i2c_ready = (err == ESP_OK);
    if (!i2c_ready) {
        ESP_LOGE(TAG, "I2C init failed: %s", esp_err_to_name(err));
    }

    sensor_packet_t packet = {
        .node_id = NODE_ID,
        .sequence = sequence_number++,
        .temp_c = 0.0f,
        .rh = 0.0f,
        .battery_mv = 0,
        .status_flags = STATUS_ESPNOW_SEND_ATTEMPTED,
    };

    float temp_c = 0.0f;
    float rh = 0.0f;
    err = i2c_ready ? sht41_read(&temp_c, &rh) : ESP_ERR_INVALID_STATE;
    if (err == ESP_OK) {
        packet.temp_c = temp_c;
        packet.rh = rh;
        packet.status_flags |= STATUS_SHT41_OK;
        ESP_LOGI(TAG, "SHT41: %.2f C, %.2f %%RH", packet.temp_c, packet.rh);
    } else {
        ESP_LOGE(TAG, "SHT41 read failed: %s", esp_err_to_name(err));
    }

    espnow_send_sem = xSemaphoreCreateBinary();
    if (espnow_send_sem == NULL) {
        ESP_LOGE(TAG, "Failed to create ESP-NOW send semaphore");
    }

    last_send_status = ESP_NOW_SEND_FAIL;
    err = esp_now_send(gateway_mac, (const uint8_t *)&packet, sizeof(packet));
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Queued packet: node=%lu sequence=%lu status=0x%08lx",
                 packet.node_id, packet.sequence, packet.status_flags);

        if (espnow_send_sem != NULL &&
            xSemaphoreTake(espnow_send_sem, pdMS_TO_TICKS(ESPNOW_SEND_TIMEOUT_MS)) == pdTRUE) {
            if (last_send_status == ESP_NOW_SEND_SUCCESS) {
                ESP_LOGI(TAG, "ESP-NOW send confirmed");
            } else {
                ESP_LOGE(TAG, "ESP-NOW send callback reported failure");
            }
        } else {
            ESP_LOGE(TAG, "Timed out waiting for ESP-NOW send callback");
        }
    } else {
        ESP_LOGE(TAG, "esp_now_send failed: %s", esp_err_to_name(err));
    }

    vTaskDelay(pdMS_TO_TICKS(50));
    enter_deep_sleep();
}
