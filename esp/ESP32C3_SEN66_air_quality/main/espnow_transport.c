#include "espnow_transport.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "ESPNOW";

static uint8_t s_peer_mac[ESP_NOW_ETH_ALEN] = {0};
static uint8_t s_channel = 0;
static uint32_t s_send_timeout_ms = 0;
static SemaphoreHandle_t s_send_sem = NULL;
static esp_now_send_status_t s_last_send_status = ESP_NOW_SEND_FAIL;
static bool s_ready = false;

static bool mac_is_all_zero(const uint8_t mac[ESP_NOW_ETH_ALEN])
{
    uint8_t any_bits = 0;

    for (size_t i = 0; i < ESP_NOW_ETH_ALEN; i++) {
        any_bits |= mac[i];
    }

    return any_bits == 0;
}

static bool mac_is_group_address(const uint8_t mac[ESP_NOW_ETH_ALEN])
{
    return (mac[0] & 0x01) != 0;
}

static esp_err_t validate_config(const espnow_transport_config_t *config)
{
    if (config == NULL || config->peer_mac == NULL || config->send_timeout_ms == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    if (config->channel > 14) {
        ESP_LOGE(TAG, "Invalid ESP-NOW channel: %u", config->channel);
        return ESP_ERR_INVALID_ARG;
    }

    if (mac_is_all_zero(config->peer_mac)) {
        ESP_LOGE(TAG, "ESP-NOW peer MAC is all zeros; set APP_ESPNOW_PEER_MAC in main/app_config.h");
        return ESP_ERR_INVALID_ARG;
    }

    if (mac_is_group_address(config->peer_mac)) {
        ESP_LOGE(TAG, "ESP-NOW peer MAC must be the gateway STA unicast MAC, got " MACSTR,
                 MAC2STR(config->peer_mac));
        return ESP_ERR_INVALID_ARG;
    }

    return ESP_OK;
}

static void on_espnow_send(const esp_now_send_info_t *tx_info,
                           esp_now_send_status_t status)
{
    if (tx_info != NULL) {
        ESP_LOGI(TAG, "Send to " MACSTR " status: %s",
                 MAC2STR(tx_info->des_addr),
                 status == ESP_NOW_SEND_SUCCESS ? "success" : "fail");
    } else {
        ESP_LOGI(TAG, "Send callback status: %s",
                 status == ESP_NOW_SEND_SUCCESS ? "success" : "fail");
    }

    s_last_send_status = status;
    if (s_send_sem != NULL) {
        xSemaphoreGive(s_send_sem);
    }
}

esp_err_t espnow_transport_init(const espnow_transport_config_t *config)
{
    esp_err_t err = validate_config(config);
    if (err != ESP_OK) {
        return err;
    }

    if (s_ready) {
        return ESP_OK;
    }

    err = esp_netif_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "esp_netif_init failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "esp_event_loop_create_default failed: %s", esp_err_to_name(err));
        return err;
    }

    wifi_init_config_t wifi_cfg = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&wifi_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_init failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_set_storage failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_set_mode failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_start failed: %s", esp_err_to_name(err));
        return err;
    }

    if (config->channel != 0) {
        err = esp_wifi_set_channel(config->channel, WIFI_SECOND_CHAN_NONE);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_wifi_set_channel failed: %s", esp_err_to_name(err));
            return err;
        }
    } else {
        ESP_LOGW(TAG, "APP_ESPNOW_CHANNEL is 0, leaving Wi-Fi on its current channel");
    }

    uint8_t local_mac[ESP_NOW_ETH_ALEN] = {0};
    err = esp_wifi_get_mac(WIFI_IF_STA, local_mac);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_get_mac failed: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "Node STA MAC: " MACSTR, MAC2STR(local_mac));

    err = esp_now_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_now_init failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_now_register_send_cb(on_espnow_send);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_now_register_send_cb failed: %s", esp_err_to_name(err));
        return err;
    }

    memcpy(s_peer_mac, config->peer_mac, ESP_NOW_ETH_ALEN);
    s_channel = config->channel;
    s_send_timeout_ms = config->send_timeout_ms;

    esp_now_peer_info_t peer = {0};
    memcpy(peer.peer_addr, s_peer_mac, ESP_NOW_ETH_ALEN);
    peer.channel = s_channel;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;

    err = esp_now_add_peer(&peer);
    if (err == ESP_ERR_ESPNOW_EXIST) {
        ESP_LOGW(TAG, "ESP-NOW peer already exists: " MACSTR, MAC2STR(s_peer_mac));
    } else if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_now_add_peer failed for " MACSTR ": %s",
                 MAC2STR(s_peer_mac),
                 esp_err_to_name(err));
        return err;
    }

    s_send_sem = xSemaphoreCreateBinary();
    if (s_send_sem == NULL) {
        ESP_LOGE(TAG, "Failed to create ESP-NOW send semaphore");
        return ESP_ERR_NO_MEM;
    }

    s_ready = true;
    ESP_LOGI(TAG, "ESP-NOW initialized: peer=" MACSTR " channel=%u",
             MAC2STR(s_peer_mac),
             s_channel);
    return ESP_OK;
}

esp_err_t espnow_transport_send(const void *payload, size_t payload_len, bool *send_confirmed)
{
    if (send_confirmed != NULL) {
        *send_confirmed = false;
    }

    if (!s_ready || payload == NULL || payload_len == 0 || payload_len > ESP_NOW_MAX_DATA_LEN) {
        return ESP_ERR_INVALID_ARG;
    }

    s_last_send_status = ESP_NOW_SEND_FAIL;
    esp_err_t err = esp_now_send(s_peer_mac, payload, payload_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_now_send failed: %s", esp_err_to_name(err));
        return err;
    }

    if (xSemaphoreTake(s_send_sem, pdMS_TO_TICKS(s_send_timeout_ms)) != pdTRUE) {
        ESP_LOGE(TAG, "Timed out waiting for ESP-NOW send callback");
        return ESP_ERR_TIMEOUT;
    }

    if (s_last_send_status != ESP_NOW_SEND_SUCCESS) {
        ESP_LOGE(TAG, "ESP-NOW send callback reported failure");
        return ESP_FAIL;
    }

    if (send_confirmed != NULL) {
        *send_confirmed = true;
    }

    return ESP_OK;
}

bool espnow_transport_is_ready(void)
{
    return s_ready;
}
