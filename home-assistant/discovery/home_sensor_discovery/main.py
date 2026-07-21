"""MQTT runtime for dynamic Home Assistant discovery and stale tracking."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from .discovery import (
    DeviceRecord,
    PayloadError,
    device_availability_topic,
    discovery_messages,
    is_stale,
    last_seen_topic,
    parse_message,
    registry_key,
    service_availability_topic,
)


LOGGER = logging.getLogger("home_sensor.discovery")
REGISTRY_PATH = Path("/data/devices.json")


class DiscoveryPublisher:
    def __init__(self) -> None:
        self.host = required_env("MQTT_HOST")
        self.port = int_env("MQTT_PORT", 1883, minimum=1, maximum=65535)
        self.username = required_env("MQTT_USERNAME")
        self.password = required_env("MQTT_PASSWORD")
        if self.password == "change_me":
            raise ValueError("replace MQTT_PASSWORD=change_me in /opt/home-assistant/.env")
        self.qos = int_env("MQTT_QOS", 1, minimum=0, maximum=2)
        self.stale_after = int_env(
            "SENSOR_STALE_AFTER_SECONDS", 1800, minimum=30, maximum=604_800
        )
        self.prefix = required_env("DISCOVERY_PREFIX", "homeassistant").strip("/")
        if not self.prefix or "+" in self.prefix or "#" in self.prefix:
            raise ValueError("DISCOVERY_PREFIX must be one or more literal topic levels")

        self.records = load_registry(REGISTRY_PATH)
        self.availability: dict[str, str] = {}
        self.registry_dirty = False
        self.last_registry_save = 0.0
        self.stop_event = threading.Event()
        self.connected = threading.Event()
        self.lock = threading.RLock()

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="home-sensor-ha-discovery",
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(self.username, self.password)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.will_set(
            service_availability_topic(self.prefix),
            payload="offline",
            qos=self.qos,
            retain=True,
        )
        self.client.enable_logger(logging.getLogger("paho.mqtt"))
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def on_connect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            LOGGER.error("MQTT connection failed: %s", reason_code)
            return
        LOGGER.info("connected to MQTT broker at %s:%d", self.host, self.port)
        self.connected.set()
        client.subscribe(
            [("home/sensors/+", self.qos), ("home/air/+", self.qos), (f"{self.prefix}/status", self.qos)]
        )
        self.publish_service_availability("online")
        self.republish_all(reason="service start/reconnect")

    def on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        self.connected.clear()
        if reason_code.is_failure:
            LOGGER.warning("unexpected MQTT disconnect: %s", reason_code)
        else:
            LOGGER.info("disconnected from MQTT broker")

    def on_message(
        self, client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage
    ) -> None:
        if message.topic == f"{self.prefix}/status":
            if bytes(message.payload).decode("utf-8", errors="replace").strip().lower() == "online":
                self.republish_all(reason="Home Assistant birth message")
            return

        try:
            record = parse_message(message.topic, bytes(message.payload))
        except PayloadError as exc:
            LOGGER.warning("ignoring malformed message from %s: %s", message.topic, exc)
            return

        key = registry_key(record)
        with self.lock:
            previous = self.records.get(key)
            identity_changed = previous is None or previous.metadata != record.metadata
            self.records[key] = record
            self.registry_dirty = True
            self.persist_registry(force=identity_changed)
            if identity_changed:
                self.publish_discovery(record)
            self.publish_record_state(record, "online", retain_last_seen=False)

    def publish_discovery(self, record: DeviceRecord) -> None:
        messages = discovery_messages(
            record,
            discovery_prefix=self.prefix,
            stale_after_seconds=self.stale_after,
        )
        for topic, payload in messages.items():
            self.publish(topic, payload, retain=True)
        LOGGER.info(
            "published %d retained discovery configs for %s from %s",
            len(messages),
            record.key,
            record.topic,
        )

    def publish_record_state(
        self, record: DeviceRecord, availability: str, *, retain_last_seen: bool = False
    ) -> None:
        key = registry_key(record)
        if self.availability.get(key) != availability:
            self.publish(
                device_availability_topic(self.prefix, record), availability, retain=True
            )
            self.availability[key] = availability
            LOGGER.info("marked %s %s", record.key, availability)
        self.publish(
            last_seen_topic(self.prefix, record),
            record.last_seen,
            retain=retain_last_seen,
        )

    def publish_service_availability(self, availability: str) -> None:
        self.publish(
            service_availability_topic(self.prefix), availability, retain=True
        )

    def republish_all(self, *, reason: str) -> None:
        with self.lock:
            LOGGER.info("republishing discovery for %d known devices: %s", len(self.records), reason)
            now = datetime.now(timezone.utc)
            for record in self.records.values():
                self.publish_discovery(record)
                status = (
                    "offline"
                    if is_stale(record, now=now, stale_after_seconds=self.stale_after)
                    else "online"
                )
                self.publish_record_state(record, status, retain_last_seen=True)

    def mark_stale_devices(self) -> None:
        with self.lock:
            now = datetime.now(timezone.utc)
            for key, record in self.records.items():
                if self.availability.get(key) == "offline":
                    continue
                if is_stale(record, now=now, stale_after_seconds=self.stale_after):
                    self.publish_record_state(record, "offline")

    def persist_registry(self, *, force: bool = False) -> None:
        if not self.registry_dirty:
            return
        now = time.monotonic()
        if not force and now - self.last_registry_save < 60:
            return
        try:
            save_registry(REGISTRY_PATH, self.records)
        except OSError as exc:
            LOGGER.error("could not persist discovery registry: %s", exc)
            return
        self.registry_dirty = False
        self.last_registry_save = now

    def publish(self, topic: str, payload: str, *, retain: bool) -> None:
        result = self.client.publish(topic, payload, qos=self.qos, retain=retain)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            LOGGER.error("MQTT publish failed for %s with code %s", topic, result.rc)

    def run(self) -> int:
        LOGGER.info(
            "starting discovery publisher: stale_after=%ds prefix=%s known_devices=%d",
            self.stale_after,
            self.prefix,
            len(self.records),
        )
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()
        try:
            while not self.stop_event.wait(timeout=min(15, max(1, self.stale_after // 4))):
                if self.connected.is_set():
                    self.mark_stale_devices()
                with self.lock:
                    self.persist_registry()
        finally:
            with self.lock:
                self.persist_registry(force=True)
            if self.connected.is_set():
                self.publish_service_availability("offline")
                time.sleep(0.1)
            self.client.disconnect()
            self.client.loop_stop()
        return 0


def load_registry(path: Path) -> dict[str, DeviceRecord]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PayloadError("registry root must be an object")
        return {
            str(key): DeviceRecord.from_dict(value)
            for key, value in raw.items()
            if isinstance(value, dict)
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, PayloadError) as exc:
        LOGGER.error("ignoring unreadable discovery registry %s: %s", path, exc)
        return {}


def save_registry(path: Path, records: dict[str, DeviceRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    serialized = {key: record.to_dict() for key, record in sorted(records.items())}
    temporary.write_text(
        json.dumps(serialized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def required_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise ValueError(f"{name} must be set")
    return value.strip()


def int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        publisher = DiscoveryPublisher()
    except (ValueError, TypeError) as exc:
        LOGGER.error("configuration error: %s", exc)
        raise SystemExit(2) from exc

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, lambda _signum, _frame: publisher.stop_event.set())

    try:
        raise SystemExit(publisher.run())
    except (OSError, mqtt.MQTTException) as exc:
        LOGGER.error("fatal MQTT error: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
