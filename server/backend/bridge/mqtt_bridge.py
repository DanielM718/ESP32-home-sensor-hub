"""MQTT-to-InfluxDB bridge service entrypoint."""

from __future__ import annotations

import logging

import paho.mqtt.client as mqtt

from app.config import AppSettings, ConfigError, configure_logging, load_settings
from app.influx import InfluxWriter
from app.validation import ValidationError
from bridge.topic_router import reading_from_mqtt_message


LOGGER = logging.getLogger("home_sensor.bridge")


class BridgeRuntime:
    """Owns MQTT callbacks and InfluxDB writes for the bridge process."""

    def __init__(self, settings: AppSettings, writer: InfluxWriter) -> None:
        self.settings = settings
        self.writer = writer

    def on_connect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _connect_flags: object,
        reason_code: object,
        _properties: object,
    ) -> None:
        if _reason_code_is_failure(reason_code):
            raise RuntimeError(f"MQTT connection failed: {reason_code}")

        subscriptions = [
            (self.settings.mqtt.sensor_topic, self.settings.mqtt.qos),
            (self.settings.mqtt.air_topic, self.settings.mqtt.qos),
        ]
        result, message_id = client.subscribe(subscriptions)
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT subscribe failed with code {result}")

        LOGGER.info(
            "connected to MQTT broker and subscribed",
            extra={"message_id": message_id, "subscriptions": subscriptions},
        )

    def on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _disconnect_flags: object,
        reason_code: object,
        _properties: object,
    ) -> None:
        if not _reason_code_is_failure(reason_code):
            LOGGER.info("disconnected from MQTT broker")
        else:
            LOGGER.warning("unexpected MQTT disconnect: %s", reason_code)

    def on_message(
        self,
        client: mqtt.Client,
        _userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        try:
            reading = reading_from_mqtt_message(
                message.topic,
                bytes(message.payload),
                max_payload_bytes=self.settings.mqtt.max_payload_bytes,
            )
        except ValidationError as exc:
            LOGGER.warning(
                "discarding invalid MQTT message from %s: %s", message.topic, exc
            )
            _ack_message(client, message)
            return

        self.writer.write_reading(reading)
        LOGGER.debug("wrote %s from topic %s", reading.measurement, message.topic)
        _ack_message(client, message)


def build_mqtt_client(settings: AppSettings, runtime: BridgeRuntime) -> mqtt.Client:
    """Create and configure the Paho MQTT client."""

    client = mqtt.Client(
        callback_api_version=_callback_api_version_2(),
        client_id=settings.mqtt.client_id,
        clean_session=True,
        protocol=mqtt.MQTTv311,
        reconnect_on_failure=True,
        manual_ack=True,
    )
    client.username_pw_set(settings.mqtt.username, settings.mqtt.password)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.enable_logger(logging.getLogger("paho.mqtt"))
    client.on_connect = runtime.on_connect
    client.on_disconnect = runtime.on_disconnect
    client.on_message = runtime.on_message
    return client


def run() -> int:
    """Run the bridge until interrupted or a fatal callback error occurs."""

    try:
        settings = load_settings()
        configure_logging(settings.log_level)
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        LOGGER.error("configuration error: %s", exc)
        return 2

    LOGGER.info("starting MQTT bridge")
    with InfluxWriter(settings.influx) as writer:
        runtime = BridgeRuntime(settings, writer)
        client = build_mqtt_client(settings, runtime)
        client.connect(
            settings.mqtt.host,
            settings.mqtt.port,
            settings.mqtt.keepalive_seconds,
        )
        client.loop_forever(retry_first_connection=False)

    return 0


def _callback_api_version_2() -> mqtt.CallbackAPIVersion:
    version = getattr(mqtt.CallbackAPIVersion, "API_VERSION2", None)
    if version is not None:
        return version

    version = getattr(mqtt.CallbackAPIVersion, "VERSION2", None)
    if version is not None:
        return version

    raise RuntimeError("paho-mqtt callback API v2 is not available")


def _reason_code_is_failure(reason_code: object) -> bool:
    is_failure = getattr(reason_code, "is_failure", None)
    if isinstance(is_failure, bool):
        return is_failure

    try:
        return int(reason_code) != 0
    except (TypeError, ValueError):
        return str(reason_code) not in {"0", "Success", "Normal disconnection"}


def _ack_message(client: mqtt.Client, message: mqtt.MQTTMessage) -> None:
    if message.qos > 0:
        client.ack(message.mid, message.qos)


def main() -> None:
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        LOGGER.info("MQTT bridge stopped by signal")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
