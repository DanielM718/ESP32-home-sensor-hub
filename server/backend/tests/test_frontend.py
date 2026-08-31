from __future__ import annotations

import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _bracket_balance_errors(source: str) -> list[str]:
    """Balance JS brackets outside comments, strings, and regex literals.

    No JavaScript runtime is installed on this host, so an unbalanced bracket
    would otherwise ship as a parse error that disables the whole dashboard.
    This is not a parser; it is the narrow guard that catches that failure.
    """

    errors: list[str] = []
    stack: list[tuple[str, int]] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    # Template-literal nesting: each entry is the ``{`` depth at which a
    # ``${`` interpolation opened, so a closing brace resumes the literal.
    template_depths: list[int] = []
    index, line, prev = 0, 1, ""
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        pair = source[index : index + 2]
        if pair == "//":
            index = source.find("\n", index)
            if index == -1:
                break
            continue
        if pair == "/*":
            end = source.find("*/", index + 2)
            line += source.count("\n", index, end if end != -1 else length)
            index = length if end == -1 else end + 2
            continue
        if char in "'\"":
            index += 1
            while index < length and source[index] != char:
                index += 2 if source[index] == "\\" else 1
            index += 1
            continue
        if char == "`":
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "`":
                    index += 1
                    break
                if source[index : index + 2] == "${":
                    template_depths.append(len(stack))
                    stack.append(("{", line))
                    index += 2
                    break
                line += source[index] == "\n"
                index += 1
            continue
        if char == "/" and prev not in ")]}" and not prev.isalnum() and prev != "_":
            index += 1
            while index < length and source[index] != "/":
                index += 2 if source[index] == "\\" else 1
            index += 1
            continue
        if char in "([{":
            stack.append((char, line))
        elif char in pairs:
            if not stack:
                errors.append(f"line {line}: unmatched '{char}'")
            elif stack[-1][0] != pairs[char]:
                opener, opened = stack.pop()
                errors.append(
                    f"line {line}: '{char}' closes '{opener}' opened on line {opened}"
                )
            else:
                stack.pop()
                if (
                    char == "}"
                    and template_depths
                    and template_depths[-1] == len(stack)
                ):
                    # Resume the template literal this interpolation interrupted.
                    template_depths.pop()
                    index += 1
                    while index < length:
                        if source[index] == "\\":
                            index += 2
                            continue
                        if source[index] == "`":
                            index += 1
                            break
                        if source[index : index + 2] == "${":
                            template_depths.append(len(stack))
                            stack.append(("{", line))
                            index += 2
                            break
                        line += source[index] == "\n"
                        index += 1
                    prev = "`"
                    continue
        if not char.isspace():
            prev = char
        index += 1
    errors.extend(f"line {opened}: unclosed '{opener}'" for opener, opened in stack)
    return errors


class FrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.javascript = (FRONTEND / "static" / "app.js").read_text(encoding="utf-8")
        cls.styles = (FRONTEND / "static" / "styles.css").read_text(encoding="utf-8")
        cls.template = (FRONTEND / "templates" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_all_nine_sen66_readings_remain_visible(self) -> None:
        for field in (
            "temperature_c",
            "humidity",
            "co2",
            "voc_index",
            "nox_index",
            "pm1",
            "pm25",
            "pm4",
            "pm10",
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
        self.assertIn("Last-known values below are not current", self.javascript)
        self.assertIn("values_are_current", self.javascript)
        self.assertIn("sensorStatusPillClass", self.javascript)

    def test_normal_chart_is_dynamic_and_omits_statistical_datasets(self) -> None:
        self.assertIn('id="chart-metrics"', self.template)
        self.assertIn("state.selectedChartFields", self.javascript)
        self.assertIn("axisForUnit", self.javascript)
        self.assertIn("measurementLabel", self.javascript)
        self.assertNotIn("eventDatasets", self.javascript)
        self.assertNotIn('buildDatasets(airQualitySeries, "pm25_max"', self.javascript)

    def test_exactly_four_hash_backed_tabs_exist(self) -> None:
        for tab_id in ("monitoring", "bambu-printer", "active-monitoring", "status"):
            self.assertIn(f'href="#{tab_id}"', self.template)
            self.assertIn(f'id="panel-{tab_id}"', self.template)
        self.assertEqual(self.template.count('role="tab"'), 4)
        self.assertIn("hashchange", self.javascript)
        self.assertIn("ArrowRight", self.javascript)

    def test_mobile_layout_is_present(self) -> None:
        self.assertIn("@media (max-width: 640px)", self.styles)
        self.assertIn("grid-template-columns: 1fr", self.styles)
        self.assertIn('name="viewport"', self.template)

    def test_monitoring_and_export_forms_are_integrated_without_inline_javascript(
        self,
    ) -> None:
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

    def test_source_capabilities_custom_time_and_wide_default_are_rendered(
        self,
    ) -> None:
        self.assertIn("available_fields", self.javascript)
        self.assertIn('id="monitoring-custom-hours"', self.template)
        self.assertIn('id="monitoring-custom-minutes"', self.template)
        self.assertIn("hours * 3600 + minutes * 60", self.javascript)
        self.assertNotIn("Custom minutes</option>", self.template)
        self.assertIn('<option value="wide" selected>Wide</option>', self.template)
        self.assertIn("Best for Excel, plotting, and most analysis", self.javascript)

    def test_status_and_debug_content_is_separate_from_monitoring(self) -> None:
        status_panel = self.template[
            self.template.index('id="panel-status"') : self.template.index(
                "<script src="
            )
        ]
        monitoring_panel = self.template[
            self.template.index('id="panel-monitoring"') : self.template.index(
                'id="panel-active-monitoring"'
            )
        ]
        self.assertIn('id="services-grid"', status_panel)
        self.assertIn('id="nodes-table"', status_panel)
        self.assertIn("Architecture / data flow", status_panel)
        self.assertNotIn('id="nodes-table"', monitoring_panel)

    def test_workflow_pollers_are_independent_and_non_overlapping(self) -> None:
        self.assertIn("WORKFLOW_POLL_INTERVAL_MS", self.javascript)
        self.assertIn("PREVIEW_POLL_INTERVAL_MS", self.javascript)
        self.assertIn("state.monitoringInFlight", self.javascript)
        self.assertIn("state.exportsInFlight", self.javascript)
        self.assertIn("state.previewInFlight", self.javascript)
        self.assertIn("document.hidden", self.javascript)

    def test_status_refresh_does_not_reset_forms_or_create_work(self) -> None:
        monitoring_refresh = self.javascript[
            self.javascript.index(
                "async function refreshMonitoringSessions"
            ) : self.javascript.index("async function refreshMonitoringPreviews")
        ]
        export_refresh = self.javascript[
            self.javascript.index(
                "async function refreshExportJobs"
            ) : self.javascript.index("function renderMonitoringSessions")
        ]
        self.assertNotIn(".reset(", monitoring_refresh)
        self.assertNotIn('method: "POST"', monitoring_refresh)
        self.assertNotIn(".reset(", export_refresh)
        self.assertNotIn('method: "POST"', export_refresh)

    def test_dedicated_printer_tab_is_read_only_and_failure_isolated(self) -> None:
        self.assertIn('id="tab-bambu-printer"', self.template)
        self.assertIn('id="panel-bambu-printer"', self.template)
        self.assertIn('id="printer-status"', self.template)
        self.assertIn("No printer controls are exposed", self.template)
        self.assertIn("records an audit event only", self.template)
        monitoring_panel = self.template[
            self.template.index('id="panel-monitoring"') : self.template.index(
                'id="panel-bambu-printer"'
            )
        ]
        self.assertNotIn('id="printer-status"', monitoring_panel)
        printer_refresh = self.javascript[
            self.javascript.index(
                "async function refreshPrinter"
            ) : self.javascript.index("async function refreshKnownSources")
        ]
        self.assertIn("fetchJson(API.printer)", printer_refresh)
        self.assertIn("Promise.allSettled", printer_refresh)
        self.assertIn("Printer state is temporarily unavailable", printer_refresh)
        self.assertIn("inferred from active AMS tray", printer_refresh)
        self.assertIn("raw_samples_expired", printer_refresh)
        self.assertNotIn("start_print", self.javascript)
        self.assertNotIn("pause_print", self.javascript)
        self.assertIn('method: "POST"', printer_refresh)
        self.assertIn("confirm: true", printer_refresh)
        self.assertIn("does not send any command to the printer", printer_refresh)

    def test_printer_tab_has_mobile_structure_and_history_detail(self) -> None:
        for element_id in (
            "printer-details",
            "printer-ams",
            "printer-maintenance",
            "printer-history",
            "printer-environment",
        ):
            self.assertIn(f'id="{element_id}"', self.template)
        self.assertIn("printer-dashboard-grid", self.styles)
        self.assertIn("@media (max-width: 640px)", self.styles)
        self.assertIn("session_id=", self.javascript)

    def test_bambu_history_is_capability_driven_and_supports_required_ranges(
        self,
    ) -> None:
        for element_id in (
            "printer-telemetry-chart",
            "printer-telemetry-ranges",
            "printer-telemetry-metrics",
            "printer-telemetry-tier",
        ):
            self.assertIn(f'id="{element_id}"', self.template)
        for range_key in ("1h", "6h", "24h", "7d"):
            self.assertIn(f'data-range="{range_key}"', self.template)
        for field in (
            "ams_humidity",
            "ams_temperature_c",
            "chamber_temperature_c",
        ):
            self.assertIn(f'"{field}"', self.javascript)
        self.assertIn("API.printerTelemetry", self.javascript)
        self.assertIn("state.fieldDefinitions", self.javascript)
        self.assertIn('source.sensor_type === "ams"', self.javascript)
        self.assertIn("source.available_fields", self.javascript)
        self.assertIn("offline", self.javascript)
        self.assertNotIn("const AMS_1", self.javascript)
        self.assertNotIn("ams_inventory_json", self.javascript)
        self.assertNotIn("home_assistant", self.javascript.lower())

    def test_dashboard_javascript_brackets_are_balanced(self) -> None:
        """Guards against a parse error taking the whole dashboard down.

        The Bambu telemetry axis builder shipped with one extra ``)``, which no
        substring assertion could see and no JS runtime was present to catch.
        """

        self.assertEqual(_bracket_balance_errors(self.javascript), [])

    def test_environment_chart_frame_is_not_matched_by_the_bambu_frame(self) -> None:
        """The Bambu tab added a second .chart-frame-large to the document.

        The environment chart's empty-state toggle must resolve its own frame
        rather than whichever one happens to come first in the DOM.
        """

        self.assertEqual(self.template.count("chart-frame-large"), 2)
        self.assertNotIn('querySelector(".chart-frame-large")', self.javascript)
        self.assertIn(
            'document.getElementById("history-chart").closest(".chart-frame-large")',
            self.javascript,
        )
        self.assertIn('id="printer-telemetry-frame"', self.template)

    def test_tracked_print_time_is_a_first_class_qualified_usage_metric(self) -> None:
        self.assertIn('id="printer-usage"', self.template)
        self.assertIn("Printer Usage", self.template)
        self.assertIn("Tracked Print Time", self.javascript)
        self.assertIn("formatTrackedRuntime", self.javascript)
        self.assertIn("tracked_print_seconds", self.javascript)
        self.assertIn("tracked_job_count", self.javascript)
        self.assertIn("tracked_first_print_at", self.javascript)
        self.assertIn("tracked_last_print_at", self.javascript)
        self.assertIn("rolling_tracked_print_hours_per_day", self.javascript)
        self.assertIn(
            "may not represent the printer's complete lifetime", self.javascript
        )
        self.assertIn("not a printer lifetime counter", self.javascript)
        self.assertIn("usage-value", self.styles)

    def test_usage_and_maintenance_precede_printer_details_and_history(self) -> None:
        panel = self.template[self.template.index('id="panel-bambu-printer"') :]
        order = [
            panel.index('id="printer-usage"'),
            panel.index('id="printer-maintenance"'),
            panel.index('id="printer-details"'),
            panel.index('id="printer-history"'),
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn("<th>Duration</th>", self.template)

    def test_maintenance_states_and_baseline_actions_are_rendered(self) -> None:
        self.assertIn('id="maintenance-summary"', self.template)
        self.assertIn('id="maintenance-complete-all"', self.template)
        self.assertIn("Mark all maintenance completed today", self.template)
        self.assertIn("renderMaintenanceSummary", self.javascript)
        self.assertIn("baseline_required", self.javascript)
        self.assertIn("Needs a baseline", self.javascript)
        self.assertIn("Manufacturer cadence:", self.javascript)
        self.assertIn("Local warning lead time only", self.javascript)
        self.assertIn("Condition-based guidance", self.javascript)
        self.assertIn("${API.printerMaintenance}/complete-all", self.javascript)
        self.assertIn("establishes the maintenance baseline", self.javascript)
        self.assertIn("does not send any command to the printer", self.javascript)
        for style in (
            ".maintenance-due_soon",
            ".maintenance-baseline_required",
            ".maintenance-advisory",
            ".maintenance-summary",
        ):
            self.assertIn(style, self.styles)

    def test_usage_and_maintenance_stay_readable_on_narrow_screens(self) -> None:
        mobile = self.styles[self.styles.index("@media (max-width: 640px)") :]
        self.assertIn(".usage-facts", mobile)
        self.assertIn(".maintenance-counts", mobile)

    def test_clocks_use_server_timestamp_and_download_requires_ready_state(
        self,
    ) -> None:
        self.assertIn("state.serverOffsetMs", self.javascript)
        self.assertIn("updateVisibleWorkflowClocks", self.javascript)
        self.assertIn("formatElapsedClock", self.javascript)
        self.assertIn("job.is_download_ready ? downloadLinkHtml(job)", self.javascript)
        self.assertIn(
            "exportJob?.is_download_ready ? downloadLinkHtml(exportJob)",
            self.javascript,
        )


if __name__ == "__main__":
    unittest.main()
