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

    def test_monitoring_and_export_forms_are_integrated_without_inline_javascript(self) -> None:
        for element_id in (
            "monitoring-form",
            "monitoring-sources",
            "monitoring-fields",
            "monitoring-current",
            "export-form",
            "export-sources",
            "export-fields",
            "export-jobs",
        ):
            self.assertIn(f'id="{element_id}"', self.template)
        self.assertNotIn("onclick=", self.template)
        self.assertNotIn("onsubmit=", self.template)

    def test_workflow_pollers_are_independent_and_non_overlapping(self) -> None:
        self.assertIn("WORKFLOW_POLL_INTERVAL_MS", self.javascript)
        self.assertIn("PREVIEW_POLL_INTERVAL_MS", self.javascript)
        self.assertIn("state.monitoringInFlight", self.javascript)
        self.assertIn("state.exportsInFlight", self.javascript)
        self.assertIn("state.previewInFlight", self.javascript)
        self.assertIn("document.hidden", self.javascript)

    def test_status_refresh_does_not_reset_forms_or_create_work(self) -> None:
        monitoring_refresh = self.javascript[
            self.javascript.index("async function refreshMonitoringSessions"):
            self.javascript.index("async function refreshMonitoringPreviews")
        ]
        export_refresh = self.javascript[
            self.javascript.index("async function refreshExportJobs"):
            self.javascript.index("function renderMonitoringSessions")
        ]
        self.assertNotIn(".reset(", monitoring_refresh)
        self.assertNotIn('method: "POST"', monitoring_refresh)
        self.assertNotIn(".reset(", export_refresh)
        self.assertNotIn('method: "POST"', export_refresh)

    def test_clocks_use_server_timestamp_and_download_requires_ready_state(self) -> None:
        self.assertIn("state.serverOffsetMs", self.javascript)
        self.assertIn("updateVisibleWorkflowClocks", self.javascript)
        self.assertIn("formatElapsedClock", self.javascript)
        self.assertIn("job.is_download_ready ? downloadLinkHtml(job)", self.javascript)
        self.assertIn("exportJob?.is_download_ready ? downloadLinkHtml(exportJob)", self.javascript)


if __name__ == "__main__":
    unittest.main()
