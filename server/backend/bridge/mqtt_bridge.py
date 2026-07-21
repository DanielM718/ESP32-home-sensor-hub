"""MQTT-to-InfluxDB bridge service entrypoint."""

from __future__ import annotations

import logging
import signal
import threading

import paho.mqtt.client as mqtt

from app.config import AppSettings, ConfigError, configure_logging, load_settings
from app.influx import InfluxWriter
from app.models import AirQualityReading
from app.validation import ValidationError
from bridge.air_quality_pipeline import AirQualityPipeline
from bridge.topic_router import reading_from_mqtt_message


LOGGER = logging.getLogger("home_sensor.bridge")


class BridgeRuntime:
    """Owns MQTT callbacks and InfluxDB writes for the bridge process."""

    def __init__(self, settings: AppSettings, writer: InfluxWriter) -> None:
        self.settings = settings
        self.writer = writer
        self.pipeline = AirQualityPipeline(
            expected_publish_seconds=settings.air_quality.expected_publish_seconds
        )
        self.fatal_error: Exception | None = None

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

        try:
            if isinstance(reading, AirQualityReading):
                result = self.pipeline.process(reading)
                if result.duplicate:
                    LOGGER.info(
                        "ignored duplicate SEN66 packet from %s (boot_id=%s sequence=%s)",
                        reading.location,
                        reading.boot_id,
                        reading.sequence,
                    )
                    _ack_message(client, message)
                    return
                if result.late:
                    LOGGER.warning(
                        "ignored late SEN66 packet from closed window: %s", reading.location
                    )
                    _ack_message(client, message)
                    return

                self.writer.write_reading(
                    reading, bucket=self.settings.influx.live_bucket
                )
                self.writer.write_point_data_many(result.aggregate_points)
                self.writer.write_point_data_many(result.event_points)
                LOGGER.debug(
                    "stored live SEN66 sample (%d aggregate, %d event updates) from %s",
                    len(result.aggregate_points),
                    len(result.event_points),
                    message.topic,
                )
            else:
                self.writer.write_reading(reading)
                LOGGER.debug("wrote %s from topic %s", reading.measurement, message.topic)
        except Exception as exc:
            LOGGER.exception("database pipeline failed; leaving MQTT packet unacknowledged")
            self.fatal_error = exc
            client.disconnect()
            return
        _ack_message(client, message)

    def recover_recent_window(self) -> None:
        """Rebuild current/just-completed windows from bounded live storage."""

        try:
            active_events = self.writer.active_air_quality_events()
            readings = self.writer.recent_air_quality_readings(
                lookback_minutes=self.settings.air_quality.recovery_lookback_minutes
            )
        except Exception:
            LOGGER.exception(
                "could not recover recent SEN66 live samples; starting with an empty window"
            )
            return

        restored_event_count = self.pipeline.restore_active_events(active_events)
        aggregate_count = 0
        event_count = 0
        for reading in readings:
            result = self.pipeline.process(reading)
            self.writer.write_point_data_many(result.aggregate_points)
            self.writer.write_point_data_many(result.event_points)
            aggregate_count += len(result.aggregate_points)
            event_count += len(result.event_points)
        LOGGER.info(
            "restored %d active events and recovered %d SEN66 live samples "
            "(%d aggregate, %d event updates)",
            restored_event_count,
            len(readings),
            aggregate_count,
            event_count,
        )

    def flush_partial_windows(self) -> None:
        points = self.pipeline.flush_partial()
        self.writer.write_point_data_many(points)
        if points:
            LOGGER.info("persisted %d partial SEN66 aggregation windows", len(points))
        active_events = self.pipeline.flush_active_events()
        self.writer.write_point_data_many(active_events)
        if active_events:
            LOGGER.info("persisted %d active SEN66 event states", len(active_events))


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
        runtime.recover_recent_window()
        client = build_mqtt_client(settings, runtime)
        stop_event = threading.Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop_event.set()

        previous_term_handler = signal.signal(signal.SIGTERM, request_stop)
        client.connect(
            settings.mqtt.host,
            settings.mqtt.port,
            settings.mqtt.keepalive_seconds,
        )
        client.loop_start()
        try:
            while not stop_event.wait(1.0):
                if runtime.fatal_error is not None:
                    raise RuntimeError("bridge pipeline failed") from runtime.fatal_error
        except KeyboardInterrupt:
            LOGGER.info("MQTT bridge stopped by operator")
        finally:
            try:
                runtime.flush_partial_windows()
            finally:
                client.disconnect()
                client.loop_stop()
                signal.signal(signal.SIGTERM, previous_term_handler)

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
    raise SystemExit(run())


if __name__ == "__main__":
    main()
