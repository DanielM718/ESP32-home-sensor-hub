idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES
                           nvs_flash
                           esp_wifi
                           esp_event
                           esp_netif
                           espnow_channel_recovery
                           esp_adc
                           esp_driver_gpio
                           esp_driver_i2c)
