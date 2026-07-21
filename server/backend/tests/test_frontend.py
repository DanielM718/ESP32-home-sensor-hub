from __future__ import annotations

from pathlib import Path
import unittest


FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


class FrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.javascript = (FRONTEND / "static" / "app.js").read_text(encoding="utf-8")
        cls.styles = (FRONTEND / "static" / "styles.css").read_text(encoding="utf-8")
        cls.template = (FRONTEND / "templates" / "index.html").read_text(encoding="utf-8")

    def test_all_nine_sen66_readings_remain_visible(self) -> None:
        for field in (
            "temperature_c", "humidity", "co2", "voc_index", "nox_index",
            "pm1", "pm25", "pm4", "pm10",
        ):
            with self.subTest(field=field):
                self.assertIn(f'field: "{field}"', self.javascript)

    def test_status_has_text_and_accessible_description_not_color_alone(self) -> None:
        self.assertIn("interpretation.category", self.javascript)
        self.assertIn('aria-label="${escapeHtml(metric.label)}:', self.javascript)
        self.assertIn("authority-label", self.javascript)
        self.assertIn("Source and limitations", self.javascript)

    def test_stale_and_warmup_warnings_are_rendered(self) -> None:
        self.assertIn("Stale — status withheld", self.javascript)
        self.assertIn("Latest sensor sample invalid — status withheld", self.javascript)
        self.assertIn("Sensor warming up / adapting", self.javascript)

    def test_historical_mean_max_and_events_are_distinguishable(self) -> None:
        self.assertIn("PM2.5 mean", self.javascript)
        self.assertIn("PM2.5 maximum", self.javascript)
        self.assertIn("PM2.5 p95", self.javascript)
        self.assertIn("eventDatasets", self.javascript)
        self.assertIn("15-minute aggregates", self.javascript)

    def test_mobile_layout_is_present(self) -> None:
        self.assertIn("@media (max-width: 640px)", self.styles)
        self.assertIn("grid-template-columns: 1fr", self.styles)
        self.assertIn('name="viewport"', self.template)


if __name__ == "__main__":
    unittest.main()
