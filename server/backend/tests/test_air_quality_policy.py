from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from app.air_quality_policy import (
    EPA_PM_BREAKPOINTS,
    epa_pm_category,
    interpret_station,
    rolling_24h_status,
)


NOW = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


def station(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "last_seen": "2026-07-21T17:59:55Z",
        "sample_valid": True,
        "temperature_c": 23.0,
        "humidity": 45.0,
        "co2": 700,
        "pm1": 3.0,
        "pm25": 8.0,
        "pm4": 5.0,
        "pm10": 20.0,
        "voc_index": 100,
        "nox_index": 1,
        "sensor_uptime_s": 30_000,
    }
    result.update(overrides)
    return result


def complete_windows(pm25: float = 8.0, pm10: float = 20.0) -> list[dict[str, object]]:
    start = NOW - timedelta(hours=24)
    return [
        {
            "window_start": (start + timedelta(minutes=15 * index)).isoformat(),
            "window_end": (start + timedelta(minutes=15 * (index + 1))).isoformat(),
            "valid_sample_count": 180,
            "expected_sample_count": 180,
            "pm25_mean": pm25,
            "pm10_mean": pm10,
            "is_partial": False,
        }
        for index in range(96)
    ]


class AirQualityPolicyTest(unittest.TestCase):
    def test_every_epa_particulate_boundary(self) -> None:
        for metric, breakpoints in EPA_PM_BREAKPOINTS.items():
            finite = [row for row in breakpoints if row[0] != float("inf")]
            step = 0.1 if metric == "pm25" else 1.0
            for index, (upper, category, _severity) in enumerate(finite):
                with self.subTest(metric=metric, upper=upper):
                    self.assertEqual(epa_pm_category(metric, upper)[0], category)
                    self.assertEqual(
                        epa_pm_category(metric, upper + step)[0],
                        breakpoints[index + 1][1],
                    )

    def test_pm25_2024_good_boundary_uses_required_truncation(self) -> None:
        self.assertEqual(epa_pm_category("pm25", 9.09)[:2], ("Good", "good"))
        self.assertEqual(epa_pm_category("pm25", 9.1)[0], "Moderate")

    def test_current_pm_is_not_official_but_covered_24h_category_is(self) -> None:
        rolling = rolling_24h_status(complete_windows(pm25=9.1))
        results = interpret_station(station(pm25=9.1), rolling_24h=rolling, now=NOW)

        self.assertEqual(results["pm25_current"]["status_type"], "dashboard_heuristic")
        self.assertFalse(results["pm25_current"]["is_official_category"])
        self.assertEqual(results["pm25_24h"]["category"], "Moderate")
        self.assertTrue(results["pm25_24h"]["is_official_category"])

    def test_insufficient_24h_history_withholds_category(self) -> None:
        rolling = rolling_24h_status(complete_windows()[:10])
        result = interpret_station(station(), rolling_24h=rolling, now=NOW)["pm25_24h"]

        self.assertEqual(result["severity"], "unavailable")
        self.assertIn("Insufficient", result["category"])
        self.assertFalse(result["data_coverage"]["is_sufficient"])

    def test_epa_and_who_frameworks_remain_distinct(self) -> None:
        rolling = rolling_24h_status(complete_windows(pm25=16.0, pm10=46.0))
        results = interpret_station(station(), rolling_24h=rolling, now=NOW)

        self.assertIn("EPA", results["pm25_24h"]["framework"])
        self.assertIn("WHO", results["pm25_who_24h"]["framework"])
        self.assertIn("Above WHO", results["pm25_who_24h"]["category"])

    def test_who_24_hour_guideline_boundaries(self) -> None:
        at_guideline = interpret_station(
            station(), rolling_24h=rolling_24h_status(complete_windows(15.0, 45.0)), now=NOW
        )
        above_guideline = interpret_station(
            station(), rolling_24h=rolling_24h_status(complete_windows(15.1, 45.1)), now=NOW
        )

        for metric in ("pm25", "pm10"):
            self.assertIn("At or below", at_guideline[f"{metric}_who_24h"]["category"])
            self.assertIn("Above", above_guideline[f"{metric}_who_24h"]["category"])

    def test_voc_is_relative_to_100_and_never_a_concentration(self) -> None:
        result = interpret_station(station(voc_index=100), now=NOW)["voc_index"]

        self.assertEqual(result["category"], "Near learned recent background")
        self.assertEqual(result["unit"], "index")
        self.assertIn("dimensionless", result["limitation"])
        self.assertIn("adaptive baseline", result["limitation"])
        self.assertEqual(result["status_type"], "dashboard_heuristic")

        action = interpret_station(station(voc_index=150), now=NOW)["voc_index"]
        self.assertEqual(
            action["status_type"], "manufacturer_index_interpretation"
        )

    def test_nox_baseline_is_one_and_not_a_no2_concentration(self) -> None:
        result = interpret_station(station(nox_index=1), now=NOW)["nox_index"]

        self.assertIn("baseline", result["category"].lower())
        self.assertEqual(result["unit"], "index")
        self.assertIn("not NO₂ concentration", result["limitation"])
        self.assertEqual(
            result["status_type"], "manufacturer_index_interpretation"
        )

        heuristic = interpret_station(station(nox_index=10), now=NOW)["nox_index"]
        self.assertEqual(heuristic["status_type"], "dashboard_heuristic")

    def test_voc_and_nox_band_boundaries(self) -> None:
        voc_cases = {
            69: "Below learned recent background",
            70: "Near learned recent background",
            130: "Near learned recent background",
            131: "Increased relative VOC activity",
            149: "Increased relative VOC activity",
            150: "Sensirion example action level reached",
        }
        nox_cases = {
            1: "Near normal NOx Index baseline",
            2: "NOx-related activity detected",
            9: "NOx-related activity detected",
            10: "Elevated relative NOx activity",
            19: "Elevated relative NOx activity",
            20: "Sensirion example action level reached",
        }
        for value, category in voc_cases.items():
            with self.subTest(metric="voc", value=value):
                self.assertEqual(
                    interpret_station(station(voc_index=value), now=NOW)["voc_index"]["category"],
                    category,
                )
        for value, category in nox_cases.items():
            with self.subTest(metric="nox", value=value):
                self.assertEqual(
                    interpret_station(station(nox_index=value), now=NOW)["nox_index"]["category"],
                    category,
                )

    def test_pm1_and_pm4_are_informational(self) -> None:
        results = interpret_station(station(), now=NOW)
        self.assertEqual(results["pm1"]["status_type"], "informational_only")
        self.assertEqual(results["pm4"]["status_type"], "informational_only")
        self.assertFalse(results["pm1"]["is_official_category"])

    def test_co2_ventilation_is_separate_from_occupational_exposure(self) -> None:
        results = interpret_station(
            station(co2=1200), summary_15m={"co2_max": 1200}, now=NOW
        )

        self.assertEqual(results["co2"]["status_type"], "ventilation_indicator")
        self.assertEqual(results["co2"]["severity"], "poor")
        self.assertEqual(
            results["co2_occupational"]["severity"], "informational"
        )
        self.assertIn("different questions", results["co2_occupational"]["limitation"])

    def test_co2_ventilation_and_occupational_boundaries(self) -> None:
        ventilation_cases = {
            800: "Ventilation appears effective",
            801: "Ventilation watch",
            1000: "Ventilation watch",
            1001: "Ventilation recommended",
            1500: "Ventilation recommended",
            1501: "Strong ventilation recommended",
        }
        for value, category in ventilation_cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    interpret_station(station(co2=value), now=NOW)["co2"]["category"],
                    category,
                )

        occupational_cases = {
            4999: "Below occupational exposure-limit numeric values",
            5000: "At or above occupational 8-hour TWA numeric value",
            30000: "At or above NIOSH 15-minute STEL numeric value",
            40000: "At or above NIOSH IDLH numeric value",
        }
        for value, category in occupational_cases.items():
            with self.subTest(value=value):
                result = interpret_station(
                    station(co2=value), summary_15m={"co2_max": value}, now=NOW
                )["co2_occupational"]
                self.assertEqual(result["category"], category)

    def test_temperature_and_humidity_ranges_are_non_dramatic(self) -> None:
        results = interpret_station(station(temperature_c=28, humidity=61), now=NOW)

        self.assertEqual(results["temperature_c"]["severity"], "moderate")
        self.assertEqual(results["humidity"]["severity"], "poor")
        self.assertNotEqual(results["temperature_c"]["severity"], "hazardous")

    def test_temperature_and_humidity_boundaries(self) -> None:
        temperature_cases = {
            19.9: "Below dashboard comfort range",
            20.0: "Within dashboard comfort range",
            26.0: "Within dashboard comfort range",
            26.1: "Above dashboard comfort range",
        }
        humidity_cases = {
            29.9: "Below EPA ideal range",
            30.0: "Within EPA ideal range",
            50.0: "Within EPA ideal range",
            50.1: "Above ideal; below moisture-control ceiling",
            59.9: "Above ideal; below moisture-control ceiling",
            60.0: "Above EPA moisture-control recommendation",
        }
        for value, category in temperature_cases.items():
            with self.subTest(metric="temperature", value=value):
                self.assertEqual(
                    interpret_station(station(temperature_c=value), now=NOW)["temperature_c"]["category"],
                    category,
                )
        for value, category in humidity_cases.items():
            with self.subTest(metric="humidity", value=value):
                self.assertEqual(
                    interpret_station(station(humidity=value), now=NOW)["humidity"]["category"],
                    category,
                )

    def test_stale_invalid_and_warmup_states_withhold_reassurance(self) -> None:
        stale = interpret_station(
            station(last_seen="2026-07-21T17:00:00Z"), now=NOW
        )["co2"]
        invalid = interpret_station(station(voc_index=None), now=NOW)["voc_index"]
        invalid_packet = interpret_station(station(sample_valid=False), now=NOW)["co2"]
        warming = interpret_station(
            station(sensor_uptime_s=120, nox_index=10), now=NOW
        )["nox_index"]

        self.assertTrue(stale["is_stale"])
        self.assertEqual(stale["severity"], "unavailable")
        self.assertEqual(invalid["severity"], "unavailable")
        self.assertEqual(invalid_packet["severity"], "unavailable")
        self.assertFalse(invalid_packet["is_stale"])
        self.assertTrue(warming["is_warming_up"])


if __name__ == "__main__":
    unittest.main()
