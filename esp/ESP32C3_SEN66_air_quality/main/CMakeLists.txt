idf_component_register(SRCS
                           "main.c"
                           "sen66.c"
                           "mqtt_transport.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES
                           nvs_flash
                           esp_wifi
                           esp_event
                           esp_netif
                           mqtt
                           esp_driver_gpio
                           esp_driver_i2c)
