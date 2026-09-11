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

    def test_dependency_health_is_rendered_with_every_state(self) -> None:
        for state in ("healthy", "degraded", "unavailable", "unknown"):
            with self.subTest(state=state):
                self.assertIn(f"health-{state}", self.styles)
                self.assertIn(f'{state}: "', self.javascript)

    def test_dependency_health_state_is_not_encoded_by_colour_alone(self) -> None:
        """Each card carries the state in words and a redundant glyph."""

        self.assertIn("HEALTH_STATE_LABELS", self.javascript)
        self.assertIn("HEALTH_STATE_ICONS", self.javascript)
        self.assertIn('<span aria-hidden="true">', self.javascript)
        self.assertIn("healthStateLabel(itemState)", self.javascript)

    def test_dependency_health_says_what_was_actually_checked(self) -> None:
        """A process-only verdict must not read as end-to-end verification."""

        self.assertIn("HEALTH_BASIS_NOTES", self.javascript)
        self.assertIn("Only the unit's state was checked", self.javascript)
        self.assertIn("Not required for sensor ingest.", self.javascript)

    def test_a_health_outage_does_not_blank_the_status_tab(self) -> None:
        """The endpoint that reports outages must not take the tab down."""

        self.assertIn("fetchJson(API.systemStatus).catch(() => null)", self.javascript)
        self.assertIn("Dependency health could not be read.", self.javascript)

    def test_deployed_revision_and_uptime_are_surfaced(self) -> None:
        self.assertIn("source_revision", self.javascript)
        self.assertIn("process_uptime_seconds", self.javascript)
        self.assertIn("checks that did not finish in time", self.javascript)
        self.assertIn('id="health-build"', self.template)

    def test_dependency_health_section_is_labelled_and_live(self) -> None:
        self.assertIn('aria-labelledby="health-heading"', self.template)
        self.assertIn('id="health-heading"', self.template)
        self.assertIn('id="health-grid" class="health-grid" aria-live="polite"', self.template)

    def test_dependency_health_grid_collapses_on_a_phone(self) -> None:
        """Same auto-fit track as the services grid, so 390px is one column."""

        health = self.styles[self.styles.index(".health-grid {") :]
        self.assertIn("repeat(auto-fit, minmax(260px, 1fr))", health[:200])

    def test_the_connection_pill_never_reports_a_printer_state(self) -> None:
        """Regression: the printer "randomly" showed partial while working fine.

        refreshPrinterDashboard issues five parallel fetches and counted the
        rejected ones, then wrote "Printer partial (N)" into #connection-state --
        the page's connection pill, which every other tab uses for Online or API
        error. A dashboard-refresh problem was therefore presented as a printer
        fault. The telemetry chart alone measured ~4.9s for its default 24h range
        against an 8s abort, so ordinary slowness tripped it.
        """

        self.assertNotIn("Printer partial (", self.javascript)
        self.assertIn("sections unavailable", self.javascript)
        self.assertIn("PRINTER_SECTIONS", self.javascript)

    def test_a_failed_refresh_names_the_section_rather_than_the_printer(self) -> None:
        self.assertIn("Could not load:", self.javascript)
        for section in (
            "printer state",
            "print history",
            "maintenance",
            "telemetry chart",
            "sensor nodes",
        ):
            with self.subTest(section=section):
                self.assertIn(f'"{section}"', self.javascript)

    def test_a_timeout_message_states_the_budget_that_was_actually_used(self) -> None:
        """A 20s telemetry abort must not claim it waited 8 seconds."""

        self.assertIn("const budgetMs =", self.javascript)
        self.assertIn("request timed out after ${budgetMs / 1000} seconds", self.javascript)
        self.assertNotIn(
            "request timed out after ${FETCH_TIMEOUT_MS / 1000} seconds", self.javascript
        )

    def test_only_the_telemetry_fetch_overrides_the_shared_budget(self) -> None:
        """The longer budget must not become every request's timeout."""

        self.assertEqual(self.javascript.count("timeoutMs: "), 1)
        self.assertIn("timeoutMs: PRINTER_TELEMETRY_TIMEOUT_MS", self.javascript)

    def test_the_slow_telemetry_query_has_its_own_budget(self) -> None:
        """The chart reads raw telemetry, not a downsampled aggregate."""

        self.assertIn("PRINTER_TELEMETRY_TIMEOUT_MS", self.javascript)
        self.assertIn("timeoutMs: PRINTER_TELEMETRY_TIMEOUT_MS", self.javascript)
        # It must be a real increase over the shared default, not decoration.
        default = int(
            self.javascript.split("const FETCH_TIMEOUT_MS = ")[1].split(";")[0]
        )
        chart = int(
            self.javascript.split("const PRINTER_TELEMETRY_TIMEOUT_MS = ")[1].split(";")[0]
        )
        self.assertGreater(chart, default)

    def test_the_printer_card_still_shows_the_real_printer_state(self) -> None:
        """The genuine state machine must keep its own, unrelated display."""

        self.assertIn("formatLabel(printer.status", self.javascript)
        self.assertIn("Normalized state", self.javascript)

    def test_filament_totals_are_rendered_with_a_material_breakdown(self) -> None:
        self.assertIn("Tracked Filament", self.javascript)
        self.assertIn("tracked_filament_by_material", self.javascript)
        self.assertIn("tracked_filament_estimate_g", self.javascript)
        self.assertIn(".filament-row", self.styles)

    def test_filament_is_presented_as_an_estimate_not_a_measurement(self) -> None:
        """The number is Bambu's slicer plan, and the card has to say so."""

        self.assertIn("slicer estimate, not weighed consumption", self.javascript)

    def test_prints_without_a_usable_amount_stay_visible(self) -> None:
        """Silently dropping them would make the total look complete."""

        self.assertIn("tracked_filament_incomplete_job_count", self.javascript)
        self.assertIn("tracked_filament_unknown_amount_job_count", self.javascript)
        self.assertIn("Not included:", self.javascript)
        self.assertIn("Not broken down", self.javascript)

    def test_the_dashboard_never_shows_the_scheduler_enum_names(self) -> None:
        """baseline_required and advisory are internal vocabulary.

        They describe why the scheduler cannot put a date on a task, which is
        not something a reader should have to decode.
        """

        self.assertIn("MAINTENANCE_STATE_LABELS", self.javascript)
        self.assertIn("Maintenance history needed", self.javascript)
        self.assertIn("Periodic inspection", self.javascript)
        # The raw enum must not reach a label. It may still appear as a state
        # comparison or a CSS class, so check the rendering helpers instead.
        self.assertNotIn("formatLabel(task.state)", self.javascript)
        self.assertNotIn("Advisory: ${Number(counts.advisory", self.javascript)
        self.assertNotIn("Needs baseline:", self.javascript)

    def test_each_unschedulable_state_explains_itself_in_plain_words(self) -> None:
        self.assertIn("MAINTENANCE_STATE_HELP", self.javascript)
        self.assertIn("no record of when it was last done", self.javascript)
        self.assertIn("publishes no print-hour or calendar interval", self.javascript)

    def test_the_completion_button_says_what_it_will_do(self) -> None:
        self.assertIn("Record last service", self.javascript)
        self.assertIn("Mark done today", self.javascript)

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

    def test_monitoring_graph_offers_bambu_sources_not_orphan_measurements(
        self,
    ) -> None:
        """Reproduces the shipped Monitoring-tab defect.

        The measurement picker unioned the *global* field catalog while the
        source picker only ever read `latest.environment` and
        `latest.air_quality`, so Bambu measurements appeared and then reported
        themselves unavailable because no selectable source could provide them.
        """

        # The source picker must now build printer/AMS options from latest data.
        self.assertIn("data.printer || []", self.javascript)
        self.assertIn("data.ams || []", self.javascript)
        self.assertIn("bambuFilterKey", self.javascript)
        self.assertIn("...bambuSources", self.javascript)

        # The measurement picker must derive choices per selected source and
        # only from numerically graphable fields - not the raw global catalog.
        self.assertIn(
            "const available = new Set(applicableSources.flatMap(graphableFieldsFor));",
            self.javascript,
        )
        self.assertNotIn(
            "applicableSources.flatMap((source) => source.available_fields || [])",
            self.javascript,
        )
        self.assertIn("numeric_aggregation === true", self.javascript)

        # Graphability is decided by the capability catalog, never by name.
        self.assertNotIn("external_spool_1", self.javascript)
        self.assertNotIn("external_spool_2", self.javascript)
        self.assertNotIn('=== "ams_1"', self.javascript)

    def test_monitoring_graph_fetches_and_attributes_bambu_history(self) -> None:
        # The graph composes the existing audited telemetry endpoint rather
        # than a second storage or query system.
        self.assertIn("printerTelemetryChartQuery", self.javascript)
        self.assertIn("API.printerTelemetry", self.javascript)
        self.assertIn("state.chartTelemetryData", self.javascript)
        # A telemetry failure must not take the environmental graph down.
        self.assertIn(".catch(() => null)", self.javascript)

        # A dataset is only built where the source family really provides the
        # field, so a printer never appears to supply AMS humidity.
        self.assertIn(
            "(definition.sensor_types || []).includes(item.sensor_type)",
            self.javascript,
        )
        self.assertIn("chartSeriesMatchesFilter", self.javascript)
        self.assertIn("chartSourceLabel", self.javascript)

        # Bambu tier is reported honestly alongside the environmental tier.
        self.assertIn('startsWith("durable_")', self.javascript)

    def test_legacy_environmental_graph_behaviour_is_unchanged(self) -> None:
        # Environment/air-quality options are still built exactly as before,
        # independent of the capability catalog.
        self.assertIn(
            "const environmentNodes = (data.environment || [])", self.javascript
        )
        self.assertIn("const airStations = (data.air_quality || [])", self.javascript)
        self.assertIn("`Node ${reading.node_id}`", self.javascript)
        self.assertIn("SEN66 · ${formatLabel(reading.location)}", self.javascript)
        # /api/readings keeps its environment/air-quality-only query params.
        self.assertIn('params.set("sensor_type", "environment")', self.javascript)
        self.assertIn('params.set("sensor_type", "air_quality")', self.javascript)
        self.assertNotIn(
            'params.set("sensor_type", "printer");\n    params.set("node_id"',
            self.javascript,
        )
        # The dedicated Bambu tab graph is untouched.
        self.assertIn("renderPrinterTelemetryChart", self.javascript)
        self.assertIn('id="printer-telemetry-chart"', self.template)

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
        # The state is still handled; it is the *label* that is now plain
        # English rather than the scheduler's enum name.
        self.assertIn("Maintenance history needed", self.javascript)
        self.assertIn("Manufacturer cadence:", self.javascript)
        self.assertIn("Local warning lead time only", self.javascript)
        # Same guarantee, now stated without the enum's vocabulary: an item
        # Bambu gives no interval for must not acquire an invented due date.
        self.assertIn("publishes no print-hour or calendar interval", self.javascript)
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
