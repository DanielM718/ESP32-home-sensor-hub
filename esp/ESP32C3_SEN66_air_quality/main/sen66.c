#include "sen66.h"

#include <math.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define SEN66_WORD_SIZE 2
#define SEN66_WORD_WITH_CRC_SIZE 3
#define SEN66_COMMAND_SIZE 2
#define SEN66_CRC_POLYNOMIAL 0x31
#define SEN66_CRC_INIT 0xFF
#define SEN66_MEASURED_VALUE_WORDS 9
#define SEN66_DEVICE_STATUS_WORDS 2

static const char *TAG = "SEN66";

static bool sen66_has_i2c_device(const sen66_t *sensor)
{
    return sensor != NULL && sensor->bus_handle != NULL && sensor->dev_handle != NULL;
}

static void sen66_encode_command(uint16_t command, uint8_t out[SEN66_COMMAND_SIZE])
{
    out[0] = (uint8_t)(command >> 8);
    out[1] = (uint8_t)(command & 0xFF);
}

static uint16_t sen66_decode_word(const uint8_t word_with_crc[SEN66_WORD_WITH_CRC_SIZE])
{
    return ((uint16_t)word_with_crc[0] << 8) | word_with_crc[1];
}

static bool sen66_u16_is_known(uint16_t value)
{
    return value != SEN66_UNKNOWN_UINT16;
}

static bool sen66_i16_is_known(int16_t value)
{
    return value != (int16_t)SEN66_UNKNOWN_INT16;
}

static float sen66_scaled_u16_or_nan(uint16_t value, float scale)
{
    return sen66_u16_is_known(value) ? ((float)value / scale) : NAN;
}

static float sen66_scaled_i16_or_nan(int16_t value, float scale)
{
    return sen66_i16_is_known(value) ? ((float)value / scale) : NAN;
}

void sen66_wait_after_power_on(void)
{
    vTaskDelay(pdMS_TO_TICKS(SEN66_POWER_UP_DELAY_MS));
}

esp_err_t sen66_i2c_init(sen66_t *sensor,
                         gpio_num_t sda_gpio,
                         gpio_num_t scl_gpio,
                         uint32_t scl_speed_hz)
{
    if (sensor == NULL || scl_speed_hz == 0 || scl_speed_hz > SEN66_I2C_MAX_SPEED_HZ) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(sensor, 0, sizeof(*sensor));

    i2c_master_bus_config_t bus_config = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = sda_gpio,
        .scl_io_num = scl_gpio,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };

    esp_err_t err = i2c_new_master_bus(&bus_config, &sensor->bus_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create I2C master bus: %s", esp_err_to_name(err));
        return err;
    }

    i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = SEN66_I2C_ADDR,
        .scl_speed_hz = scl_speed_hz,
    };

    err = i2c_master_bus_add_device(sensor->bus_handle, &dev_config, &sensor->dev_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to add SEN66 I2C device: %s", esp_err_to_name(err));
        i2c_del_master_bus(sensor->bus_handle);
        sensor->bus_handle = NULL;
        return err;
    }

    ESP_LOGI(TAG, "I2C initialized: addr=0x%02X SDA GPIO%d SCL GPIO%d speed=%lu Hz",
             SEN66_I2C_ADDR,
             sda_gpio,
             scl_gpio,
             (unsigned long)scl_speed_hz);
    return ESP_OK;
}

esp_err_t sen66_probe(const sen66_t *sensor)
{
    if (sensor == NULL || sensor->bus_handle == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = i2c_master_probe(sensor->bus_handle,
                                     SEN66_I2C_ADDR,
                                     SEN66_I2C_XFER_TIMEOUT_MS);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "SEN66 detected at I2C address 0x%02X", SEN66_I2C_ADDR);
    } else {
        ESP_LOGE(TAG, "SEN66 probe failed at I2C address 0x%02X: %s",
                 SEN66_I2C_ADDR,
                 esp_err_to_name(err));
    }
    return err;
}

esp_err_t sen66_deinit(sen66_t *sensor)
{
    if (sensor == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t first_err = ESP_OK;

    if (sensor->dev_handle != NULL) {
        first_err = i2c_master_bus_rm_device(sensor->dev_handle);
        if (first_err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to remove SEN66 I2C device: %s", esp_err_to_name(first_err));
        }
        sensor->dev_handle = NULL;
    }

    if (sensor->bus_handle != NULL) {
        esp_err_t err = i2c_del_master_bus(sensor->bus_handle);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Failed to delete I2C master bus: %s", esp_err_to_name(err));
            if (first_err == ESP_OK) {
                first_err = err;
            }
        }
        sensor->bus_handle = NULL;
    }

    return first_err;
}

uint8_t sen66_crc8(const uint8_t *data, size_t len)
{
    uint8_t crc = SEN66_CRC_INIT;

    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            if ((crc & 0x80) != 0) {
                crc = (uint8_t)((crc << 1) ^ SEN66_CRC_POLYNOMIAL);
            } else {
                crc <<= 1;
            }
        }
    }

    return crc;
}

bool sen66_word_crc_is_valid(const uint8_t word_with_crc[SEN66_WORD_WITH_CRC_SIZE])
{
    return word_with_crc != NULL &&
           sen66_crc8(word_with_crc, SEN66_WORD_SIZE) == word_with_crc[2];
}

esp_err_t sen66_send_command(const sen66_t *sensor,
                             uint16_t command,
                             uint32_t execution_time_ms)
{
    if (!sen66_has_i2c_device(sensor)) {
        return ESP_ERR_INVALID_STATE;
    }

    uint8_t tx[SEN66_COMMAND_SIZE];
    sen66_encode_command(command, tx);

    esp_err_t err = i2c_master_transmit(sensor->dev_handle,
                                        tx,
                                        sizeof(tx),
                                        SEN66_I2C_XFER_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Command 0x%04X transmit failed: %s",
                 command,
                 esp_err_to_name(err));
        return err;
    }

    if (execution_time_ms > 0) {
        vTaskDelay(pdMS_TO_TICKS(execution_time_ms));
    }

    return ESP_OK;
}

esp_err_t sen66_send_command_with_words(const sen66_t *sensor,
                                        uint16_t command,
                                        const uint16_t *words,
                                        size_t word_count,
                                        uint32_t execution_time_ms)
{
    if (!sen66_has_i2c_device(sensor) ||
        (word_count > 0 && words == NULL) ||
        word_count > SEN66_MAX_WRITE_WORDS) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t tx[SEN66_COMMAND_SIZE + (SEN66_MAX_WRITE_WORDS * SEN66_WORD_WITH_CRC_SIZE)] = {0};
    sen66_encode_command(command, tx);

    for (size_t i = 0; i < word_count; i++) {
        uint8_t *encoded_word = &tx[SEN66_COMMAND_SIZE + (i * SEN66_WORD_WITH_CRC_SIZE)];
        encoded_word[0] = (uint8_t)(words[i] >> 8);
        encoded_word[1] = (uint8_t)(words[i] & 0xFF);
        encoded_word[2] = sen66_crc8(encoded_word, SEN66_WORD_SIZE);
    }

    const size_t tx_len = SEN66_COMMAND_SIZE + (word_count * SEN66_WORD_WITH_CRC_SIZE);
    esp_err_t err = i2c_master_transmit(sensor->dev_handle,
                                        tx,
                                        tx_len,
                                        SEN66_I2C_XFER_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Command 0x%04X write failed: %s",
                 command,
                 esp_err_to_name(err));
        return err;
    }

    if (execution_time_ms > 0) {
        vTaskDelay(pdMS_TO_TICKS(execution_time_ms));
    }

    return ESP_OK;
}

esp_err_t sen66_read_words(const sen66_t *sensor,
                           uint16_t command,
                           uint16_t *words,
                           size_t word_count,
                           uint32_t execution_time_ms)
{
    if (!sen66_has_i2c_device(sensor) ||
        words == NULL ||
        word_count == 0 ||
        word_count > SEN66_MAX_READ_WORDS) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t err = sen66_send_command(sensor, command, execution_time_ms);
    if (err != ESP_OK) {
        return err;
    }

    uint8_t rx[SEN66_MAX_READ_WORDS * SEN66_WORD_WITH_CRC_SIZE] = {0};
    const size_t rx_len = word_count * SEN66_WORD_WITH_CRC_SIZE;

    err = i2c_master_receive(sensor->dev_handle,
                             rx,
                             rx_len,
                             SEN66_I2C_XFER_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Command 0x%04X read failed: %s",
                 command,
                 esp_err_to_name(err));
        return err;
    }

    for (size_t i = 0; i < word_count; i++) {
        const uint8_t *word_with_crc = &rx[i * SEN66_WORD_WITH_CRC_SIZE];
        const uint8_t expected_crc = sen66_crc8(word_with_crc, SEN66_WORD_SIZE);
        if (expected_crc != word_with_crc[2]) {
            ESP_LOGE(TAG, "Command 0x%04X word %u CRC mismatch: expected 0x%02X got 0x%02X",
                     command,
                     (unsigned int)i,
                     expected_crc,
                     word_with_crc[2]);
            return ESP_ERR_INVALID_CRC;
        }

        words[i] = sen66_decode_word(word_with_crc);
    }

    return ESP_OK;
}

esp_err_t sen66_start_continuous_measurement(const sen66_t *sensor)
{
    esp_err_t err = sen66_send_command(sensor,
                                       SEN66_CMD_START_CONTINUOUS_MEASUREMENT,
                                       SEN66_START_CONTINUOUS_MEASUREMENT_DELAY_MS);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Continuous measurement started");
    }
    return err;
}

esp_err_t sen66_stop_measurement(const sen66_t *sensor)
{
    esp_err_t err = sen66_send_command(sensor,
                                       SEN66_CMD_STOP_MEASUREMENT,
                                       SEN66_STOP_MEASUREMENT_DELAY_MS);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Measurement stopped");
    }
    return err;
}

esp_err_t sen66_get_data_ready(const sen66_t *sensor, bool *ready)
{
    if (ready == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t word = 0;
    esp_err_t err = sen66_read_words(sensor,
                                     SEN66_CMD_GET_DATA_READY,
                                     &word,
                                     1,
                                     SEN66_GET_DATA_READY_DELAY_MS);
    if (err != ESP_OK) {
        *ready = false;
        return err;
    }

    *ready = (word & 0x00FF) == 0x01;
    return ESP_OK;
}

esp_err_t sen66_read_measured_values(const sen66_t *sensor, sen66_measurement_t *measurement)
{
    if (measurement == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t words[SEN66_MEASURED_VALUE_WORDS] = {0};
    esp_err_t err = sen66_read_words(sensor,
                                     SEN66_CMD_READ_MEASURED_VALUES,
                                     words,
                                     SEN66_MEASURED_VALUE_WORDS,
                                     SEN66_READ_MEASURED_VALUES_DELAY_MS);
    if (err != ESP_OK) {
        memset(measurement, 0, sizeof(*measurement));
        return err;
    }

    memset(measurement, 0, sizeof(*measurement));
    measurement->pm1_ug_m3_x10 = words[0];
    measurement->pm25_ug_m3_x10 = words[1];
    measurement->pm4_ug_m3_x10 = words[2];
    measurement->pm10_ug_m3_x10 = words[3];
    measurement->humidity_rh_x100 = (int16_t)words[4];
    measurement->temperature_c_x200 = (int16_t)words[5];
    measurement->voc_index_x10 = (int16_t)words[6];
    measurement->nox_index_x10 = (int16_t)words[7];
    measurement->co2_ppm = words[8];

    if (sen66_u16_is_known(measurement->pm1_ug_m3_x10)) {
        measurement->valid_flags |= SEN66_VALUE_PM1_VALID;
    }
    if (sen66_u16_is_known(measurement->pm25_ug_m3_x10)) {
        measurement->valid_flags |= SEN66_VALUE_PM25_VALID;
    }
    if (sen66_u16_is_known(measurement->pm4_ug_m3_x10)) {
        measurement->valid_flags |= SEN66_VALUE_PM4_VALID;
    }
    if (sen66_u16_is_known(measurement->pm10_ug_m3_x10)) {
        measurement->valid_flags |= SEN66_VALUE_PM10_VALID;
    }
    if (sen66_i16_is_known(measurement->humidity_rh_x100)) {
        measurement->valid_flags |= SEN66_VALUE_HUMIDITY_VALID;
    }
    if (sen66_i16_is_known(measurement->temperature_c_x200)) {
        measurement->valid_flags |= SEN66_VALUE_TEMPERATURE_VALID;
    }
    if (sen66_i16_is_known(measurement->voc_index_x10)) {
        measurement->valid_flags |= SEN66_VALUE_VOC_VALID;
    }
    if (sen66_i16_is_known(measurement->nox_index_x10)) {
        measurement->valid_flags |= SEN66_VALUE_NOX_VALID;
    }
    if (sen66_u16_is_known(measurement->co2_ppm)) {
        measurement->valid_flags |= SEN66_VALUE_CO2_VALID;
    }

    measurement->pm1_ug_m3 = sen66_scaled_u16_or_nan(measurement->pm1_ug_m3_x10, 10.0f);
    measurement->pm25_ug_m3 = sen66_scaled_u16_or_nan(measurement->pm25_ug_m3_x10, 10.0f);
    measurement->pm4_ug_m3 = sen66_scaled_u16_or_nan(measurement->pm4_ug_m3_x10, 10.0f);
    measurement->pm10_ug_m3 = sen66_scaled_u16_or_nan(measurement->pm10_ug_m3_x10, 10.0f);
    measurement->humidity_rh = sen66_scaled_i16_or_nan(measurement->humidity_rh_x100, 100.0f);
    measurement->temperature_c = sen66_scaled_i16_or_nan(measurement->temperature_c_x200, 200.0f);
    measurement->voc_index = sen66_scaled_i16_or_nan(measurement->voc_index_x10, 10.0f);
    measurement->nox_index = sen66_scaled_i16_or_nan(measurement->nox_index_x10, 10.0f);

    return ESP_OK;
}

esp_err_t sen66_read_device_status(const sen66_t *sensor, uint32_t *device_status)
{
    if (device_status == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t words[SEN66_DEVICE_STATUS_WORDS] = {0};
    esp_err_t err = sen66_read_words(sensor,
                                     SEN66_CMD_READ_DEVICE_STATUS,
                                     words,
                                     SEN66_DEVICE_STATUS_WORDS,
                                     SEN66_READ_DEVICE_STATUS_DELAY_MS);
    if (err != ESP_OK) {
        *device_status = 0;
        return err;
    }

    *device_status = ((uint32_t)words[0] << 16) | words[1];
    return ESP_OK;
}

esp_err_t sen66_read_and_clear_device_status(const sen66_t *sensor, uint32_t *device_status)
{
    if (device_status == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint16_t words[SEN66_DEVICE_STATUS_WORDS] = {0};
    esp_err_t err = sen66_read_words(sensor,
                                     SEN66_CMD_READ_AND_CLEAR_DEVICE_STATUS,
                                     words,
                                     SEN66_DEVICE_STATUS_WORDS,
                                     SEN66_READ_DEVICE_STATUS_DELAY_MS);
    if (err != ESP_OK) {
        *device_status = 0;
        return err;
    }

    *device_status = ((uint32_t)words[0] << 16) | words[1];
    return ESP_OK;
}

