#ifndef SEN66_H
#define SEN66_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_err.h"

#define SEN66_I2C_ADDR 0x6B
#define SEN66_I2C_MAX_SPEED_HZ 100000U
#define SEN66_POWER_UP_DELAY_MS 100U
#define SEN66_I2C_XFER_TIMEOUT_MS 1000

#define SEN66_START_CONTINUOUS_MEASUREMENT_DELAY_MS 50U
#define SEN66_STOP_MEASUREMENT_DELAY_MS 1400U
#define SEN66_GET_DATA_READY_DELAY_MS 20U
#define SEN66_READ_MEASURED_VALUES_DELAY_MS 20U
#define SEN66_READ_MEASURED_RAW_VALUES_DELAY_MS 20U
#define SEN66_READ_DEVICE_STATUS_DELAY_MS 20U

#define SEN66_UNKNOWN_UINT16 0xFFFFU
#define SEN66_UNKNOWN_INT16 0x7FFF

#define SEN66_CMD_START_CONTINUOUS_MEASUREMENT 0x0021
#define SEN66_CMD_STOP_MEASUREMENT 0x0104
#define SEN66_CMD_GET_DATA_READY 0x0202
#define SEN66_CMD_READ_MEASURED_VALUES 0x0300
#define SEN66_CMD_READ_MEASURED_RAW_VALUES 0x0405
#define SEN66_CMD_READ_DEVICE_STATUS 0xD206
#define SEN66_CMD_READ_AND_CLEAR_DEVICE_STATUS 0xD210
#define SEN66_CMD_GET_PRODUCT_NAME 0xD014
#define SEN66_CMD_GET_SERIAL_NUMBER 0xD033
#define SEN66_CMD_GET_VERSION 0xD100
#define SEN66_CMD_DEVICE_RESET 0xD304

#define SEN66_MAX_READ_WORDS 16
#define SEN66_MAX_WRITE_WORDS 16

#define SEN66_VALUE_PM1_VALID (1UL << 0)
#define SEN66_VALUE_PM25_VALID (1UL << 1)
#define SEN66_VALUE_PM4_VALID (1UL << 2)
#define SEN66_VALUE_PM10_VALID (1UL << 3)
#define SEN66_VALUE_HUMIDITY_VALID (1UL << 4)
#define SEN66_VALUE_TEMPERATURE_VALID (1UL << 5)
#define SEN66_VALUE_VOC_VALID (1UL << 6)
#define SEN66_VALUE_NOX_VALID (1UL << 7)
#define SEN66_VALUE_CO2_VALID (1UL << 8)
#define SEN66_VALUE_SRAW_VOC_VALID (1UL << 9)
#define SEN66_VALUE_SRAW_NOX_VALID (1UL << 10)

typedef struct {
    i2c_master_bus_handle_t bus_handle;
    i2c_master_dev_handle_t dev_handle;
} sen66_t;

typedef struct {
    uint16_t pm1_ug_m3_x10;
    uint16_t pm25_ug_m3_x10;
    uint16_t pm4_ug_m3_x10;
    uint16_t pm10_ug_m3_x10;
    int16_t humidity_rh_x100;
    int16_t temperature_c_x200;
    int16_t voc_index_x10;
    int16_t nox_index_x10;
    uint16_t co2_ppm;
    uint16_t sraw_voc;
    uint16_t sraw_nox;

    float pm1_ug_m3;
    float pm25_ug_m3;
    float pm4_ug_m3;
    float pm10_ug_m3;
    float humidity_rh;
    float temperature_c;
    float voc_index;
    float nox_index;

    uint32_t valid_flags;
} sen66_measurement_t;

void sen66_wait_after_power_on(void);

esp_err_t sen66_i2c_init(sen66_t *sensor,
                         gpio_num_t sda_gpio,
                         gpio_num_t scl_gpio,
                         uint32_t scl_speed_hz);
esp_err_t sen66_probe(const sen66_t *sensor);
esp_err_t sen66_deinit(sen66_t *sensor);

uint8_t sen66_crc8(const uint8_t *data, size_t len);
bool sen66_word_crc_is_valid(const uint8_t word_with_crc[3]);

esp_err_t sen66_send_command(const sen66_t *sensor,
                             uint16_t command,
                             uint32_t execution_time_ms);
esp_err_t sen66_send_command_with_words(const sen66_t *sensor,
                                        uint16_t command,
                                        const uint16_t *words,
                                        size_t word_count,
                                        uint32_t execution_time_ms);
esp_err_t sen66_read_words(const sen66_t *sensor,
                           uint16_t command,
                           uint16_t *words,
                           size_t word_count,
                           uint32_t execution_time_ms);

esp_err_t sen66_start_continuous_measurement(const sen66_t *sensor);
esp_err_t sen66_stop_measurement(const sen66_t *sensor);
esp_err_t sen66_get_data_ready(const sen66_t *sensor, bool *ready);
esp_err_t sen66_read_measured_values(const sen66_t *sensor, sen66_measurement_t *measurement);
esp_err_t sen66_read_measured_raw_values(const sen66_t *sensor,
                                         sen66_measurement_t *measurement);
esp_err_t sen66_read_device_status(const sen66_t *sensor, uint32_t *device_status);
esp_err_t sen66_read_and_clear_device_status(const sen66_t *sensor, uint32_t *device_status);

#endif
