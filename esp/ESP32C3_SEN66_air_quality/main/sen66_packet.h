#ifndef SEN66_PACKET_H
#define SEN66_PACKET_H

#include <stdint.h>

#include "sen66.h"

#define SENSOR_PACKET_TYPE_SEN66 0x6601U
#define SEN66_PACKET_SIZE_BYTES 32U

#define SEN66_PACKET_STATUS_I2C_READY (1UL << 0)
#define SEN66_PACKET_STATUS_MEASUREMENT_STARTED (1UL << 1)
#define SEN66_PACKET_STATUS_DATA_READY (1UL << 2)
#define SEN66_PACKET_STATUS_MEASUREMENT_READ_OK (1UL << 3)
#define SEN66_PACKET_STATUS_DEVICE_STATUS_READ_OK (1UL << 4)
#define SEN66_PACKET_STATUS_DEVICE_STATUS_NONZERO (1UL << 5)
#define SEN66_PACKET_STATUS_ESPNOW_READY (1UL << 6)
#define SEN66_PACKET_STATUS_ESPNOW_SEND_ATTEMPTED (1UL << 7)
#define SEN66_PACKET_STATUS_ESPNOW_SEND_OK (1UL << 8)
#define SEN66_PACKET_STATUS_ESPNOW_SEND_FAILED (1UL << 9)
#define SEN66_PACKET_STATUS_READ_ERROR (1UL << 10)
#define SEN66_PACKET_STATUS_CRC_ERROR (1UL << 11)

#define SEN66_PACKET_STATUS_PM1_UNKNOWN (1UL << 12)
#define SEN66_PACKET_STATUS_PM25_UNKNOWN (1UL << 13)
#define SEN66_PACKET_STATUS_PM4_UNKNOWN (1UL << 14)
#define SEN66_PACKET_STATUS_PM10_UNKNOWN (1UL << 15)
#define SEN66_PACKET_STATUS_HUMIDITY_UNKNOWN (1UL << 16)
#define SEN66_PACKET_STATUS_TEMPERATURE_UNKNOWN (1UL << 17)
#define SEN66_PACKET_STATUS_VOC_UNKNOWN (1UL << 18)
#define SEN66_PACKET_STATUS_NOX_UNKNOWN (1UL << 19)
#define SEN66_PACKET_STATUS_CO2_UNKNOWN (1UL << 20)

typedef struct __attribute__((packed)) {
    uint16_t packet_type;
    uint32_t node_id;
    uint32_t sequence;
    uint16_t co2_ppm;
    uint16_t pm1_ug_m3_x10;
    uint16_t pm25_ug_m3_x10;
    uint16_t pm4_ug_m3_x10;
    uint16_t pm10_ug_m3_x10;
    int16_t voc_index_x10;
    int16_t nox_index_x10;
    int16_t temperature_c_x200;
    int16_t humidity_rh_x100;
    uint32_t status_flags;
} sen66_packet_t;

_Static_assert(sizeof(sen66_packet_t) == SEN66_PACKET_SIZE_BYTES,
               "sen66_packet_t size must stay fixed for gateway parsing");

static inline uint32_t sen66_packet_status_from_measurement(const sen66_measurement_t *measurement)
{
    uint32_t status_flags = 0;

    if ((measurement->valid_flags & SEN66_VALUE_PM1_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_PM1_UNKNOWN;
    }
    if ((measurement->valid_flags & SEN66_VALUE_PM25_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_PM25_UNKNOWN;
    }
    if ((measurement->valid_flags & SEN66_VALUE_PM4_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_PM4_UNKNOWN;
    }
    if ((measurement->valid_flags & SEN66_VALUE_PM10_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_PM10_UNKNOWN;
    }
    if ((measurement->valid_flags & SEN66_VALUE_HUMIDITY_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_HUMIDITY_UNKNOWN;
    }
    if ((measurement->valid_flags & SEN66_VALUE_TEMPERATURE_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_TEMPERATURE_UNKNOWN;
    }
    if ((measurement->valid_flags & SEN66_VALUE_VOC_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_VOC_UNKNOWN;
    }
    if ((measurement->valid_flags & SEN66_VALUE_NOX_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_NOX_UNKNOWN;
    }
    if ((measurement->valid_flags & SEN66_VALUE_CO2_VALID) == 0) {
        status_flags |= SEN66_PACKET_STATUS_CO2_UNKNOWN;
    }

    return status_flags;
}

static inline void sen66_packet_from_measurement(sen66_packet_t *packet,
                                                 uint32_t node_id,
                                                 uint32_t sequence,
                                                 const sen66_measurement_t *measurement,
                                                 uint32_t status_flags)
{
    packet->packet_type = SENSOR_PACKET_TYPE_SEN66;
    packet->node_id = node_id;
    packet->sequence = sequence;
    packet->co2_ppm = measurement->co2_ppm;
    packet->pm1_ug_m3_x10 = measurement->pm1_ug_m3_x10;
    packet->pm25_ug_m3_x10 = measurement->pm25_ug_m3_x10;
    packet->pm4_ug_m3_x10 = measurement->pm4_ug_m3_x10;
    packet->pm10_ug_m3_x10 = measurement->pm10_ug_m3_x10;
    packet->voc_index_x10 = measurement->voc_index_x10;
    packet->nox_index_x10 = measurement->nox_index_x10;
    packet->temperature_c_x200 = measurement->temperature_c_x200;
    packet->humidity_rh_x100 = measurement->humidity_rh_x100;
    packet->status_flags = status_flags | sen66_packet_status_from_measurement(measurement);
}

#endif

