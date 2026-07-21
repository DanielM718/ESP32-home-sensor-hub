#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "espnow_transport.h"
#include "sen66.h"
#include "sen66_packet.h"

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

#define DEFAULT_MEASUREMENT_INTERVAL_MS 5000U

static const char *TAG = "SEN66_node";
static const uint8_t gateway_mac[ESP_NOW_ETH_ALEN] = APP_ESPNOW_PEER_MAC;
static uint32_t sequence_number = 0;

typedef struct {
    sen66_t sen66;
    bool espnow_ready;
    uint32_t base_status_flags;
} app_context_t;

static app_context_t app_context;

static esp_err_t init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }

    return err;
}

static uint32_t measurement_interval_ms(void)
{
    if (APP_MEASUREMENT_INTERVAL_MS == 0) {
        ESP_LOGW(TAG,
                 "APP_MEASUREMENT_INTERVAL_MS is 0; using default %u ms",
                 DEFAULT_MEASUREMENT_INTERVAL_MS);
        return DEFAULT_MEASUREMENT_INTERVAL_MS;
    }

    return APP_MEASUREMENT_INTERVAL_MS;
}

static esp_err_t wait_for_first_sample(const sen66_t *sen66)
{
    const uint32_t poll_delay_ms = 100;
    const uint32_t timeout_ms = 3000;

    for (uint32_t elapsed_ms = 0; elapsed_ms < timeout_ms; elapsed_ms += poll_delay_ms) {
        bool ready = false;
        esp_err_t err = sen66_get_data_ready(sen66, &ready);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Data-ready check failed: %s", esp_err_to_name(err));
        } else if (ready) {
            ESP_LOGI(TAG, "SEN66 first measurement is ready");
            return ESP_OK;
        }

        vTaskDelay(pdMS_TO_TICKS(poll_delay_ms));
    }

    ESP_LOGW(TAG, "Timed out waiting for initial SEN66 data-ready flag");
    return ESP_ERR_TIMEOUT;
}

static bool measurement_field_is_valid(const sen66_measurement_t *measurement, uint32_t valid_flag)
{
    return (measurement->valid_flags & valid_flag) != 0;
}

static float float_or_zero_if_unknown(const sen66_measurement_t *measurement,
                                      uint32_t valid_flag,
                                      float value)
{
    return measurement_field_is_valid(measurement, valid_flag) ? value : 0.0f;
}

static const char *validity_label(const sen66_measurement_t *measurement, uint32_t valid_flag)
{
    return measurement_field_is_valid(measurement, valid_flag) ? "valid" : "unknown";
}

static void set_unknown_measurement(sen66_measurement_t *measurement)
{
    memset(measurement, 0, sizeof(*measurement));
    measurement->pm1_ug_m3_x10 = SEN66_UNKNOWN_UINT16;
    measurement->pm25_ug_m3_x10 = SEN66_UNKNOWN_UINT16;
    measurement->pm4_ug_m3_x10 = SEN66_UNKNOWN_UINT16;
    measurement->pm10_ug_m3_x10 = SEN66_UNKNOWN_UINT16;
    measurement->humidity_rh_x100 = (int16_t)SEN66_UNKNOWN_INT16;
    measurement->temperature_c_x200 = (int16_t)SEN66_UNKNOWN_INT16;
    measurement->voc_index_x10 = (int16_t)SEN66_UNKNOWN_INT16;
    measurement->nox_index_x10 = (int16_t)SEN66_UNKNOWN_INT16;
    measurement->co2_ppm = SEN66_UNKNOWN_UINT16;
}

static void log_measurement(const sen66_measurement_t *measurement)
{
    ESP_LOGI(TAG,
             "SEN66 PM[ug/m3]: PM1.0=%.1f(%s) PM2.5=%.1f(%s) PM4.0=%.1f(%s) PM10=%.1f(%s)",
             float_or_zero_if_unknown(measurement, SEN66_VALUE_PM1_VALID, measurement->pm1_ug_m3),
             validity_label(measurement, SEN66_VALUE_PM1_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_PM25_VALID, measurement->pm25_ug_m3),
             validity_label(measurement, SEN66_VALUE_PM25_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_PM4_VALID, measurement->pm4_ug_m3),
             validity_label(measurement, SEN66_VALUE_PM4_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_PM10_VALID, measurement->pm10_ug_m3),
             validity_label(measurement, SEN66_VALUE_PM10_VALID));
    ESP_LOGI(TAG,
             "SEN66 gas/env: CO2=%u(%s) VOC=%.1f(%s) NOx=%.1f(%s) T=%.2f(%s) RH=%.2f(%s) valid=0x%08lx",
             measurement_field_is_valid(measurement, SEN66_VALUE_CO2_VALID) ? (unsigned int)measurement->co2_ppm : 0U,
             validity_label(measurement, SEN66_VALUE_CO2_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_VOC_VALID, measurement->voc_index),
             validity_label(measurement, SEN66_VALUE_VOC_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_NOX_VALID, measurement->nox_index),
             validity_label(measurement, SEN66_VALUE_NOX_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_TEMPERATURE_VALID, measurement->temperature_c),
             validity_label(measurement, SEN66_VALUE_TEMPERATURE_VALID),
             float_or_zero_if_unknown(measurement, SEN66_VALUE_HUMIDITY_VALID, measurement->humidity_rh),
             validity_label(measurement, SEN66_VALUE_HUMIDITY_VALID),
             (unsigned long)measurement->valid_flags);
}

static void send_measurement_packet(const sen66_measurement_t *measurement,
                                    uint32_t status_flags,
                                    bool espnow_ready)
{
    sen66_packet_t packet = {0};
    sen66_packet_from_measurement(&packet,
                                  APP_NODE_ID,
                                  sequence_number++,
                                  measurement,
                                  status_flags);

    if (!espnow_ready) {
        ESP_LOGW(TAG,
                 "Skipping ESP-NOW send for sequence=%lu because transport is not ready",
                 (unsigned long)packet.sequence);
        return;
    }

    packet.status_flags |= SEN66_PACKET_STATUS_ESPNOW_SEND_ATTEMPTED;

    bool send_confirmed = false;
    esp_err_t err = espnow_transport_send(&packet, sizeof(packet), &send_confirmed);
    if (err == ESP_OK && send_confirmed) {
        uint32_t local_status_flags = packet.status_flags | SEN66_PACKET_STATUS_ESPNOW_SEND_OK;
        ESP_LOGI(TAG,
                 "ESP-NOW send confirmed: type=0x%04x node=%lu sequence=%lu bytes=%u sent_status=0x%08lx local_status=0x%08lx",
                 packet.packet_type,
                 (unsigned long)packet.node_id,
                 (unsigned long)packet.sequence,
                 (unsigned int)sizeof(packet),
                 (unsigned long)packet.status_flags,
                 (unsigned long)local_status_flags);
    } else {
        uint32_t local_status_flags = packet.status_flags | SEN66_PACKET_STATUS_ESPNOW_SEND_FAILED;
        ESP_LOGE(TAG,
                 "SEN66 packet send failed: sequence=%lu err=%s sent_status=0x%08lx local_status=0x%08lx",
                 (unsigned long)packet.sequence,
                 esp_err_to_name(err),
                 (unsigned long)packet.status_flags,
                 (unsigned long)local_status_flags);
    }
}

static void add_device_status_flags(const sen66_t *sen66, uint32_t *packet_status_flags)
{
    uint32_t device_status = 0;
    esp_err_t err = sen66_read_device_status(sen66, &device_status);
    if (err == ESP_OK) {
        *packet_status_flags |= SEN66_PACKET_STATUS_DEVICE_STATUS_READ_OK;
        if (device_status != 0) {
            *packet_status_flags |= SEN66_PACKET_STATUS_DEVICE_STATUS_NONZERO;
        }
        ESP_LOGI(TAG, "SEN66 device status: 0x%08lx", (unsigned long)device_status);
    } else {
        ESP_LOGW(TAG, "SEN66 device status read failed: %s", esp_err_to_name(err));
    }
}

static void send_unknown_measurement_packet(app_context_t *context, uint32_t status_flags)
{
    sen66_measurement_t measurement = {0};
    set_unknown_measurement(&measurement);
    send_measurement_packet(&measurement, status_flags, context->espnow_ready);
}

static void read_and_send_sample(app_context_t *context)
{
    uint32_t packet_status_flags = context->base_status_flags;
    bool ready = false;

    esp_err_t err = sen66_get_data_ready(&context->sen66, &ready);
    if (err != ESP_OK) {
        packet_status_flags |= SEN66_PACKET_STATUS_READ_ERROR;
        if (err == ESP_ERR_INVALID_CRC) {
            packet_status_flags |= SEN66_PACKET_STATUS_CRC_ERROR;
        }
        ESP_LOGE(TAG, "SEN66 data-ready check failed: %s", esp_err_to_name(err));
        send_unknown_measurement_packet(context, packet_status_flags);
        return;
    }

    if (!ready) {
        ESP_LOGW(TAG, "SEN66 data not ready; skipping this interval");
        return;
    }

    packet_status_flags |= SEN66_PACKET_STATUS_DATA_READY;

    sen66_measurement_t measurement = {0};
    err = sen66_read_measured_values(&context->sen66, &measurement);
    if (err != ESP_OK) {
        packet_status_flags |= SEN66_PACKET_STATUS_READ_ERROR;
        if (err == ESP_ERR_INVALID_CRC) {
            packet_status_flags |= SEN66_PACKET_STATUS_CRC_ERROR;
        }
        ESP_LOGE(TAG, "SEN66 measured-value read failed: %s", esp_err_to_name(err));
        send_unknown_measurement_packet(context, packet_status_flags);
        return;
    }

    packet_status_flags |= SEN66_PACKET_STATUS_MEASUREMENT_READ_OK;
    log_measurement(&measurement);
    add_device_status_flags(&context->sen66, &packet_status_flags);
    send_measurement_packet(&measurement, packet_status_flags, context->espnow_ready);
}

static void measurement_task(void *arg)
{
    app_context_t *context = (app_context_t *)arg;
    const uint32_t interval_ms = measurement_interval_ms();
    TickType_t last_wake = xTaskGetTickCount();

    ESP_LOGI(TAG, "SEN66 measurement task started: interval=%lu ms",
             (unsigned long)interval_ms);

    while (true) {
        read_and_send_sample(context);
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(interval_ms));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "ESP32C3 SEN66 air quality node starting with %s", APP_CONFIG_SOURCE);
#if APP_CONFIG_USING_EXAMPLE
    ESP_LOGW(TAG, "Using example config; copy it to main/app_config.h before flashing");
#endif

    ESP_ERROR_CHECK(init_nvs());

    espnow_transport_config_t espnow_config = {
        .peer_mac = gateway_mac,
        .channel = APP_ESPNOW_CHANNEL,
        .send_timeout_ms = APP_ESPNOW_SEND_TIMEOUT_MS,
    };
    esp_err_t espnow_err = espnow_transport_init(&espnow_config);
    bool espnow_ready = (espnow_err == ESP_OK);
    if (!espnow_ready) {
        ESP_LOGW(TAG,
                 "ESP-NOW transport disabled until configuration is fixed: %s",
                 esp_err_to_name(espnow_err));
    }

    memset(&app_context, 0, sizeof(app_context));
    app_context.espnow_ready = espnow_ready;
    app_context.base_status_flags = espnow_ready ? SEN66_PACKET_STATUS_ESPNOW_READY : 0;

    sen66_wait_after_power_on();

    esp_err_t err = sen66_i2c_init(&app_context.sen66,
                                   APP_I2C_SDA_GPIO,
                                   APP_I2C_SCL_GPIO,
                                   APP_I2C_FREQ_HZ);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SEN66 I2C initialization failed: %s", esp_err_to_name(err));
        return;
    }
    app_context.base_status_flags |= SEN66_PACKET_STATUS_I2C_READY;

    err = sen66_probe(&app_context.sen66);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SEN66 probe failed; check power, wiring, pull-ups, and I2C pins");
        sen66_deinit(&app_context.sen66);
        return;
    }

    err = sen66_start_continuous_measurement(&app_context.sen66);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start SEN66 continuous measurement: %s", esp_err_to_name(err));
        sen66_deinit(&app_context.sen66);
        return;
    }
    app_context.base_status_flags |= SEN66_PACKET_STATUS_MEASUREMENT_STARTED;

    err = wait_for_first_sample(&app_context.sen66);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Measurement task will continue polling for SEN66 data");
    }

    BaseType_t task_created = xTaskCreate(measurement_task,
                                          "sen66_measure",
                                          APP_MEASUREMENT_TASK_STACK_SIZE,
                                          &app_context,
                                          APP_MEASUREMENT_TASK_PRIORITY,
                                          NULL);
    if (task_created != pdPASS) {
        ESP_LOGE(TAG, "Failed to create measurement task; running inline loop");
        measurement_task(&app_context);
    }

    ESP_LOGI(TAG, "SEN66 continuous measurement task created");
}
