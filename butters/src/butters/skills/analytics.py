"""Small deterministic statistics over bounded, already-filtered sensor series."""

from __future__ import annotations

import math
import statistics
from datetime import datetime


def summarize_points(
    points: tuple[dict[str, object], ...] | list[dict[str, object]],
    metrics: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    summaries: dict[str, dict[str, object]] = {}
    for metric in metrics:
        samples = [
            (str(point.get("time")), float(point[metric]))
            for point in points
            if isinstance(point.get(metric), (int, float))
            and not isinstance(point.get(metric), bool)
            and math.isfinite(float(point[metric]))
        ]
        if not samples:
            summaries[metric] = {"count": 0, "available": False}
            continue
        values = [value for _time, value in samples]
        start = values[0]
        end = values[-1]
        delta = end - start
        summaries[metric] = {
            "count": len(values),
            "available": True,
            "minimum": _round(min(values)),
            "maximum": _round(max(values)),
            "mean": _round(statistics.fmean(values)),
            "median": _round(statistics.median(values)),
            "start_value": _round(start),
            "end_value": _round(end),
            "absolute_delta": _round(delta),
            "percentage_delta": (
                None if abs(start) < 1e-12 else _round(delta / abs(start) * 100)
            ),
            "slope_per_hour": _round(_slope_per_hour(samples)),
        }
    return summaries


def compare_summaries(
    first: dict[str, dict[str, object]],
    second: dict[str, dict[str, object]],
    metrics: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for metric in metrics:
        left = first.get(metric, {})
        right = second.get(metric, {})
        left_mean = _number(left.get("mean"))
        right_mean = _number(right.get("mean"))
        delta = (
            None if left_mean is None or right_mean is None else right_mean - left_mean
        )
        result[metric] = {
            "first": left,
            "second": right,
            "mean_delta": _round(delta),
            "mean_percentage_delta": (
                None
                if delta is None or left_mean is None or abs(left_mean) < 1e-12
                else _round(delta / abs(left_mean) * 100)
            ),
            "evidence_quality": _evidence_quality(
                int(left.get("count", 0) or 0), int(right.get("count", 0) or 0)
            ),
        }
    return result


def detect_spike(
    points: tuple[dict[str, object], ...] | list[dict[str, object]], metric: str
) -> dict[str, object]:
    samples = [
        (str(point.get("time")), float(point[metric]))
        for point in points
        if isinstance(point.get(metric), (int, float))
        and not isinstance(point.get(metric), bool)
        and math.isfinite(float(point[metric]))
    ]
    if len(samples) < 3:
        return {
            "metric": metric,
            "sample_count": len(samples),
            "available": False,
            "reason": "at least three samples are required",
        }
    baseline = statistics.median(value for _stamp, value in samples)
    stamp, peak = max(samples, key=lambda item: abs(item[1] - baseline))
    return {
        "metric": metric,
        "sample_count": len(samples),
        "available": True,
        "baseline_median": _round(baseline),
        "spike_value": _round(peak),
        "spike_magnitude": _round(peak - baseline),
        "spike_time": stamp,
        "method": "largest absolute deviation from the window median",
    }


def correlate(
    points: tuple[dict[str, object], ...] | list[dict[str, object]],
    metric_x: str,
    metric_y: str,
) -> dict[str, object]:
    pairs = [
        (float(point[metric_x]), float(point[metric_y]))
        for point in points
        if isinstance(point.get(metric_x), (int, float))
        and not isinstance(point.get(metric_x), bool)
        and isinstance(point.get(metric_y), (int, float))
        and not isinstance(point.get(metric_y), bool)
    ]
    if len(pairs) < 5:
        return {
            "metrics": [metric_x, metric_y],
            "sample_count": len(pairs),
            "available": False,
            "reason": "at least five paired samples are required",
            "causal": False,
        }
    xs, ys = zip(*pairs, strict=True)
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    denominator = math.sqrt(
        sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys)
    )
    coefficient = None if denominator == 0 else numerator / denominator
    return {
        "metrics": [metric_x, metric_y],
        "sample_count": len(pairs),
        "available": coefficient is not None,
        "pearson_r": _round(coefficient),
        "strength": _correlation_strength(coefficient),
        "causal": False,
        "limitation": "correlation alone does not establish causation",
    }


def _slope_per_hour(samples: list[tuple[str, float]]) -> float | None:
    if len(samples) < 2:
        return None
    try:
        times = [
            datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            for stamp, _value in samples
        ]
    except ValueError:
        return None
    origin = times[0]
    hours = [(value - origin) / 3600 for value in times]
    mean_x = statistics.fmean(hours)
    mean_y = statistics.fmean(value for _stamp, value in samples)
    denominator = sum((value - mean_x) ** 2 for value in hours)
    if denominator == 0:
        return None
    return (
        sum(
            (x - mean_x) * (sample[1] - mean_y)
            for x, sample in zip(hours, samples, strict=True)
        )
        / denominator
    )


def _correlation_strength(value: float | None) -> str:
    if value is None:
        return "unknown"
    magnitude = abs(value)
    if magnitude < 0.3:
        return "weak"
    if magnitude < 0.7:
        return "moderate"
    return "strong"


def _evidence_quality(first: int, second: int) -> str:
    minimum = min(first, second)
    if minimum < 3:
        return "insufficient"
    if minimum < 10:
        return "limited"
    return "adequate"


def _number(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _round(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, 6)
