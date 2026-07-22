from __future__ import annotations

from threading import Barrier
import unittest

from app.config import (
    AirQualitySettings,
    AppSettings,
    InfluxSettings,
    MqttSettings,
)
from app.web import create_app


class ConcurrentRepository:
    def __init__(self) -> None:
        self.barrier = Barrier(2, timeout=1.0)

    def latest(self):
        self.barrier.wait()
        return {"generated_at": "2026-07-22T12:00:00Z", "environment": [], "air_quality": []}

    def air_quality_context(self):
        self.barrier.wait()
        return {"locations": {}}


def settings() -> AppSettings:
    return AppSettings(
        log_level="INFO",
        node_stale_after_seconds=1800,
        mqtt=MqttSettings(
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=60,
            client_id="test",
            username="test",
            password="test",
            sensor_topic="home/sensors/+",
            air_topic="home/air/+",
            qos=1,
            max_payload_bytes=4096,
        ),
        influx=InfluxSettings(
            url="http://127.0.0.1:8086",
            org="test",
            bucket="environment",
            write_token="test",
            read_token="test",
            live_bucket="environment_live",
        ),
        air_quality=AirQualitySettings(),
    )


class WebConcurrencyTest(unittest.TestCase):
    def test_latest_and_context_queries_run_concurrently(self) -> None:
        repository = ConcurrentRepository()
        client = create_app(settings(), repository=repository).test_client()

        response = client.get("/api/latest")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(repository.barrier.n_waiting, 0)


if __name__ == "__main__":
    unittest.main()
