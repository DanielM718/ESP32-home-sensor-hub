#include <ctype.h>
#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "mqtt_transport.h"
#include "sen66.h"

#if __has_include("app_config.h")
#include "app_config.h"
#define APP_CONFIG_SOURCE "main/app_config.h"
#define APP_CONFIG_USING_EXAMPLE 0
#else
#include "app_config.example.h"
#define APP_CONFIG_SOURCE "main/app_config.example.h"
#define APP_CONFIG_USING_EXAMPLE 1
#endif

#ifndef APP_MEASUREMENT_TASK_STACK_SIZE
#define APP_MEASUREMENT_TASK_STACK_SIZE 4096U
#endif

#ifndef APP_MEASUREMENT_TASK_PRIORITY
#define APP_MEASUREMENT_TASK_PRIORITY 5U
#endif

#ifndef APP_SENSOR_INIT_RETRY_MS
#define APP_SENSOR_INIT_RETRY_MS 5000U
#endif

#ifndef APP_SENSOR_REINIT_AFTER_ERRORS
#define APP_SENSOR_REINIT_AFTER_ERRORS 3U
#endif

#define DEFAULT_MEASUREMENT_INTERVAL_MS 5000U
#define MINIMUM_MEASUREMENT_INTERVAL_MS 1000U
#define MQTT_TOPIC_MAX_LEN 96U
#define MQTT_PAYLOAD_MAX_LEN 512U

#define SEN66_REQUIRED_VALID_FLAGS (SEN66_VALUE_PM1_VALID |              \
                                    SEN66_VALUE_PM25_VALID |             \
                                    SEN66_VALUE_PM4_VALID |              \
                                    SEN66_VALUE_PM10_VALID |             \
                                    SEN66_VALUE_HUMIDITY_VALID |         \
                                    SEN66_VALUE_TEMPERATURE_VALID |      \
                                    SEN66_VALUE_VOC_VALID |              \
                                    SEN66_VALUE_NOX_VALID |              \
                                    SEN66_VALUE_CO2_VALID)

// These flags are diagnostic metadata. The server stores the nine required
// measurements and safely ignores additional JSON fields.
#define APP_STATUS_I2C_READY (1UL << 0)
#define APP_STATUS_MEASUREMENT_STARTED (1UL << 1)
#define APP_STATUS_DATA_READY (1UL << 2)
#define APP_STATUS_MEASUREMENT_READ_OK (1UL << 3)
#define APP_STATUS_DEVICE_STATUS_READ_OK (1UL << 4)
#define APP_STATUS_DEVICE_STATUS_NONZERO (1UL << 5)
#define APP_STATUS_WIFI_CONNECTED (1UL << 6)
#define APP_STATUS_MQTT_CONNECTED (1UL << 7)
#define APP_STATUS_MQTT_PUBLISH_ATTEMPTED (1UL << 8)

static const char *TAG = "SEN66_node";

typedef struct {
    sen66_t sensor;
    uint32_t base_status_flags;
    uint32_t consecutive_read_errors;
} app_context_t;

static app_context_t app_context;
static char mqtt_topic[MQTT_TOPIC_MAX_LEN];
static uint32_t sequence_number;

static bool string_is_set(const char *value)
{
    return value != NULL && value[0] != '\0';
}

static esp_err_t init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS requires reinitialization; erasing the NVS partition");
        err = nvs_flash_erase();
        if (err != ESP_OK) {
            return err;
        }
        err = nvs_flash_init();
    }

    return err;
}

static uint32_t measurement_interval_ms(void)
{
    if (APP_MEASUREMENT_INTERVAL_MS < MINIMUM_MEASUREMENT_INTERVAL_MS) {
        ESP_LOGW(TAG,
                 "APP_MEASUREMENT_INTERVAL_MS=%u is below the SEN66 sampling interval; using %u ms",
                 (unsigned int)APP_MEASUREMENT_INTERVAL_MS,
                 DEFAULT_MEASUREMENT_INTERVAL_MS);
        return DEFAULT_MEASUREMENT_INTERVAL_MS;
    }

    return APP_MEASUREMENT_INTERVAL_MS;
}

static bool location_is_valid(const char *location)
{
    if (!string_is_set(location)) {
        return false;
    }

    const size_t length = strlen(location);
    if (length > 64U) {
        return false;
    }

    for (size_t index = 0; index < length; index++) {
        const unsigned char character = (unsigned char)location[index];
        if (!isalnum(character) && character != '_' && character != '-') {
            return false;
        }
    }

    return true;
}

static esp_err_t validate_network_config(void)
{
#if APP_CONFIG_USING_EXAMPLE
    ESP_LOGE(TAG, "Network publishing is disabled while the example configuration is in use");
    return ESP_ERR_INVALID_STATE;
#endif

    if (!location_is_valid(APP_LOCATION)) {
        ESP_LOGE(TAG,
                 "APP_LOCATION must contain 1-64 ASCII letters, digits, underscores, or hyphens");
        return ESP_ERR_INVALID_ARG;
    }

    if (!string_is_set(APP_WIFI_SSID) || !string_is_set(APP_WIFI_PASSWORD) ||
        !string_is_set(APP_MQTT_BROKER_HOST) || !string_is_set(APP_MQTT_CLIENT_ID) ||
        APP_MQTT_BROKER_PORT == 0 || APP_MQTT_QOS < 0 || APP_MQTT_QOS > 2) {
        ESP_LOGE(TAG, "Wi-Fi/MQTT configuration is incomplete or invalid");
        return ESP_ERR_INVALID_ARG;
    }

    int written = snprintf(mqtt_topic, sizeof(mqtt_topic), "home/air/%s", APP_LOCATION);
    if (written < 0 || written >= (int)sizeof(mqtt_topic)) {
        ESP_LOGE(TAG, "MQTT topic exceeds %u bytes", (unsigned int)sizeof(mqtt_topic));
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}

static void start_network_transport(void)
{
    esp_err_t err = validate_network_config();
    if (err != ESP_OK) {
        ESP_LOGW(TAG,
                 "Wi-Fi/MQTT transport not started; sensor readings will only be logged");
        return;
    }

    const mqtt_transport_config_t config = {
        .wifi_ssid = APP_WIFI_SSID,
        .wifi_password = APP_WIFI_PASSWORD,
        .broker_host = APP_MQTT_BROKER_HOST,
        .broker_port = APP_MQTT_BROKER_PORT,
        .client_id = APP_MQTT_CLIENT_ID,
        .username = APP_MQTT_USERNAME,
        .password = APP_MQTT_PASSWORD,
        .qos = APP_MQTT_QOS,
        .network_timeout_ms = APP_MQTT_NETWORK_TIMEOUT_MS,
        .reconnect_timeout_ms = APP_MQTT_RECONNECT_TIMEOUT_MS,
    };

    err = mqtt_transport_start(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi/MQTT transport start failed: %s", esp_err_to_name(err));
        return;
    }

    ESP_LOGI(TAG, "SEN66 readings will publish to %s", mqtt_topic);
}

static esp_err_t wait_for_first_sample(const sen66_t *sensor)
{
    const uint32_t poll_delay_ms = 100U;
    const uint32_t timeout_ms = 3000U;

    for (uint32_t elapsed_ms = 0; elapsed_ms < timeout_ms; elapsed_ms += poll_delay_ms) {
        bool ready = false;
        esp_err_t err = sen66_get_data_ready(sensor, &ready);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Initial data-ready check failed: %s", esp_err_to_name(err));
        } else if (ready) {
            ESP_LOGI(TAG, "SEN66 first measurement is ready");
            return ESP_OK;
        }

        vTaskDelay(pdMS_TO_TICKS(poll_delay_ms));
    }

    return ESP_ERR_TIMEOUT;
}

static esp_err_t initialize_sensor(app_context_t *context)
{
    memset(&context->sensor, 0, sizeof(context->sensor));
    context->base_status_flags = 0;

    esp_err_t err = sen66_i2c_init(&context->sensor,
                                   APP_I2C_SDA_GPIO,
                                   APP_I2C_SCL_GPIO,
                                   APP_I2C_FREQ_HZ);
    if (err != ESP_OK) {
        return err;
    }
    context->base_status_flags |= APP_STATUS_I2C_READY;

    err = sen66_probe(&context->sensor);
    if (err != ESP_OK) {
        sen66_deinit(&context->sensor);
        context->base_status_flags = 0;
        return err;
    }

    err = sen66_start_continuous_measurement(&context->sensor);
    if (err != ESP_OK) {
        sen66_deinit(&context->sensor);
        context->base_status_flags = 0;
        return err;
    }
    context->base_status_flags |= APP_STATUS_MEASUREMENT_STARTED;

    err = wait_for_first_sample(&context->sensor);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Initial sample is not ready yet; normal polling will continue");
    }

    context->consecutive_read_errors = 0;
    return ESP_OK;
}

static void initialize_sensor_with_retry(app_context_t *context)
{
    while (true) {
        esp_err_t err = initialize_sensor(context);
        if (err == ESP_OK) {
            return;
        }

        ESP_LOGE(TAG,
                 "SEN66 initialization failed: %s; check power, wiring, and GPIOs; retrying in %u ms",
                 esp_err_to_name(err),
                 (unsigned int)APP_SENSOR_INIT_RETRY_MS);
        vTaskDelay(pdMS_TO_TICKS(APP_SENSOR_INIT_RETRY_MS));
    }
}

static void recover_sensor(app_context_t *context)
{
    ESP_LOGW(TAG, "Reinitializing SEN66 after repeated I2C/read failures");

    esp_err_t err = sen66_stop_measurement(&context->sensor);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Could not stop measurement during recovery: %s", esp_err_to_name(err));
    }

    err = sen66_deinit(&context->sensor);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Could not fully release I2C during recovery: %s", esp_err_to_name(err));
    }

    vTaskDelay(pdMS_TO_TICKS(APP_SENSOR_INIT_RETRY_MS));
    initialize_sensor_with_retry(context);
}

static void record_read_error(app_context_t *context, esp_err_t err, const char *operation)
{
    context->consecutive_read_errors++;
    ESP_LOGE(TAG,
             "%s failed: %s (consecutive_errors=%" PRIu32 ")",
             operation,
             esp_err_to_name(err),
             context->consecutive_read_errors);

    if (context->consecutive_read_errors >= APP_SENSOR_REINIT_AFTER_ERRORS) {
        recover_sensor(context);
    }
}

static bool measurement_field_is_valid(const sen66_measurement_t *measurement,
                                       uint32_t valid_flag)
{
    return (measurement->valid_flags & valid_flag) != 0;
}

static float float_or_zero_if_unknown(const sen66_measurement_t *measurement,
                                      uint32_t valid_flag,
                                      float value)
{
    return measurement_field_is_valid(measurement, valid_flag) ? value : 0.0f;
}

static const char *validity_label(const sen66_measurement_t *measurement,
                                  uint32_t valid_flag)
{
    return measurement_field_is_valid(measurement, valid_flag) ? "valid" : "unknown";
}

static void log_measurement(const sen66_measurement_t *measurement)
{
    ESP_LOGI(TAG,
             "PM ug/m3: PM1=%.1f(%s) PM2.5=%.1f(%s) PM4=%.1f(%s) PM10=%.1f(%s)",
             float_or_zero_if_unknown(measurement, SEN66_VALUE_PM1_VALID, measurement->pm1_ug_m3),
             validity_label(measurement, SEN66_VALUE_PM1_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_PM25_VALID, measurement->pm25_ug_m3),
             validity_label(measurement, SEN66_VALUE_PM25_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_PM4_VALID, measurement->pm4_ug_m3),
             validity_label(measurement, SEN66_VALUE_PM4_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_PM10_VALID, measurement->pm10_ug_m3),
             validity_label(measurement, SEN66_VALUE_PM10_VALID));
    ESP_LOGI(TAG,
             "Gas/env: CO2=%u(%s) VOC=%.1f(%s) NOx=%.1f(%s) T=%.2f C(%s) RH=%.2f%%(%s) valid=0x%03" PRIx32,
             measurement_field_is_valid(measurement, SEN66_VALUE_CO2_VALID)
                 ? (unsigned int)measurement->co2_ppm : 0U,
             validity_label(measurement, SEN66_VALUE_CO2_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_VOC_VALID, measurement->voc_index),
             validity_label(measurement, SEN66_VALUE_VOC_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_NOX_VALID, measurement->nox_index),
             validity_label(measurement, SEN66_VALUE_NOX_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_TEMPERATURE_VALID, measurement->temperature_c),
             validity_label(measurement, SEN66_VALUE_TEMPERATURE_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_HUMIDITY_VALID, measurement->humidity_rh),
             validity_label(measurement, SEN66_VALUE_HUMIDITY_VALID),
             measurement->valid_flags);
}

static bool measurement_is_server_compatible(const sen66_measurement_t *measurement)
{
    if ((measurement->valid_flags & SEN66_REQUIRED_VALID_FLAGS) != SEN66_REQUIRED_VALID_FLAGS) {
        ESP_LOGW(TAG,
                 "Skipping incomplete SEN66 sample: valid=0x%03" PRIx32 " required=0x%03" PRIx32,
                 measurement->valid_flags,
                 (uint32_t)SEN66_REQUIRED_VALID_FLAGS);
        return false;
    }

    const bool ranges_are_valid =
        isfinite(measurement->pm1_ug_m3) && measurement->pm1_ug_m3 >= 0.0f &&
        isfinite(measurement->pm25_ug_m3) && measurement->pm25_ug_m3 >= 0.0f &&
        isfinite(measurement->pm4_ug_m3) && measurement->pm4_ug_m3 >= 0.0f &&
        isfinite(measurement->pm10_ug_m3) && measurement->pm10_ug_m3 >= 0.0f &&
        isfinite(measurement->voc_index) && measurement->voc_index >= 0.0f &&
        measurement->voc_index <= 500.0f &&
        isfinite(measurement->nox_index) && measurement->nox_index >= 0.0f &&
        measurement->nox_index <= 500.0f &&
        isfinite(measurement->temperature_c) && measurement->temperature_c >= -80.0f &&
        measurement->temperature_c <= 125.0f &&
        isfinite(measurement->humidity_rh) && measurement->humidity_rh >= 0.0f &&
        measurement->humidity_rh <= 100.0f;

    if (!ranges_are_valid) {
        ESP_LOGW(TAG, "Skipping SEN66 sample containing an out-of-range value");
    }
    return ranges_are_valid;
}

static uint32_t add_device_status(const sen66_t *sensor, uint32_t status_flags)
{
    uint32_t device_status = 0;
    esp_err_t err = sen66_read_device_status(sensor, &device_status);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SEN66 device-status read failed: %s", esp_err_to_name(err));
        return status_flags;
    }

    status_flags |= APP_STATUS_DEVICE_STATUS_READ_OK;
    if (device_status != 0) {
        status_flags |= APP_STATUS_DEVICE_STATUS_NONZERO;
        ESP_LOGW(TAG, "SEN66 reports device status 0x%08" PRIx32, device_status);
    } else {
        ESP_LOGD(TAG, "SEN66 device status is clear");
    }

    return status_flags;
}

static void publish_measurement(const sen66_measurement_t *measurement,
                                uint32_t status_flags)
{
    if (mqtt_transport_wifi_is_connected()) {
        status_flags |= APP_STATUS_WIFI_CONNECTED;
    }
    if (mqtt_transport_is_connected()) {
        status_flags |= APP_STATUS_MQTT_CONNECTED;
    } else {
        ESP_LOGW(TAG, "Skipping SEN66 publish because MQTT is not connected");
        return;
    }

    const uint32_t sequence = sequence_number++;
    status_flags |= APP_STATUS_MQTT_PUBLISH_ATTEMPTED;
    const long voc_index = lroundf(measurement->voc_index);
    const long nox_index = lroundf(measurement->nox_index);

    char payload[MQTT_PAYLOAD_MAX_LEN];
    int written = snprintf(
        payload,
        sizeof(payload),
        "{\"co2\":%u,\"pm1\":%.1f,\"pm25\":%.1f,\"pm4\":%.1f,\"pm10\":%.1f,"
        "\"voc_index\":%ld,\"nox_index\":%ld,\"temperature_c\":%.2f,\"humidity\":%.2f,"
        "\"packet_type\":\"sen66\",\"schema_version\":1,\"firmware_version\":\"%s\","
        "\"node_id\":%u,\"sequence\":%" PRIu32 ",\"status_flags\":%" PRIu32 "}",
        (unsigned int)measurement->co2_ppm,
        measurement->pm1_ug_m3,
        measurement->pm25_ug_m3,
        measurement->pm4_ug_m3,
        measurement->pm10_ug_m3,
        voc_index,
        nox_index,
        measurement->temperature_c,
        measurement->humidity_rh,
        APP_FIRMWARE_VERSION,
        (unsigned int)APP_NODE_ID,
        sequence,
        status_flags);

    if (written < 0 || written >= (int)sizeof(payload)) {
        ESP_LOGE(TAG, "SEN66 MQTT payload formatting failed or exceeded %u bytes",
                 (unsigned int)sizeof(payload));
        return;
    }

    esp_err_t err = mqtt_transport_publish(mqtt_topic, payload);
    if (err != ESP_OK) {
        ESP_LOGE(TAG,
                 "SEN66 publish failed: sequence=%" PRIu32 " err=%s",
                 sequence,
                 esp_err_to_name(err));
        return;
    }

    ESP_LOGI(TAG,
             "SEN66 publish queued: topic=%s sequence=%" PRIu32 " status=0x%08" PRIx32,
             mqtt_topic,
             sequence,
             status_flags);
}

static void read_and_publish_sample(app_context_t *context)
{
    uint32_t status_flags = context->base_status_flags;
    bool ready = false;

    esp_err_t err = sen66_get_data_ready(&context->sensor, &ready);
    if (err != ESP_OK) {
        record_read_error(context, err, "SEN66 data-ready check");
        return;
    }

    if (!ready) {
        ESP_LOGD(TAG, "SEN66 data is not ready; skipping this interval");
        return;
    }
    status_flags |= APP_STATUS_DATA_READY;

    sen66_measurement_t measurement = {0};
    err = sen66_read_measured_values(&context->sensor, &measurement);
    if (err != ESP_OK) {
        record_read_error(context, err, "SEN66 measured-value read");
        return;
    }

    context->consecutive_read_errors = 0;
    status_flags |= APP_STATUS_MEASUREMENT_READ_OK;
    log_measurement(&measurement);
    status_flags = add_device_status(&context->sensor, status_flags);

    if (!measurement_is_server_compatible(&measurement)) {
        return;
    }

    publish_measurement(&measurement, status_flags);
}

static void measurement_task(void *arg)
{
    app_context_t *context = (app_context_t *)arg;
    const uint32_t interval_ms = measurement_interval_ms();
    TickType_t last_wake = xTaskGetTickCount();

    ESP_LOGI(TAG, "SEN66 measurement task started: interval=%" PRIu32 " ms", interval_ms);

    while (true) {
        read_and_publish_sample(context);
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(interval_ms));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG,
             "ESP32-C3 SEN66 air-quality station starting: firmware=%s config=%s",
             APP_FIRMWARE_VERSION,
             APP_CONFIG_SOURCE);
#if APP_CONFIG_USING_EXAMPLE
    ESP_LOGW(TAG, "Copy main/app_config.example.h to ignored main/app_config.h before flashing");
#endif

    ESP_ERROR_CHECK(init_nvs());
    start_network_transport();

    memset(&app_context, 0, sizeof(app_context));
    sen66_wait_after_power_on();
    initialize_sensor_with_retry(&app_context);

    BaseType_t task_created = xTaskCreate(measurement_task,
                                          "sen66_measure",
                                          APP_MEASUREMENT_TASK_STACK_SIZE,
                                          &app_context,
                                          APP_MEASUREMENT_TASK_PRIORITY,
                                          NULL);
    if (task_created != pdPASS) {
        ESP_LOGE(TAG, "Failed to create measurement task; running it inline");
        measurement_task(&app_context);
    }

    ESP_LOGI(TAG, "SEN66 continuous measurement task created");
}
