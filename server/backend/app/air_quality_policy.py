"""Source-backed SEN66 interpretation and event policy.

The threshold metadata in this module is deliberately centralized.  API code,
the dashboard, event detection, tests, and documentation all consume the same
definitions so that a heuristic cannot silently masquerade as an official
health category.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping, Sequence


SEVERITIES = (
    "excellent",
    "good",
    "moderate",
    "poor",
    "very_poor",
    "hazardous",
    "informational",
    "unavailable",
)

EPA_PM_SOURCE = {
    "source_name": "U.S. Environmental Protection Agency",
    "source_document": "40 CFR Part 58, Appendix G — Uniform Air Quality Index (AQI) and 2024 PM NAAQS final rule",
    "source_revision": "2024-03-06",
    "source_url": "https://www.epa.gov/system/files/documents/2024-04/2024-pm-naaqs-fr-published.pdf",
}

WHO_PM_SOURCE = {
    "source_name": "World Health Organization",
    "source_document": "WHO global air quality guidelines: PM2.5, PM10, ozone, nitrogen dioxide, sulfur dioxide and carbon monoxide",
    "source_revision": "2021-09-22",
    "source_url": "https://www.who.int/publications/i/item/9789240034228",
}

SENSIRION_SOURCE = {
    "source_name": "Sensirion AG",
    "source_document": "SEN6x Datasheet D1 v0.92 and Sensirion VOC/NOx Index information notes",
    "source_revision": "2025-12",
    "source_url": "https://sensirion.com/media/documents/FAFC548D/693FBB15/PS_DS_SEN6x.pdf",
}

ASHRAE_CO2_SOURCE = {
    "source_name": "ASHRAE and CDC/NIOSH",
    "source_document": "ASHRAE Position Document on Indoor Carbon Dioxide, Ventilation and Indoor Air Quality; CDC/NIOSH Ventilation FAQ",
    "source_revision": "ASHRAE 2025-02-12; CDC page accessed 2026-07-21",
    "source_url": "https://www.ashrae.org/file%20library/about/position%20documents/pd_indoorcarbondioxide_2022.pdf",
}

NIOSH_CO2_SOURCE = {
    "source_name": "CDC/NIOSH and OSHA",
    "source_document": "NIOSH IDLH documentation for carbon dioxide and OSHA Table Z-1",
    "source_revision": "NIOSH 1994; OSHA table accessed 2026-07-21",
    "source_url": "https://www.cdc.gov/niosh/idlh/124389.html",
}

EPA_HUMIDITY_SOURCE = {
    "source_name": "U.S. Environmental Protection Agency",
    "source_document": "A Brief Guide to Mold, Moisture and Your Home",
    "source_revision": "accessed 2026-07-21",
    "source_url": "https://www.epa.gov/mold/brief-guide-mold-moisture-and-your-home",
}

ASHRAE_COMFORT_SOURCE = {
    "source_name": "ASHRAE",
    "source_document": "ANSI/ASHRAE Standard 55-2023 — Thermal Environmental Conditions for Human Occupancy",
    "source_revision": "2023",
    "source_url": "https://www.ashrae.org/technical-resources/bookstore/standard-55-thermal-environmental-conditions-for-human-occupancy",
}


# Official 24-hour EPA AQI concentration breakpoints.  PM2.5 is truncated to
# 0.1 ug/m3 and PM10 to integer ug/m3 before category selection.  These are
# category boundaries only; this project intentionally does not calculate an
# official AQI number.
EPA_PM_BREAKPOINTS: dict[str, tuple[tuple[float, str, str], ...]] = {
    "pm25": (
        (9.0, "Good", "good"),
        (35.4, "Moderate", "moderate"),
        (55.4, "Unhealthy for sensitive groups", "poor"),
        (125.4, "Unhealthy", "very_poor"),
        (225.4, "Very unhealthy", "very_poor"),
        (math.inf, "Hazardous", "hazardous"),
    ),
    "pm10": (
        (54.0, "Good", "good"),
        (154.0, "Moderate", "moderate"),
        (254.0, "Unhealthy for sensitive groups", "poor"),
        (354.0, "Unhealthy", "very_poor"),
        (424.0, "Very unhealthy", "very_poor"),
        (math.inf, "Hazardous", "hazardous"),
    ),
}

WHO_PM_24H_GUIDELINES = {"pm25": 15.0, "pm10": 45.0}

# Dashboard-only ventilation bands.  ASHRAE explicitly says Standard 62.1
# does not define a universal indoor CO2 limit; these bands are conservative
# operational cues, not toxicity thresholds or an ASHRAE category scale.
CO2_VENTILATION_BANDS = (
    (800.0, "Ventilation appears effective", "good"),
    (1000.0, "Ventilation watch", "moderate"),
    (1500.0, "Ventilation recommended", "poor"),
    (math.inf, "Strong ventilation recommended", "very_poor"),
)

VOC_BANDS = (
    (69.0, "Below learned recent background", "informational"),
    (130.0, "Near learned recent background", "good"),
    (149.0, "Increased relative VOC activity", "moderate"),
    (math.inf, "Sensirion example action level reached", "poor"),
)

NOX_BANDS = (
    (1.0, "Near normal NOx Index baseline", "good"),
    (9.0, "NOx-related activity detected", "informational"),
    (19.0, "Elevated relative NOx activity", "moderate"),
    (math.inf, "Sensirion example action level reached", "poor"),
)

VOC_ACTION_THRESHOLD = 150.0
NOX_ACTION_THRESHOLD = 20.0
VOC_RELIABLE_EVENT_SECONDS = 60
VOC_SPECIFICATION_SECONDS = 60 * 60
NOX_RELIABLE_EVENT_SECONDS = 5 * 60
NOX_SPECIFICATION_SECONDS = 6 * 60 * 60
PM_SENSOR_STABLE_SECONDS = 30


@dataclass(frozen=True)
class Interpretation:
    metric: str
    raw_value: float | int | None
    unit: str
    evaluated_value: float | int | None
    evaluated_window: str
    category: str
    severity: str
    framework: str
    status_type: str
    is_official_category: bool
    source_name: str
    source_document: str
    source_revision: str
    source_url: str
    explanation: str
    limitation: str
    data_coverage: Mapping[str, Any] | None
    is_stale: bool
    is_warming_up: bool
    updated_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventRule:
    event_type: str
    metric: str
    trigger: float
    clear: float
    severity: str
    framework: str
    status_type: str
    source_name: str
    source_document: str
    baseline_mode: str = "threshold"
    evaluated_window: str = "latest_accepted_publish"
    cooldown_seconds: int = 15 * 60


EVENT_RULES = (
    EventRule(
        "voc_action_level",
        "voc_index",
        VOC_ACTION_THRESHOLD,
        130.0,
        "poor",
        "Sensirion VOC Index",
        "manufacturer_index_interpretation",
        SENSIRION_SOURCE["source_name"],
        SENSIRION_SOURCE["source_document"],
    ),
    EventRule(
        "nox_action_level",
        "nox_index",
        NOX_ACTION_THRESHOLD,
        15.0,
        "poor",
        "Sensirion NOx Index",
        "manufacturer_index_interpretation",
        SENSIRION_SOURCE["source_name"],
        SENSIRION_SOURCE["source_document"],
    ),
    EventRule(
        "pm25_current_level",
        "pm25",
        35.5,
        30.0,
        "poor",
        "EPA 24-hour breakpoint used as current-level context",
        "dashboard_heuristic",
        EPA_PM_SOURCE["source_name"],
        EPA_PM_SOURCE["source_document"],
    ),
    EventRule(
        "pm10_current_level",
        "pm10",
        155.0,
        140.0,
        "poor",
        "EPA 24-hour breakpoint used as current-level context",
        "dashboard_heuristic",
        EPA_PM_SOURCE["source_name"],
        EPA_PM_SOURCE["source_document"],
    ),
    EventRule(
        "co2_ventilation",
        "co2",
        1000.0,
        900.0,
        "poor",
        "ASHRAE-informed dashboard ventilation indicator",
        "ventilation_indicator",
        ASHRAE_CO2_SOURCE["source_name"],
        ASHRAE_CO2_SOURCE["source_document"],
    ),
    EventRule(
        "voc_rapid_rise",
        "voc_index",
        50.0,
        25.0,
        "moderate",
        "Dashboard change detector",
        "dashboard_heuristic",
        SENSIRION_SOURCE["source_name"],
        SENSIRION_SOURCE["source_document"],
        baseline_mode="delta",
        evaluated_window="change_from_previous_accepted_publish",
    ),
    EventRule(
        "nox_rapid_rise",
        "nox_index",
        10.0,
        5.0,
        "moderate",
        "Dashboard change detector",
        "dashboard_heuristic",
        SENSIRION_SOURCE["source_name"],
        SENSIRION_SOURCE["source_document"],
        baseline_mode="delta",
        evaluated_window="change_from_previous_accepted_publish",
    ),
    EventRule(
        "pm25_rapid_rise",
        "pm25",
        15.0,
        7.5,
        "moderate",
        "Dashboard change detector",
        "dashboard_heuristic",
        EPA_PM_SOURCE["source_name"],
        EPA_PM_SOURCE["source_document"],
        baseline_mode="delta",
        evaluated_window="change_from_previous_accepted_publish",
    ),
    EventRule(
        "pm10_rapid_rise",
        "pm10",
        30.0,
        15.0,
        "moderate",
        "Dashboard change detector",
        "dashboard_heuristic",
        EPA_PM_SOURCE["source_name"],
        EPA_PM_SOURCE["source_document"],
        baseline_mode="delta",
        evaluated_window="change_from_previous_accepted_publish",
    ),
    EventRule(
        "co2_rapid_rise",
        "co2",
        200.0,
        100.0,
        "moderate",
        "Dashboard change detector",
        "dashboard_heuristic",
        ASHRAE_CO2_SOURCE["source_name"],
        ASHRAE_CO2_SOURCE["source_document"],
        baseline_mode="delta",
        evaluated_window="change_from_previous_accepted_publish",
    ),
)

SENSOR_INVALID_EVENT_RULE = EventRule(
    "sensor_invalid",
    "sensor_state",
    1.0,
    0.0,
    "poor",
    "SEN66 validation",
    "unavailable",
    "Sensirion AG",
    "SEN6x Datasheet D1 v0.92",
    evaluated_window="latest_packet_validation_state",
    cooldown_seconds=60,
)


def interpret_station(
    station: Mapping[str, Any],
    *,
    summary_15m: Mapping[str, Any] | None = None,
    rolling_24h: Mapping[str, Any] | None = None,
    stale_after_seconds: int = 20,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Interpret every user-facing SEN66 value for one station."""

    now = now or datetime.now(timezone.utc)
    updated_at = _string_or_none(station.get("last_seen"))
    age = _age_seconds(updated_at, now)
    # Fresh-but-invalid and stale are distinct states.  The latest-response
    # assembler nulls every primary field for an invalid packet, so the
    # interpretation becomes unavailable without mislabeling it as stale.
    stale = age is None or age > stale_after_seconds
    sensor_uptime = _finite_or_none(station.get("sensor_uptime_s"))
    invalid_sample = station.get("sample_valid") is False

    def current_value(field: str) -> Any:
        return None if invalid_sample else station.get(field)

    current_summary = None if invalid_sample else summary_15m

    result = {
        "temperature_c": _interpret_temperature(current_value("temperature_c"), stale, updated_at),
        "humidity": _interpret_humidity(current_value("humidity"), stale, updated_at),
        "co2": _interpret_co2(current_value("co2"), stale, updated_at),
        "co2_occupational": _interpret_co2_occupational(
            current_value("co2"), current_summary, stale, updated_at
        ),
        "pm1": _informational_pm(current_value("pm1"), "pm1", stale, updated_at),
        "pm4": _informational_pm(current_value("pm4"), "pm4", stale, updated_at),
        "pm25_current": _interpret_pm_current(current_value("pm25"), "pm25", stale, updated_at),
        "pm10_current": _interpret_pm_current(current_value("pm10"), "pm10", stale, updated_at),
        "pm25_24h": _interpret_pm_24h(current_value("pm25"), "pm25", rolling_24h, stale, updated_at),
        "pm10_24h": _interpret_pm_24h(current_value("pm10"), "pm10", rolling_24h, stale, updated_at),
        "pm25_who_24h": _interpret_who_pm(current_value("pm25"), "pm25", rolling_24h, stale, updated_at),
        "pm10_who_24h": _interpret_who_pm(current_value("pm10"), "pm10", rolling_24h, stale, updated_at),
        "voc_index": _interpret_voc(
            current_value("voc_index"), sensor_uptime, stale, updated_at
        ),
        "nox_index": _interpret_nox(
            current_value("nox_index"), sensor_uptime, stale, updated_at
        ),
    }
    return {key: value.as_dict() for key, value in result.items()}


def epa_pm_category(metric: str, value: float) -> tuple[str, str, float]:
    """Return category, severity, and EPA-prescribed truncated value."""

    if metric not in EPA_PM_BREAKPOINTS:
        raise ValueError(f"unsupported EPA PM metric: {metric}")
    evaluated = _truncate(value, 1 if metric == "pm25" else 0)
    for upper, category, severity in EPA_PM_BREAKPOINTS[metric]:
        if evaluated <= upper:
            return category, severity, evaluated
    raise AssertionError("infinite breakpoint must match")


def rolling_24h_status(
    windows: Sequence[Mapping[str, Any]],
    *,
    expected_publish_seconds: int = 5,
    minimum_coverage_percent: float = 75.0,
    minimum_span_hours: float = 20.0,
) -> dict[str, Any]:
    """Calculate coverage-aware PM rolling status from aligned aggregates.

    The period ends at the newest completed aggregate window and covers the
    preceding 24 hours.  Means are weighted by valid sample count, which is a
    practical time-weighted approximation for a regular five-second stream.
    """

    completed = [window for window in windows if not bool(window.get("is_partial"))]
    completed.sort(key=lambda row: str(row.get("window_end", "")))
    if not completed:
        return _empty_24h(expected_publish_seconds)

    newest_end = _parse_time(completed[-1].get("window_end"))
    if newest_end is None:
        return _empty_24h(expected_publish_seconds)
    period_start = newest_end.timestamp() - (24 * 60 * 60)
    included = []
    for window in completed:
        start = _parse_time(window.get("window_start"))
        end = _parse_time(window.get("window_end"))
        if start is None or end is None:
            continue
        if end.timestamp() > period_start and end <= newest_end:
            included.append(window)

    expected = int((24 * 60 * 60) / expected_publish_seconds)
    valid = sum(_nonnegative_int(row.get("valid_sample_count")) for row in included)
    coverage = min(100.0, (valid / expected * 100.0) if expected else 0.0)
    starts = [_parse_time(row.get("window_start")) for row in included]
    ends = [_parse_time(row.get("window_end")) for row in included]
    starts = [value for value in starts if value is not None]
    ends = [value for value in ends if value is not None]
    span_hours = (
        (max(ends) - min(starts)).total_seconds() / 3600.0 if starts and ends else 0.0
    )
    sufficient = (
        coverage >= minimum_coverage_percent
        and span_hours >= minimum_span_hours
        and len(included) >= int(96 * minimum_coverage_percent / 100.0)
    )

    response: dict[str, Any] = {
        "evaluated_window": "rolling_24h_ending_at_latest_completed_15m_window",
        "sample_coverage_percent": round(coverage, 1),
        "expected_sample_count": expected,
        "valid_sample_count": valid,
        "oldest_included_timestamp": _iso(min(starts)) if starts else None,
        "newest_included_timestamp": _iso(max(ends)) if ends else None,
        "included_window_count": len(included),
        "expected_window_count": 96,
        "span_hours": round(span_hours, 2),
        "is_sufficient": sufficient,
        "insufficient_reason": None,
    }
    if not sufficient:
        response["insufficient_reason"] = (
            f"requires at least {minimum_coverage_percent:.0f}% sample coverage, "
            f"{minimum_span_hours:.0f} hours of span, and 72 aligned windows"
        )

    for metric in ("pm25", "pm10"):
        weighted_sum = 0.0
        weight = 0
        for row in included:
            value = _finite_or_none(row.get(f"{metric}_mean"))
            row_valid = _nonnegative_int(row.get("valid_sample_count"))
            if value is None or row_valid <= 0:
                continue
            weighted_sum += value * row_valid
            weight += row_valid
        response[f"{metric}_average"] = (
            round(weighted_sum / weight, 3) if sufficient and weight else None
        )
    return response


def _empty_24h(expected_publish_seconds: int) -> dict[str, Any]:
    return {
        "evaluated_window": "rolling_24h_ending_at_latest_completed_15m_window",
        "sample_coverage_percent": 0.0,
        "expected_sample_count": int(86400 / expected_publish_seconds),
        "valid_sample_count": 0,
        "oldest_included_timestamp": None,
        "newest_included_timestamp": None,
        "included_window_count": 0,
        "expected_window_count": 96,
        "span_hours": 0.0,
        "is_sufficient": False,
        "insufficient_reason": "no completed 15-minute aggregate windows",
        "pm25_average": None,
        "pm10_average": None,
    }


def _interpret_pm_current(
    value: Any, metric: str, stale: bool, updated_at: str | None
) -> Interpretation:
    numeric = _finite_or_none(value)
    if numeric is None or numeric < 0 or stale:
        return _unavailable(metric, value, "µg/m³", stale, updated_at, EPA_PM_SOURCE)
    category, severity, evaluated = epa_pm_category(metric, numeric)
    name = "PM2.5" if metric == "pm25" else "PM10"
    return Interpretation(
        metric,
        numeric,
        "µg/m³",
        evaluated,
        "latest_sample",
        f"{category} boundary context (provisional)",
        severity,
        "EPA 24-hour AQI concentration boundaries used as current-level context",
        "dashboard_heuristic",
        False,
        **EPA_PM_SOURCE,
        explanation=(
            f"Current {name} is placed beside EPA concentration boundaries for practical context; "
            f"the provisional {category} range is {_epa_range_label(metric, category)}."
        ),
        limitation="This is a single indoor sensor sample, not a 24-hour regulatory monitor result or official AQI.",
        data_coverage=None,
        is_stale=False,
        is_warming_up=False,
        updated_at=updated_at,
    )


def _interpret_pm_24h(
    raw: Any,
    metric: str,
    rolling: Mapping[str, Any] | None,
    stale: bool,
    updated_at: str | None,
) -> Interpretation:
    coverage = dict(rolling or _empty_24h(5))
    average = _finite_or_none(coverage.get(f"{metric}_average"))
    if stale or not coverage.get("is_sufficient") or average is None:
        result = _unavailable(metric, raw, "µg/m³", stale, updated_at, EPA_PM_SOURCE)
        return Interpretation(
            **{
                **result.as_dict(),
                "evaluated_window": "rolling_24h",
                "category": "Insufficient 24-hour history" if not stale else "Stale",
                "framework": "Estimated EPA 24-hour category",
                "status_type": "unavailable",
                "explanation": "The dashboard withholds a category until the coverage requirement is met.",
                "limitation": str(coverage.get("insufficient_reason") or result.limitation),
                "data_coverage": coverage,
            }
        )
    category, severity, evaluated = epa_pm_category(metric, average)
    return Interpretation(
        metric,
        _finite_or_none(raw),
        "µg/m³",
        evaluated,
        "rolling_24h_ending_at_latest_completed_15m_window",
        category,
        severity,
        "Estimated EPA 24-hour concentration category",
        "official_aqi_category",
        True,
        **EPA_PM_SOURCE,
        explanation=(
            "The category names and boundaries are official EPA AQI categories applied to a "
            f"coverage-qualified estimate; the {category} range is {_epa_range_label(metric, category)}."
        ),
        limitation="This is an indoor low-cost sensor estimate; it is not a regulatory monitor result and no official AQI number is calculated.",
        data_coverage=coverage,
        is_stale=False,
        is_warming_up=False,
        updated_at=updated_at,
    )


def _interpret_who_pm(
    raw: Any,
    metric: str,
    rolling: Mapping[str, Any] | None,
    stale: bool,
    updated_at: str | None,
) -> Interpretation:
    coverage = dict(rolling or _empty_24h(5))
    average = _finite_or_none(coverage.get(f"{metric}_average"))
    guideline = WHO_PM_24H_GUIDELINES[metric]
    if stale or not coverage.get("is_sufficient") or average is None:
        result = _unavailable(metric, raw, "µg/m³", stale, updated_at, WHO_PM_SOURCE)
        return Interpretation(
            **{
                **result.as_dict(),
                "evaluated_window": "rolling_24h",
                "category": "Insufficient 24-hour history" if not stale else "Stale",
                "framework": "WHO 2021 24-hour guideline comparison",
                "explanation": "The WHO comparison is withheld until rolling coverage is sufficient.",
                "data_coverage": coverage,
            }
        )
    category = "At or below WHO 24-hour guideline" if average <= guideline else "Above WHO 24-hour guideline"
    severity = "good" if average <= guideline else "poor"
    return Interpretation(
        metric,
        _finite_or_none(raw),
        "µg/m³",
        round(average, 3),
        "rolling_24h",
        category,
        severity,
        "WHO 2021 24-hour air quality guideline",
        "official_guideline_comparison",
        False,
        **WHO_PM_SOURCE,
        explanation=f"Compared with the WHO 2021 24-hour guideline of {guideline:g} µg/m³.",
        limitation="WHO also publishes a separate annual guideline; this dashboard does not substitute the 24-hour value for the annual value.",
        data_coverage=coverage,
        is_stale=False,
        is_warming_up=False,
        updated_at=updated_at,
    )


def _informational_pm(
    value: Any, metric: str, stale: bool, updated_at: str | None
) -> Interpretation:
    numeric = _finite_or_none(value)
    if numeric is None or numeric < 0 or stale:
        return _unavailable(metric, value, "µg/m³", stale, updated_at, SENSIRION_SOURCE)
    if metric == "pm1":
        explanation = "PM1.0 is the very-fine subset of the reported PM2.5 mass fraction."
    else:
        explanation = "PM4.0 is the respirable particle fraction reported by the SEN66."
    return Interpretation(
        metric,
        numeric,
        "µg/m³",
        numeric,
        "latest_sample",
        "Informational trend",
        "informational",
        "No separate U.S. EPA PM1.0 or PM4.0 AQI category",
        "informational_only",
        False,
        **SENSIRION_SOURCE,
        explanation=explanation,
        limitation="No applicable official public-health category is assigned; use the raw value and trend with PM2.5/PM10 context.",
        data_coverage=None,
        is_stale=False,
        is_warming_up=False,
        updated_at=updated_at,
    )


def _interpret_co2(value: Any, stale: bool, updated_at: str | None) -> Interpretation:
    numeric = _finite_or_none(value)
    if numeric is None or numeric < 0 or stale:
        return _unavailable("co2", value, "ppm", stale, updated_at, ASHRAE_CO2_SOURCE)
    category, severity = _band(CO2_VENTILATION_BANDS, numeric)
    return Interpretation(
        "co2",
        numeric,
        "ppm",
        numeric,
        "latest_sample_with_15m_context",
        category,
        severity,
        "ASHRAE-informed ventilation indicator",
        "ventilation_indicator",
        False,
        **ASHRAE_CO2_SOURCE,
        explanation=(
            "CO₂ from occupants can indicate whether ventilation is keeping pace: dashboard bands are "
            "≤800, 801–1,000, 1,001–1,500, and >1,500 ppm."
        ),
        limitation="ASHRAE does not define a universal indoor CO₂ limit. This dashboard scale is a heuristic, and CO₂ does not represent every indoor pollutant.",
        data_coverage=None,
        is_stale=False,
        is_warming_up=False,
        updated_at=updated_at,
    )


def _interpret_co2_occupational(
    value: Any,
    summary_15m: Mapping[str, Any] | None,
    stale: bool,
    updated_at: str | None,
) -> Interpretation:
    numeric = _finite_or_none(value)
    max_15m = _finite_or_none((summary_15m or {}).get("co2_max"))
    evaluated = max_15m if max_15m is not None else numeric
    if evaluated is None or evaluated < 0 or stale:
        return _unavailable("co2", value, "ppm", stale, updated_at, NIOSH_CO2_SOURCE)
    if evaluated >= 40000:
        category, severity = "At or above NIOSH IDLH numeric value", "hazardous"
    elif evaluated >= 30000:
        category, severity = "At or above NIOSH 15-minute STEL numeric value", "hazardous"
    elif evaluated >= 5000:
        category, severity = "At or above occupational 8-hour TWA numeric value", "very_poor"
    else:
        category, severity = "Below occupational exposure-limit numeric values", "informational"
    return Interpretation(
        "co2",
        numeric,
        "ppm",
        evaluated,
        "15m_max" if max_15m is not None else "latest_sample",
        category,
        severity,
        "NIOSH/OSHA occupational exposure context",
        "occupational_limit_comparison",
        False,
        **NIOSH_CO2_SOURCE,
        explanation="NIOSH REL/OSHA PEL: 5,000 ppm time-weighted average; NIOSH STEL: 30,000 ppm for 15 minutes; NIOSH IDLH: 40,000 ppm.",
        limitation="Residential ventilation comfort and occupational toxicity are different questions. A maximum or single sample is not an 8-hour TWA.",
        data_coverage=None,
        is_stale=False,
        is_warming_up=False,
        updated_at=updated_at,
    )


def _interpret_voc(
    value: Any, uptime: float | None, stale: bool, updated_at: str | None
) -> Interpretation:
    numeric = _finite_or_none(value)
    warming = uptime is not None and uptime < VOC_SPECIFICATION_SECONDS
    if numeric is None or not 1 <= numeric <= 500 or stale:
        return _unavailable("voc_index", value, "index", stale, updated_at, SENSIRION_SOURCE, warming)
    category, severity = _band(VOC_BANDS, numeric)
    manufacturer_threshold = numeric >= VOC_ACTION_THRESHOLD
    return Interpretation(
        "voc_index",
        numeric,
        "index",
        numeric,
        "latest_sample_relative_to_adaptive_24h_history",
        category,
        severity,
        (
            "Sensirion VOC Index example action threshold"
            if manufacturer_threshold
            else "Dashboard band using the Sensirion adaptive VOC Index"
        ),
        (
            "manufacturer_index_interpretation"
            if manufacturer_threshold
            else "dashboard_heuristic"
        ),
        False,
        **SENSIRION_SOURCE,
        explanation="100 represents learned recent VOC background and 150 is Sensirion's example action threshold; above/below 100 means more/less relative activity, not a concentration.",
        limitation="The index is dimensionless, cannot identify a chemical, and cannot be converted here to ppm, ppb, or µg/m³. Sustained pollution can become part of the adaptive baseline.",
        data_coverage=None,
        is_stale=False,
        is_warming_up=warming,
        updated_at=updated_at,
    )


def _interpret_nox(
    value: Any, uptime: float | None, stale: bool, updated_at: str | None
) -> Interpretation:
    numeric = _finite_or_none(value)
    warming = uptime is not None and uptime < NOX_SPECIFICATION_SECONDS
    if numeric is None or not 1 <= numeric <= 500 or stale:
        return _unavailable("nox_index", value, "index", stale, updated_at, SENSIRION_SOURCE, warming)
    category, severity = _band(NOX_BANDS, numeric)
    if numeric >= NOX_ACTION_THRESHOLD or numeric == 1:
        framework = "Sensirion NOx Index baseline/example action threshold"
        status_type = "manufacturer_index_interpretation"
    else:
        framework = "Dashboard band using the Sensirion NOx Index"
        status_type = "dashboard_heuristic"
    return Interpretation(
        "nox_index",
        numeric,
        "index",
        numeric,
        "latest_sample_relative_to_recent_history",
        category,
        severity,
        framework,
        status_type,
        False,
        **SENSIRION_SOURCE,
        explanation="The normal baseline is near 1; values above 1 indicate increasing oxidizing-gas activity. Sensirion gives 20 as an example device-control trigger.",
        limitation="The dimensionless NOx Index is not NO₂ concentration and cannot be compared with EPA/WHO NO₂ concentration limits.",
        data_coverage=None,
        is_stale=False,
        is_warming_up=warming,
        updated_at=updated_at,
    )


def _interpret_temperature(value: Any, stale: bool, updated_at: str | None) -> Interpretation:
    numeric = _finite_or_none(value)
    if numeric is None or not -10 <= numeric <= 50 or stale:
        return _unavailable("temperature_c", value, "°C", stale, updated_at, ASHRAE_COMFORT_SOURCE)
    if numeric < 20:
        category, severity = "Below dashboard comfort range", "moderate"
    elif numeric <= 26:
        category, severity = "Within dashboard comfort range", "good"
    else:
        category, severity = "Above dashboard comfort range", "moderate"
    return Interpretation(
        "temperature_c",
        numeric,
        "°C",
        numeric,
        "latest_sample",
        category,
        severity,
        "ASHRAE-informed dashboard comfort range",
        "comfort_range",
        False,
        **ASHRAE_COMFORT_SOURCE,
        explanation="20–26 °C is used as a simple residential dashboard comfort cue.",
        limitation="ASHRAE comfort depends on clothing, activity, air speed, radiant temperature, humidity, season, and occupant preference; the band is a heuristic, not a hazard limit.",
        data_coverage=None,
        is_stale=False,
        is_warming_up=False,
        updated_at=updated_at,
    )


def _interpret_humidity(value: Any, stale: bool, updated_at: str | None) -> Interpretation:
    numeric = _finite_or_none(value)
    if numeric is None or not 0 <= numeric <= 90 or stale:
        return _unavailable("humidity", value, "%RH", stale, updated_at, EPA_HUMIDITY_SOURCE)
    if numeric < 30:
        category, severity = "Below EPA ideal range", "moderate"
    elif numeric <= 50:
        category, severity = "Within EPA ideal range", "good"
    elif numeric < 60:
        category, severity = "Above ideal; below moisture-control ceiling", "moderate"
    else:
        category, severity = "Above EPA moisture-control recommendation", "poor"
    return Interpretation(
        "humidity",
        numeric,
        "%RH",
        numeric,
        "latest_sample",
        category,
        severity,
        "EPA residential moisture guidance",
        "comfort_range",
        False,
        **EPA_HUMIDITY_SOURCE,
        explanation="EPA recommends keeping indoor RH below 60%, ideally 30–50%, to help control moisture and mold.",
        limitation="This is human-building moisture guidance, not a filament-storage specification. SEN66 recommended operation is 20–80% RH, non-condensing.",
        data_coverage=None,
        is_stale=False,
        is_warming_up=False,
        updated_at=updated_at,
    )


def _unavailable(
    metric: str,
    raw: Any,
    unit: str,
    stale: bool,
    updated_at: str | None,
    source: Mapping[str, str],
    warming: bool = False,
) -> Interpretation:
    return Interpretation(
        metric,
        _finite_or_none(raw),
        unit,
        None,
        "unavailable",
        "Stale" if stale else "Unavailable",
        "unavailable",
        "No valid current interpretation",
        "unavailable",
        False,
        source_name=source["source_name"],
        source_document=source["source_document"],
        source_revision=source["source_revision"],
        source_url=source["source_url"],
        explanation="A valid, recent value is required before assigning a status.",
        limitation="Missing, invalid, non-finite, out-of-range, or stale values are never converted to zero or a reassuring category.",
        data_coverage=None,
        is_stale=stale,
        is_warming_up=warming,
        updated_at=updated_at,
    )


def _band(bands: Iterable[tuple[float, str, str]], value: float) -> tuple[str, str]:
    for upper, category, severity in bands:
        if value <= upper:
            return category, severity
    raise AssertionError("infinite band must match")


def _truncate(value: float, digits: int) -> float:
    factor = 10**digits
    return math.floor(value * factor) / factor


def _epa_range_label(metric: str, category: str) -> str:
    digits = 1 if metric == "pm25" else 0
    lower = 0.0
    for upper, candidate, _severity in EPA_PM_BREAKPOINTS[metric]:
        if candidate == category:
            unit_format = f"{{:.{digits}f}}"
            if math.isinf(upper):
                return f"{unit_format.format(lower)} µg/m³ or above (24-hour basis)"
            return (
                f"{unit_format.format(lower)}–{unit_format.format(upper)} µg/m³ "
                "(24-hour basis)"
            )
        lower = upper + (0.1 if metric == "pm25" else 1.0)
    raise ValueError(f"unknown EPA category for {metric}: {category}")


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _string_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _age_seconds(value: str | None, now: datetime) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now.astimezone(timezone.utc) - parsed).total_seconds())


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
