"""Read-only latency probe for dashboard inventory Flux queries.

Run from ``server/backend`` with the production environment exported.  The
probe only uses InfluxDB's query API and prints aggregate timings; it never
writes points or configuration.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from app.config import load_settings
from app.queries import (
    AIR_QUALITY_AGGREGATE_MEASUREMENT,
    AIR_QUALITY_FIELDS,
    AIR_QUALITY_LATEST_FIELDS,
    AIR_QUALITY_LATEST_LOOKBACK,
    AIR_QUALITY_MEASUREMENT,
    ENVIRONMENT_LATEST_FIELDS,
    ENVIRONMENT_LATEST_LOOKBACK,
    ENVIRONMENT_MEASUREMENT,
    InfluxReadRepository,
    air_quality_context_flux,
    latest_flux,
)


class _NoOpStore:
    def initialize(self) -> None:
        pass


def _flux_string(value: str) -> str:
    return json.dumps(value)


def _flux_array(values: tuple[str, ...]) -> str:
    return json.dumps(list(values))


def query_variants(bucket: str, live_bucket: str) -> dict[str, str]:
    aggregate_mean_fields = tuple(f"{field}_mean" for field in AIR_QUALITY_FIELDS)
    environment_inventory = f"""from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(ENVIRONMENT_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(ENVIRONMENT_LATEST_FIELDS)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()
"""
    air_inventory = f"""from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_AGGREGATE_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(aggregate_mean_fields)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()
"""
    environment_latest = f"""from(bucket: {_flux_string(bucket)})
  |> range(start: {ENVIRONMENT_LATEST_LOOKBACK})
  |> filter(fn: (r) => r._measurement == {_flux_string(ENVIRONMENT_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(ENVIRONMENT_LATEST_FIELDS)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()
"""
    air_latest = f"""from(bucket: {_flux_string(live_bucket)})
  |> range(start: {AIR_QUALITY_LATEST_LOOKBACK})
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(AIR_QUALITY_LATEST_FIELDS)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()
"""
    optimized_environment_inventory = f"""from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(ENVIRONMENT_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(ENVIRONMENT_LATEST_FIELDS)}))
  |> last()
"""
    optimized_air_inventory = f"""from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_AGGREGATE_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(aggregate_mean_fields)}))
  |> last()
"""
    optimized_inventory = (
        "environmentInventory = "
        + optimized_environment_inventory
        + "\nairQualityInventory = "
        + optimized_air_inventory
        + "\nunion(tables: [environmentInventory, airQualityInventory])\n"
    )
    schema_air_locations = f"""import "influxdata/influxdb/schema"

schema.measurementTagValues(
  bucket: {_flux_string(bucket)},
  measurement: {_flux_string(AIR_QUALITY_AGGREGATE_MEASUREMENT)},
  tag: "location",
  start: 0,
)
"""
    schema_environment_nodes = f"""import "influxdata/influxdb/schema"

schema.measurementTagValues(
  bucket: {_flux_string(bucket)},
  measurement: {_flux_string(ENVIRONMENT_MEASUREMENT)},
  tag: "node_id",
  start: 0,
)
"""
    targeted_offline_air = f"""from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(AIR_QUALITY_AGGREGATE_MEASUREMENT)})
  |> filter(fn: (r) => r.location == "office")
  |> filter(fn: (r) => contains(value: r._field, set: {_flux_array(aggregate_mean_fields)}))
  |> group(columns: ["_measurement", "node_id", "location", "topic", "sensor_type", "_field"])
  |> last()
"""
    return {
        "current_environment_inventory": environment_inventory,
        "current_air_inventory": air_inventory,
        "current_inventory_combined": (
            "environmentInventory = "
            + environment_inventory
            + "\nairQualityInventory = "
            + air_inventory
            + "\nunion(tables: [environmentInventory, airQualityInventory])\n"
        ),
        "current_environment_latest": environment_latest,
        "current_air_latest": air_latest,
        "current_latest_flux": latest_flux(bucket, live_bucket),
        "current_air_quality_context": air_quality_context_flux(bucket, live_bucket),
        "optimized_environment_inventory": optimized_environment_inventory,
        "optimized_air_inventory": optimized_air_inventory,
        "optimized_inventory_combined": optimized_inventory,
        "schema_air_locations": schema_air_locations,
        "schema_environment_nodes": schema_environment_nodes,
        "targeted_offline_air": targeted_offline_air,
    }


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def _measure(
    name: str, operation: Callable[[], list[Any]], runs: int
) -> dict[str, Any]:
    samples = []
    counts = []
    for _ in range(runs):
        started = time.perf_counter()
        rows = operation()
        samples.append(time.perf_counter() - started)
        counts.append(len(rows))
    return {
        "name": name,
        "runs": runs,
        "first_s": round(samples[0], 6),
        "median_s": round(statistics.median(samples), 6),
        "min_s": round(min(samples), 6),
        "max_s": round(max(samples), 6),
        "p95_s": round(_percentile(samples, 0.95), 6),
        "row_counts": sorted(set(counts)),
        "samples_s": [round(sample, 6) for sample in samples],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", nargs="*")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--report-startup", action="store_true")
    parser.add_argument("--verify-office", action="store_true")
    args = parser.parse_args()
    settings = load_settings(env_file=None)
    startup_started = time.perf_counter()
    repository = InfluxReadRepository(settings.influx)
    startup_seconds = time.perf_counter() - startup_started
    if args.report_startup:
        print(
            json.dumps(
                {"name": "repository_startup", "seconds": round(startup_seconds, 6)}
            ),
            flush=True,
        )
    variants = query_variants(settings.influx.bucket, settings.influx.live_bucket)
    labels = args.labels or list(variants)
    wsgi_client = None
    if any(label.startswith("wsgi_") for label in labels):
        from app.web import create_app

        app = create_app(
            settings,
            repository=repository,
            monitoring_store=_NoOpStore(),
            export_query_repository=object(),
            status_provider=object(),
            printer_repository=object(),
        )
        app.testing = True
        wsgi_client = app.test_client()
    try:
        for label in labels:
            if label == "current_latest_method":
                operation = lambda: [repository.latest()]
            elif label.startswith("http_"):
                path = label.removeprefix("http_")

                def operation(path: str = path) -> list[Any]:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:8080/api/{path}", timeout=30
                    ) as response:
                        return [response.read()]

            elif label.startswith("wsgi_"):
                path = label.removeprefix("wsgi_")

                def operation(path: str = path) -> list[Any]:
                    assert wsgi_client is not None
                    response = wsgi_client.get(f"/api/{path}")
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"WSGI request failed with {response.status_code}"
                        )
                    return [response.data]

            else:
                flux = variants[label]
                operation = lambda flux=flux: repository._query(flux)
            result = _measure(
                label,
                operation,
                args.runs,
            )
            print(json.dumps(result, sort_keys=True), flush=True)
        if args.verify_office:
            payload = (
                wsgi_client.get("/api/latest").get_json()
                if wsgi_client is not None
                else repository.latest()
            )
            office = next(
                (
                    station
                    for station in payload["air_quality"]
                    if station.get("location") == "office"
                ),
                None,
            )
            print(
                json.dumps(
                    {
                        "name": "office_semantics",
                        "station": office,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    finally:
        repository.close()


if __name__ == "__main__":
    main()
