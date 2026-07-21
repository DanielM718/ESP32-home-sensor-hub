#include "mqtt_transport.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "mqtt_client.h"

#define MQTT_BROKER_URI_MAX_LEN 160

static const char *TAG = "SEN66_network";

static esp_mqtt_client_handle_t s_mqtt_client;
static bool s_started;
static volatile bool s_wifi_connected;
static volatile bool s_mqtt_connected;
static uint32_t s_wifi_reconnect_count;
static int s_mqtt_qos;
static char s_broker_uri[MQTT_BROKER_URI_MAX_LEN];

static bool config_string_is_set(const char *value)
{
    return value != NULL && value[0] != '\0';
}

static esp_err_t validate_config(const mqtt_transport_config_t *config)
{
    if (config == NULL ||
        !config_string_is_set(config->wifi_ssid) ||
        !config_string_is_set(config->wifi_password) ||
        !config_string_is_set(config->broker_host) ||
        !config_string_is_set(config->client_id) ||
        config->broker_port == 0 ||
        config->qos < 0 || config->qos > 2 ||
        config->network_timeout_ms == 0 ||
        config->reconnect_timeout_ms == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    if (strlen(config->wifi_ssid) > sizeof(((wifi_config_t *)0)->sta.ssid) ||
        strlen(config->wifi_password) > sizeof(((wifi_config_t *)0)->sta.password)) {
        ESP_LOGE(TAG, "Wi-Fi SSID or password exceeds the ESP-IDF field limit");
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    (void)arg;

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "Wi-Fi station started; connecting");
        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Initial Wi-Fi connect request failed: %s", esp_err_to_name(err));
        }
        return;
    }

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = event_data;
        s_wifi_connected = false;
        s_wifi_reconnect_count++;
        ESP_LOGW(TAG, "Wi-Fi disconnected: reason=%u reconnect_attempt=%" PRIu32,
                 event != NULL ? (unsigned int)event->reason : 0U,
                 s_wifi_reconnect_count);

        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Wi-Fi reconnect request failed: %s", esp_err_to_name(err));
        }
        return;
    }

    if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = event_data;
        s_wifi_connected = true;
        s_wifi_reconnect_count = 0;
        if (event != NULL) {
            ESP_LOGI(TAG, "Wi-Fi connected: " IPSTR, IP2STR(&event->ip_info.ip));
        } else {
            ESP_LOGI(TAG, "Wi-Fi connected");
        }
    }
}

static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    (void)handler_args;
    (void)base;

    const esp_mqtt_event_handle_t event = event_data;

    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        s_mqtt_connected = true;
        ESP_LOGI(TAG, "MQTT connected");
        break;

    case MQTT_EVENT_DISCONNECTED:
        s_mqtt_connected = false;
        ESP_LOGW(TAG, "MQTT disconnected; automatic reconnect remains enabled");
        break;

    case MQTT_EVENT_PUBLISHED:
        if (event != NULL) {
            ESP_LOGD(TAG, "MQTT publish acknowledged: msg_id=%d", event->msg_id);
        }
        break;

    case MQTT_EVENT_ERROR:
        s_mqtt_connected = false;
        ESP_LOGE(TAG, "MQTT transport error");
        if (event != NULL && event->error_handle != NULL) {
            ESP_LOGE(TAG,
                     "MQTT error type=%d connect_return_code=%d sock_errno=%d",
                     event->error_handle->error_type,
                     event->error_handle->connect_return_code,
                     event->error_handle->esp_transport_sock_errno);
        }
        break;

    default:
        ESP_LOGD(TAG, "MQTT event id=%" PRIi32, event_id);
        break;
    }
}

esp_err_t mqtt_transport_start(const mqtt_transport_config_t *config)
{
    esp_err_t err = validate_config(config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Network/MQTT configuration is invalid: %s", esp_err_to_name(err));
        return err;
    }

    if (s_started) {
        ESP_LOGW(TAG, "Network/MQTT transport already started");
        return ESP_OK;
    }

    int written = snprintf(s_broker_uri, sizeof(s_broker_uri), "mqtt://%s:%u",
                           config->broker_host, (unsigned int)config->broker_port);
    if (written < 0 || written >= (int)sizeof(s_broker_uri)) {
        ESP_LOGE(TAG, "MQTT broker URI exceeds %u bytes",
                 (unsigned int)sizeof(s_broker_uri));
        return ESP_ERR_INVALID_SIZE;
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

    if (esp_netif_create_default_wifi_sta() == NULL) {
        ESP_LOGE(TAG, "Failed to create the default Wi-Fi station interface");
        return ESP_FAIL;
    }

    wifi_init_config_t wifi_init_config = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&wifi_init_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_init failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_set_storage failed: %s", esp_err_to_name(err));
        return err;
    }

    wifi_config_t wifi_config = {0};
    memcpy(wifi_config.sta.ssid, config->wifi_ssid, strlen(config->wifi_ssid));
    memcpy(wifi_config.sta.password, config->wifi_password, strlen(config->wifi_password));
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    wifi_config.sta.pmf_cfg.capable = true;
    wifi_config.sta.pmf_cfg.required = false;

    err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                     wifi_event_handler, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi event handler registration failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                     wifi_event_handler, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "IP event handler registration failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_set_mode failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_set_config failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_start failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_wifi_set_ps(WIFI_PS_NONE);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Could not disable Wi-Fi power save: %s", esp_err_to_name(err));
    }

    esp_mqtt_client_config_t mqtt_config = {
        .broker.address.uri = s_broker_uri,
        .credentials.client_id = config->client_id,
        .credentials.username = config_string_is_set(config->username)
                                    ? config->username : NULL,
        .credentials.authentication.password = config_string_is_set(config->password)
                                                   ? config->password : NULL,
        .network.timeout_ms = (int)config->network_timeout_ms,
        .network.reconnect_timeout_ms = (int)config->reconnect_timeout_ms,
        .network.disable_auto_reconnect = false,
    };

    s_mqtt_client = esp_mqtt_client_init(&mqtt_config);
    if (s_mqtt_client == NULL) {
        ESP_LOGE(TAG, "Failed to initialize MQTT client");
        return ESP_FAIL;
    }

    err = esp_mqtt_client_register_event(s_mqtt_client, ESP_EVENT_ANY_ID,
                                         mqtt_event_handler, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "MQTT event handler registration failed: %s", esp_err_to_name(err));
        esp_mqtt_client_destroy(s_mqtt_client);
        s_mqtt_client = NULL;
        return err;
    }

    err = esp_mqtt_client_start(s_mqtt_client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "MQTT client start failed: %s", esp_err_to_name(err));
        esp_mqtt_client_destroy(s_mqtt_client);
        s_mqtt_client = NULL;
        return err;
    }

    s_mqtt_qos = config->qos;
    s_started = true;
    ESP_LOGI(TAG, "Network/MQTT transport started: broker=%s qos=%d",
             s_broker_uri, s_mqtt_qos);
    return ESP_OK;
}

bool mqtt_transport_wifi_is_connected(void)
{
    return s_wifi_connected;
}

bool mqtt_transport_is_connected(void)
{
    return s_mqtt_connected;
}

esp_err_t mqtt_transport_publish(const char *topic, const char *payload)
{
    if (topic == NULL || payload == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    if (s_mqtt_client == NULL || !s_mqtt_connected) {
        return ESP_ERR_INVALID_STATE;
    }

    int message_id = esp_mqtt_client_publish(s_mqtt_client, topic, payload,
                                              0, s_mqtt_qos, 0);
    if (message_id < 0) {
        ESP_LOGE(TAG, "MQTT publish failed: topic=%s", topic);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "MQTT publish queued: topic=%s msg_id=%d", topic, message_id);
    return ESP_OK;
}
