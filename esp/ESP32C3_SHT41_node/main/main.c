#include <stdio.h>
#include <stdint.h>
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
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"

#define NODE_ID 1//4
#define ESPNOW_CHANNEL 6
#define SLEEP_INTERVAL_US (15ULL * 60ULL * 1000000ULL) // 15 min
//#define SLEEP_INTERVAL_US (10ULL * 1000000ULL) // 10 seconds for testing
#define ESPNOW_SEND_TIMEOUT_MS 500

// Seeed XIAO ESP32-C3 common I2C pins are D4/SDA = GPIO6 and D5/SCL = GPIO7.
// Verify these match your wiring before flashing.
#define I2C_SDA_GPIO GPIO_NUM_6
#define I2C_SCL_GPIO GPIO_NUM_7
#define I2C_FREQ_HZ 100000
#define I2C_XFER_TIMEOUT_MS 100

#define BATTERY_ADC_GPIO GPIO_NUM_2
#define BATTERY_ADC_UNIT ADC_UNIT_1
#define BATTERY_ADC_CHANNEL ADC_CHANNEL_2
#define BATTERY_ADC_ATTEN ADC_ATTEN_DB_12
#define BATTERY_R_TOP_OHMS UINT64_C(1000000)
#define BATTERY_R_BOTTOM_OHMS UINT64_C(1000000)
#define BATTERY_ADC_SAMPLE_COUNT 32U
#define BATTERY_ADC_DISCARD_COUNT 4U
#define BATTERY_ADC_SETTLE_MS 10U
#define BATTERY_LOW_MV 3400
#define BATTERY_SHUTDOWN_MV 3200
#define BATTERY_ABSOLUTE_MIN_MV 2500
#define BATTERY_LOW_CONFIRMATION_COUNT 2

#define USER_LED_GPIO GPIO_NUM_10
#define SHT41_I2C_ADDR 0x44
#define SHT41_CMD_HIGH_PRECISION_NO_HEATER 0xFD
#define SHT41_MEASUREMENT_DELAY_MS 30

#define STATUS_SHT41_OK BIT0
#define STATUS_ESPNOW_SEND_ATTEMPTED BIT1
#define STATUS_BATTERY_OK BIT2
#define STATUS_BATTERY_LOW BIT3
#define STATUS_BATTERY_SHUTDOWN BIT4

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
RTC_DATA_ATTR static uint8_t low_battery_reading_count = 0;

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

static esp_err_t battery_calibration_create(adc_cali_handle_t *out_handle)
{
    if (out_handle == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *out_handle = NULL;

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t config = {
        .unit_id = BATTERY_ADC_UNIT,
        .chan = BATTERY_ADC_CHANNEL,
        .atten = BATTERY_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    return adc_cali_create_scheme_curve_fitting(&config, out_handle);
#elif ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    adc_cali_line_fitting_config_t config = {
        .unit_id = BATTERY_ADC_UNIT,
        .atten = BATTERY_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    return adc_cali_create_scheme_line_fitting(&config, out_handle);
#else
    ESP_LOGE(TAG, "No ADC calibration scheme is available for this target");
    return ESP_ERR_NOT_SUPPORTED;
#endif
}

static esp_err_t battery_calibration_delete(adc_cali_handle_t handle)
{
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    return adc_cali_delete_scheme_curve_fitting(handle);
#elif ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    return adc_cali_delete_scheme_line_fitting(handle);
#else
    (void)handle;
    return ESP_ERR_NOT_SUPPORTED;
#endif
}

static esp_err_t battery_read_mv(uint16_t *battery_mv)
{
    if (battery_mv == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *battery_mv = 0;

    adc_oneshot_unit_handle_t adc_handle = NULL;
    adc_cali_handle_t cali_handle = NULL;
    esp_err_t err;
    uint64_t calibrated_mv_sum = 0;
    uint64_t midpoint_mv = 0;
    uint64_t calculated_battery_mv = 0;
    uint16_t result_mv = 0;
    unsigned int valid_samples = 0;

    adc_unit_t mapped_unit;
    adc_channel_t mapped_channel;
    err = adc_oneshot_io_to_channel(BATTERY_ADC_GPIO, &mapped_unit, &mapped_channel);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Battery ADC GPIO%d mapping lookup failed: %s",
                 BATTERY_ADC_GPIO, esp_err_to_name(err));
        return err;
    }
    if (mapped_unit != BATTERY_ADC_UNIT || mapped_channel != BATTERY_ADC_CHANNEL) {
        ESP_LOGE(TAG,
                 "Battery ADC mapping mismatch: GPIO%d maps to unit %d channel %d, expected unit %d channel %d",
                 BATTERY_ADC_GPIO, mapped_unit + 1, mapped_channel,
                 BATTERY_ADC_UNIT + 1, BATTERY_ADC_CHANNEL);
        return ESP_ERR_INVALID_STATE;
    }

    adc_oneshot_unit_init_cfg_t unit_config = {
        .unit_id = BATTERY_ADC_UNIT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    err = adc_oneshot_new_unit(&unit_config, &adc_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Battery ADC unit initialization failed: %s", esp_err_to_name(err));
        goto cleanup;
    }

    adc_oneshot_chan_cfg_t channel_config = {
        .atten = BATTERY_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    err = adc_oneshot_config_channel(adc_handle, BATTERY_ADC_CHANNEL, &channel_config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Battery ADC channel configuration failed: %s", esp_err_to_name(err));
        goto cleanup;
    }

    err = battery_calibration_create(&cali_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG,
                 "Battery ADC calibration unavailable; refusing uncalibrated conversion: %s",
                 esp_err_to_name(err));
        goto cleanup;
    }

    vTaskDelay(pdMS_TO_TICKS(BATTERY_ADC_SETTLE_MS));

    for (unsigned int i = 0; i < BATTERY_ADC_DISCARD_COUNT; i++) {
        int raw;
        err = adc_oneshot_read(adc_handle, BATTERY_ADC_CHANNEL, &raw);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Battery ADC discarded sample %u failed: %s",
                     i + 1U, esp_err_to_name(err));
            goto cleanup;
        }
    }

    for (unsigned int i = 0; i < BATTERY_ADC_SAMPLE_COUNT; i++) {
        int raw;
        int calibrated_mv;

        err = adc_oneshot_read(adc_handle, BATTERY_ADC_CHANNEL, &raw);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Battery ADC sample %u failed after %u valid samples: %s",
                     i + 1U, valid_samples, esp_err_to_name(err));
            goto cleanup;
        }

        err = adc_cali_raw_to_voltage(cali_handle, raw, &calibrated_mv);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Battery ADC calibration conversion failed for sample %u: %s",
                     i + 1U, esp_err_to_name(err));
            goto cleanup;
        }
        if (calibrated_mv < 0) {
            ESP_LOGE(TAG, "Battery ADC calibration returned negative voltage: %d mV",
                     calibrated_mv);
            err = ESP_ERR_INVALID_RESPONSE;
            goto cleanup;
        }

        calibrated_mv_sum += (uint64_t)calibrated_mv;
        valid_samples++;
    }

    midpoint_mv = (calibrated_mv_sum + (BATTERY_ADC_SAMPLE_COUNT / 2U)) /
                  BATTERY_ADC_SAMPLE_COUNT;
    calculated_battery_mv =
        (midpoint_mv * (BATTERY_R_TOP_OHMS + BATTERY_R_BOTTOM_OHMS) +
         (BATTERY_R_BOTTOM_OHMS / 2U)) /
        BATTERY_R_BOTTOM_OHMS;
    result_mv = calculated_battery_mv > UINT16_MAX
                    ? UINT16_MAX
                    : (uint16_t)calculated_battery_mv;
    err = ESP_OK;

cleanup:
    if (cali_handle != NULL) {
        esp_err_t cleanup_err = battery_calibration_delete(cali_handle);
        if (cleanup_err != ESP_OK) {
            ESP_LOGE(TAG, "Battery ADC calibration cleanup failed: %s",
                     esp_err_to_name(cleanup_err));
            if (err == ESP_OK) {
                err = cleanup_err;
            }
        }
    }
    if (adc_handle != NULL) {
        esp_err_t cleanup_err = adc_oneshot_del_unit(adc_handle);
        if (cleanup_err != ESP_OK) {
            ESP_LOGE(TAG, "Battery ADC unit cleanup failed: %s", esp_err_to_name(cleanup_err));
            if (err == ESP_OK) {
                err = cleanup_err;
            }
        }
    }

    if (err != ESP_OK) {
        return err;
    }

    *battery_mv = result_mv;
    ESP_LOGI(TAG,
             "Battery ADC: midpoint=%llu mV whole_battery=%u mV valid_samples=%u calibrated=yes",
             (unsigned long long)midpoint_mv, (unsigned int)*battery_mv, valid_samples);

    if (*battery_mv > 4300U) {
        ESP_LOGW(TAG, "Battery reading is suspicious; check wiring and calibration: %u mV",
                 (unsigned int)*battery_mv);
    }

    return ESP_OK;
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

static esp_err_t sht41_probe(void)
{
    esp_err_t err = i2c_master_probe(i2c_bus, SHT41_I2C_ADDR, I2C_XFER_TIMEOUT_MS);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "SHT41 probe OK at I2C address 0x%02x", SHT41_I2C_ADDR);
    } else {
        ESP_LOGW(TAG, "SHT41 probe failed at I2C address 0x%02x: %s",
                 SHT41_I2C_ADDR, esp_err_to_name(err));
    }

    return err;
}

static esp_err_t sht41_read(float *temp_c, float *rh)
{
    uint8_t command = SHT41_CMD_HIGH_PRECISION_NO_HEATER;
    uint8_t data[6] = {0};
    esp_err_t err = i2c_master_transmit(sht41_dev, &command, sizeof(command),
                                        I2C_XFER_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SHT41 command transmit failed: %s", esp_err_to_name(err));
        return err;
    }

    vTaskDelay(pdMS_TO_TICKS(SHT41_MEASUREMENT_DELAY_MS));

    err = i2c_master_receive(sht41_dev, data, sizeof(data), I2C_XFER_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SHT41 measurement receive failed after %d ms delay: %s",
                 SHT41_MEASUREMENT_DELAY_MS, esp_err_to_name(err));
        return err;
    }

    if (sht41_crc8(&data[0], 2) != data[2]) {
        ESP_LOGE(TAG, "SHT41 temperature CRC mismatch: got=0x%02x expected=0x%02x",
                 data[2], sht41_crc8(&data[0], 2));
        return ESP_ERR_INVALID_CRC;
    }

    if (sht41_crc8(&data[3], 2) != data[5]) {
        ESP_LOGE(TAG, "SHT41 humidity CRC mismatch: got=0x%02x expected=0x%02x",
                 data[5], sht41_crc8(&data[3], 2));
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
    peer.channel = 0; // Use the current Wi-Fi channel because this gateway is connected to the router.
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

static void enter_indefinite_deep_sleep(void)
{
    ESP_LOGW(TAG,
             "Low-battery shutdown confirmed: disabling all wakeup sources; timed wakeup is disabled");
    esp_err_t err = esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_ALL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to disable deep-sleep wakeup sources: %s", esp_err_to_name(err));
    }
    ESP_LOGW(TAG, "Entering indefinite deep sleep; external reset or power cycle is required");
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

    uint16_t measured_battery_mv = 0;
    esp_err_t battery_err = battery_read_mv(&measured_battery_mv);
    if (battery_err != ESP_OK) {
        ESP_LOGE(TAG, "Battery measurement failed; reporting unavailable: %s",
                 esp_err_to_name(battery_err));
    }

    wifi_init();
    espnow_init();
    err = i2c_init();
    bool i2c_ready = (err == ESP_OK);
    if (!i2c_ready) {
        ESP_LOGE(TAG, "I2C init failed: %s", esp_err_to_name(err));
    } else {
        err = sht41_probe();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Continuing after SHT41 probe failure to capture read-stage errors");
        }
    }

    sensor_packet_t packet = {
        .node_id = NODE_ID,
        .sequence = sequence_number++,
        .temp_c = 0.0f,
        .rh = 0.0f,
        .battery_mv = 0,
        .status_flags = STATUS_ESPNOW_SEND_ATTEMPTED,
    };
    bool low_battery_shutdown = false;

    if (battery_err == ESP_OK) {
        packet.battery_mv = measured_battery_mv;
        packet.status_flags |= STATUS_BATTERY_OK;
    }

    if (battery_err == ESP_OK &&
        (packet.status_flags & STATUS_BATTERY_OK) != 0 &&
        packet.battery_mv != 0) {
        if (packet.battery_mv <= BATTERY_ABSOLUTE_MIN_MV) {
            ESP_LOGE(TAG,
                     "Battery at or below EVE cell absolute discharge cutoff: %u mV (cutoff=%u mV)",
                     (unsigned int)packet.battery_mv,
                     (unsigned int)BATTERY_ABSOLUTE_MIN_MV);
        }

        if (packet.battery_mv < BATTERY_LOW_MV) {
            packet.status_flags |= STATUS_BATTERY_LOW;
            ESP_LOGW(TAG, "Battery low: %u mV (warning threshold=%u mV)",
                     (unsigned int)packet.battery_mv, (unsigned int)BATTERY_LOW_MV);
        }

        if (packet.battery_mv <= BATTERY_SHUTDOWN_MV) {
            if (low_battery_reading_count < BATTERY_LOW_CONFIRMATION_COUNT) {
                low_battery_reading_count++;
            }
            ESP_LOGW(TAG,
                     "Battery shutdown confirmation %u/%u: %u mV (threshold=%u mV)",
                     (unsigned int)low_battery_reading_count,
                     (unsigned int)BATTERY_LOW_CONFIRMATION_COUNT,
                     (unsigned int)packet.battery_mv,
                     (unsigned int)BATTERY_SHUTDOWN_MV);
        } else {
            if (low_battery_reading_count != 0) {
                ESP_LOGI(TAG, "Battery recovered above shutdown threshold; confirmation count reset");
            }
            low_battery_reading_count = 0;
        }

        if (low_battery_reading_count >= BATTERY_LOW_CONFIRMATION_COUNT) {
            packet.status_flags |= STATUS_BATTERY_SHUTDOWN;
            low_battery_shutdown = true;
            ESP_LOGW(TAG,
                     "Low-battery shutdown confirmed; this cycle will send the final packet before indefinite sleep");
        }
    } else if (battery_err == ESP_OK &&
               (packet.status_flags & STATUS_BATTERY_OK) != 0 &&
               packet.battery_mv == 0) {
        ESP_LOGE(TAG, "Battery measurement returned zero; shutdown evaluation skipped");
    }

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

    vTaskDelay(pdMS_TO_TICKS(500));
    if (low_battery_shutdown) {
        enter_indefinite_deep_sleep();
    } else {
        enter_deep_sleep();
    }
}
