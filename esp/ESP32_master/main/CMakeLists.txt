idf_component_register(
    SRCS
        "main.c"
        "gateway_packets.c"
        "mqtt_publisher.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES
        nvs_flash
        esp_event
        esp_netif
        esp_wifi
)
