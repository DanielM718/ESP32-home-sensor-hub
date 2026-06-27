#include "mqtt_publisher.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "mqtt_client.h"

#include "wifi_cred.h"

static const char *TAG = "mqtt_publisher";

#define MQTT_BROKER_URI_MAX_LEN 128

#ifndef MQTT_NETWORK_TIMEOUT_MS
#define MQTT_NETWORK_TIMEOUT_MS 5000
#endif

#ifndef MQTT_RECONNECT_TIMEOUT_MS
#define MQTT_RECONNECT_TIMEOUT_MS 5000
#endif

static esp_mqtt_client_handle_t s_mqtt_client;
static bool s_mqtt_connected;
static char s_broker_uri[MQTT_BROKER_URI_MAX_LEN];

static bool config_string_is_set(const char *value)
{
    return value != NULL && value[0] != '\0';
}

static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    (void)handler_args;
    (void)base;

    esp_mqtt_event_handle_t event = event_data;
    if (event == NULL) {
        ESP_LOGW(TAG, "MQTT event missing event data");
        return;
    }

    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        s_mqtt_connected = true;
        ESP_LOGI(TAG, "MQTT connected");
        break;

    case MQTT_EVENT_DISCONNECTED:
        s_mqtt_connected = false;
        ESP_LOGW(TAG, "MQTT disconnected");
        break;

    case MQTT_EVENT_PUBLISHED:
        ESP_LOGI(TAG, "MQTT published, msg_id=%d", event->msg_id);
        break;

    case MQTT_EVENT_ERROR:
        s_mqtt_connected = false;
        ESP_LOGE(TAG, "MQTT error");
        if (event->error_handle != NULL) {
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

esp_err_t mqtt_publisher_start(void)
{
    if (s_mqtt_client != NULL) {
        ESP_LOGW(TAG, "MQTT publisher already started");
        return ESP_OK;
    }

    int written = snprintf(s_broker_uri, sizeof(s_broker_uri), "mqtt://%s:%d",
                           MQTT_BROKER_HOST, MQTT_BROKER_PORT);
    if (written < 0 || written >= (int)sizeof(s_broker_uri)) {
        ESP_LOGE(TAG, "MQTT broker URI is too long");
        return ESP_ERR_INVALID_ARG;
    }

    esp_mqtt_client_config_t mqtt_config = {
        .broker.address.uri = s_broker_uri,
        .credentials.client_id = MQTT_CLIENT_ID,
        .credentials.username = config_string_is_set(MQTT_USERNAME) ? MQTT_USERNAME : NULL,
        .credentials.authentication.password =
            config_string_is_set(MQTT_PASSWORD) ? MQTT_PASSWORD : NULL,
        .network.timeout_ms = MQTT_NETWORK_TIMEOUT_MS,
        .network.reconnect_timeout_ms = MQTT_RECONNECT_TIMEOUT_MS,
    };

    ESP_LOGI(TAG, "Starting MQTT client: %s timeout=%dms reconnect=%dms",
             s_broker_uri, MQTT_NETWORK_TIMEOUT_MS, MQTT_RECONNECT_TIMEOUT_MS);
    s_mqtt_client = esp_mqtt_client_init(&mqtt_config);
    if (s_mqtt_client == NULL) {
        ESP_LOGE(TAG, "Failed to initialize MQTT client");
        return ESP_FAIL;
    }

    esp_err_t err = esp_mqtt_client_register_event(s_mqtt_client, ESP_EVENT_ANY_ID,
                                                   mqtt_event_handler, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register MQTT event handler: %s", esp_err_to_name(err));
        esp_mqtt_client_destroy(s_mqtt_client);
        s_mqtt_client = NULL;
        return err;
    }

    err = esp_mqtt_client_start(s_mqtt_client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start MQTT client: %s", esp_err_to_name(err));
        esp_mqtt_client_destroy(s_mqtt_client);
        s_mqtt_client = NULL;
        return err;
    }

    return ESP_OK;
}

bool mqtt_publisher_is_connected(void)
{
    return s_mqtt_connected;
}

esp_err_t mqtt_publisher_publish(const char *topic, const char *payload, int qos)
{
    if (topic == NULL || payload == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    if (qos < 0 || qos > 2) {
        ESP_LOGE(TAG, "Invalid MQTT QoS: %d", qos);
        return ESP_ERR_INVALID_ARG;
    }

    if (s_mqtt_client == NULL) {
        ESP_LOGW(TAG, "MQTT publish skipped, client is not initialized");
        return ESP_ERR_INVALID_STATE;
    }

    if (!s_mqtt_connected) {
        ESP_LOGW(TAG, "MQTT publish skipped, client is disconnected");
        return ESP_ERR_INVALID_STATE;
    }

    int msg_id = esp_mqtt_client_publish(s_mqtt_client, topic, payload, 0, qos, 0);
    if (msg_id < 0) {
        ESP_LOGE(TAG, "MQTT publish failed: topic=%s", topic);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "MQTT publish queued: topic=%s msg_id=%d", topic, msg_id);
    return ESP_OK;
}
