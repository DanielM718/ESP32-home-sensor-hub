"use strict";

const API = {
  latest: "/api/latest",
  readings: "/api/readings",
  nodes: "/api/nodes",
  workflowOptions: "/api/workflows/options",
  monitoringSessions: "/api/monitoring/sessions",
  exports: "/api/exports",
  status: "/api/status",
  systemStatus: "/api/system-status",
  printer: "/api/printer",
  printerHistory: "/api/printer/history",
  printerTelemetry: "/api/printer/telemetry",
  printerMaintenance: "/api/printer/maintenance",
  printerEnvironment: "/api/printer/environment-summary",
};

const POLL_INTERVAL_MS = 7000;
const WORKFLOW_POLL_INTERVAL_MS = 5000;
const PREVIEW_POLL_INTERVAL_MS = 15000;
const FETCH_TIMEOUT_MS = 8000;
// The printer chart reads raw telemetry rather than a downsampled aggregate, so
// the default 24h range is the slowest request the dashboard makes.
const PRINTER_TELEMETRY_TIMEOUT_MS = 20000;
// Named in the same order they are requested, so a refresh failure can say
// which section is missing instead of blaming the printer.
const PRINTER_SECTIONS = ["printer state", "print history", "maintenance", "telemetry chart", "sensor nodes"];
const KNOWN_ENVIRONMENT_STATUS_MASK = 0x1f;

const AIR_QUALITY_METRIC_GROUPS = [
  {
    label: "Climate",
    metrics: [
      { field: "temperature_c", interpretation: "temperature_c", label: "Temperature", digits: 1, suffix: " °C" },
      { field: "humidity", interpretation: "humidity", label: "Relative humidity", digits: 1, suffix: "%" },
    ],
  },
  {
    label: "Gas and indices",
    metrics: [
      { field: "co2", interpretation: "co2", label: "CO₂", digits: 0, suffix: " ppm" },
      { field: "voc_index", interpretation: "voc_index", label: "VOC Index", digits: 0, suffix: "" },
      { field: "nox_index", interpretation: "nox_index", label: "NOx Index", digits: 0, suffix: "" },
    ],
  },
  {
    label: "Particulate matter",
    metrics: [
      { field: "pm1", interpretation: "pm1", label: "PM1.0", digits: 1, suffix: " µg/m³" },
      { field: "pm25", interpretation: "pm25_current", label: "PM2.5", digits: 1, suffix: " µg/m³" },
      { field: "pm4", interpretation: "pm4", label: "PM4.0", digits: 1, suffix: " µg/m³" },
      { field: "pm10", interpretation: "pm10_current", label: "PM10", digits: 1, suffix: " µg/m³" },
    ],
  },
];

const ENVIRONMENT_STATUS_FLAGS = [
  { mask: 1 << 0, label: "SHT41 read OK", className: "ok" },
  { mask: 1 << 1, label: "ESP-NOW send attempted", className: "info" },
  { mask: 1 << 2, label: "Battery measurement OK", className: "ok" },
  { mask: 1 << 3, label: "Low battery", className: "warning" },
  { mask: 1 << 4, label: "Battery shutdown", className: "danger" },
];

const chartPalette = [
  "#0f766e",
  "#2563eb",
  "#7c3aed",
  "#b45309",
  "#be123c",
  "#047857",
  "#4338ca",
  "#0e7490",
];

const state = {
  range: "24h",
  nodeFilter: "all",
  activeTab: "monitoring",
  charts: {},
  latestTimer: null,
  fullRefreshInFlight: false,
  latestRefreshInFlight: false,
  knownSources: new Map(),
  fieldDefinitions: new Map(),
  latestData: null,
  readingsData: null,
  nodesData: null,
  workflowOptions: null,
  workflowSelectedFields: {
    monitoring: new Set(["temperature_c", "humidity"]),
    export: new Set(["temperature_c", "humidity"]),
  },
  monitoringSessions: [],
  exportJobs: [],
  monitoringTimer: null,
  exportTimer: null,
  previewTimer: null,
  clockTimer: null,
  monitoringInFlight: false,
  exportsInFlight: false,
  previewInFlight: false,
  statusInFlight: false,
  printerInFlight: false,
  printerDashboardInFlight: false,
  printerTelemetryInFlight: false,
  printerCurrent: null,
  selectedPrinterHistoryId: null,
  printerTelemetryRange: "24h",
  printerTelemetryData: null,
  selectedPrinterTelemetryFields: new Set([
    "ams_humidity",
    "ams_temperature_c",
    "chamber_temperature_c",
  ]),
  serverOffsetMs: 0,
  selectedChartFields: new Set(["temperature_c", "humidity"]),
  chartTelemetryData: null,
  ready: false,
};

document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  setupWorkflowControllers();
  setupRangeButtons();
  setupNodeFilter();
  setupPrinterActions();
  setupPrinterTelemetryControls();
  document.getElementById("refresh-button").addEventListener("click", () => refreshActiveTab(true));

  if (window.Chart) {
    Chart.defaults.font.family = 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
    Chart.defaults.color = "#42534a";
    initializeCharts();
  } else {
    showError("Chart.js is not available. Run scripts/install_frontend_assets.sh on the Raspberry Pi.");
    setStatus("Chart.js missing", "error");
  }

  state.ready = true;
  void refreshWorkflowOptions();
  void refreshActiveTab(true);
  state.latestTimer = window.setInterval(refreshLatestOnly, POLL_INTERVAL_MS);
  state.monitoringTimer = window.setInterval(refreshMonitoringSessions, WORKFLOW_POLL_INTERVAL_MS);
  state.exportTimer = window.setInterval(refreshExportJobs, WORKFLOW_POLL_INTERVAL_MS);
  state.previewTimer = window.setInterval(refreshMonitoringPreviews, PREVIEW_POLL_INTERVAL_MS);
  state.clockTimer = window.setInterval(updateVisibleWorkflowClocks, 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      void refreshActiveTab(true);
    }
  });
});

const TAB_IDS = ["monitoring", "bambu-printer", "active-monitoring", "status"];

function setupTabs() {
  const links = TAB_IDS.map((id) => document.getElementById(`tab-${id}`));
  const activateHash = () => {
    const requested = window.location.hash.replace(/^#/, "");
    activateTab(TAB_IDS.includes(requested) ? requested : "monitoring");
  };
  for (const [index, link] of links.entries()) {
    link.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        return;
      }
      event.preventDefault();
      const nextIndex = event.key === "Home"
        ? 0
        : event.key === "End"
          ? links.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + links.length) % links.length;
      links[nextIndex].focus();
      links[nextIndex].click();
    });
  }
  window.addEventListener("hashchange", activateHash);
  activateHash();
}

function activateTab(tabId) {
  state.activeTab = tabId;
  for (const id of TAB_IDS) {
    const active = id === tabId;
    const link = document.getElementById(`tab-${id}`);
    const panel = document.getElementById(`panel-${id}`);
    link.classList.toggle("is-active", active);
    link.setAttribute("aria-selected", String(active));
    link.tabIndex = active ? 0 : -1;
    panel.hidden = !active;
  }
  if (state.ready) {
    void refreshActiveTab(true);
  }
}

async function refreshActiveTab(force = false) {
  if (document.hidden && !force) {
    return;
  }
  if (state.activeTab === "monitoring") {
    await refreshAll();
  } else if (state.activeTab === "bambu-printer") {
    await refreshPrinterDashboard(force);
  } else if (state.activeTab === "active-monitoring") {
    try {
      await Promise.all([
        refreshKnownSources(force),
        refreshMonitoringSessions(force),
        refreshExportJobs(force),
      ]);
      await refreshMonitoringPreviews(force);
      setStatus("Online", "ok");
      clearError();
    } catch (error) {
      setStatus("API error", "error");
      showError(error.message || "Active Monitoring refresh failed");
    }
  } else {
    await refreshStatusTab(force);
  }
}

async function refreshAll() {
  if (state.fullRefreshInFlight || state.latestRefreshInFlight) {
    return;
  }

  state.fullRefreshInFlight = true;
  setRefreshButtonBusy(true);
  clearError();
  setStatus("Loading", "loading");
  try {
    const readingsUrl = `${API.readings}?${readingsQueryParams().toString()}`;
    const telemetryUrl = printerTelemetryChartQuery();
    const [latest, readings, telemetry] = await Promise.all([
      fetchJson(API.latest),
      fetchJson(readingsUrl),
      // Bambu history is optional here: a telemetry failure must never take
      // the environmental graph down with it.
      telemetryUrl ? fetchJson(telemetryUrl).catch(() => null) : Promise.resolve(null),
    ]);
    const nodes = await nodesForLatest(latest);

    state.latestData = latest;
    state.readingsData = readings;
    state.chartTelemetryData = telemetry;
    state.nodesData = nodes;
    updateWorkflowSources(nodes.nodes || []);
    updateNodeFilterOptions(latest);
    renderLatest(latest);
    renderChartMetricSelector();
    if (state.printerTelemetryData) {
      renderPrinterTelemetrySelector(state.printerTelemetryData);
      renderPrinterTelemetryChart(state.printerTelemetryData);
    }
    renderCharts(readings);
    setLastUpdated(latest.generated_at || readings.generated_at || nodes.generated_at);
    setStatus("Online", "ok");
  } catch (error) {
    setStatus("API error", "error");
    showError(error.message || "Dashboard refresh failed");
  } finally {
    state.fullRefreshInFlight = false;
    setRefreshButtonBusy(false);
  }
}

async function refreshLatestOnly() {
  if (state.activeTab === "bambu-printer" && !document.hidden) {
    await refreshPrinter();
    return;
  }
  if (state.activeTab !== "monitoring" || document.hidden
      || state.fullRefreshInFlight || state.latestRefreshInFlight) {
    return;
  }

  state.latestRefreshInFlight = true;
  try {
    const latest = await fetchJson(API.latest);
    const nodes = await nodesForLatest(latest);

    state.latestData = latest;
    state.nodesData = nodes;
    updateWorkflowSources(nodes.nodes || []);
    updateNodeFilterOptions(latest);
    renderLatest(latest);
    renderChartMetricSelector();
    setLastUpdated(latest.generated_at || nodes.generated_at);
    setStatus("Online", "ok");
    clearError();
  } catch (error) {
    setStatus("API error", "error");
    showError(error.message || "Latest refresh failed");
  } finally {
    state.latestRefreshInFlight = false;
  }
}

async function refreshPrinter() {
  if (state.printerInFlight) {
    return;
  }
  state.printerInFlight = true;
  try {
    const printer = await fetchJson(API.printer);
    state.printerCurrent = printer;
    renderPrinter(printer);
    renderPrinterUsage(printer);
    renderPrinterDetails(printer);
    renderAms(printer.ams_units || []);
  } catch (_error) {
    renderPrinter({ available: false, status: "unavailable", reason: "Printer state is temporarily unavailable" });
    renderPrinterSectionError("printer-usage", "Tracked print time is temporarily unavailable.");
    renderPrinterSectionError("printer-details", "Printer details are temporarily unavailable.");
    renderPrinterSectionError("printer-ams", "AMS state is temporarily unavailable.");
  } finally {
    state.printerInFlight = false;
  }
}

function renderPrinter(printer) {
  const container = document.getElementById("printer-status");
  if (!container) {
    return;
  }
  if (printer.status === "not_configured") {
    container.innerHTML = '<p class="empty-state">Printer observer is not configured.</p>';
    return;
  }
  const progress = Number.isFinite(printer.progress_percent)
    ? `${Math.round(printer.progress_percent)}%`
    : "Unknown";
  const remaining = Number.isFinite(printer.remaining_seconds)
    ? formatDuration(printer.remaining_seconds)
    : "Unknown";
  const layer = Number.isInteger(printer.current_layer)
    ? `${printer.current_layer}${Number.isInteger(printer.total_layers) ? ` / ${printer.total_layers}` : ""}`
    : "Unknown";
  const materialProvenance = (printer.provenance || {}).active_material;
  const materialQualifier = materialProvenance === "observed"
    ? ""
    : materialProvenance === "inferred_active_ams_tray"
      ? " (inferred from active AMS tray)"
      : " (provenance unknown)";
  const material = printer.active_material
    ? `${printer.active_material}${materialQualifier}`
    : "Unknown";
  const expectedFinish = printer.expected_finished_at
    ? formatDateTime(printer.expected_finished_at)
    : "Unknown";
  const sen66Monitoring = printer.sen66_monitoring || null;
  const sen66MonitoringText = sen66Monitoring
    ? `${formatLabel(sen66Monitoring.state)}${sen66Monitoring.reason ? ` — ${sen66Monitoring.reason}` : ""}`
    : "No automatic SEN66 monitoring decision recorded";
  container.innerHTML = `
    <article class="reading-card printer-card printer-current-card">
      <div class="air-station-heading">
        <div><h3>${escapeHtml(printer.job_name || printer.printer_model || "Printer")}</h3><span class="authority-label">Read only · ${escapeHtml(printer.source || "unavailable")} · observed values unless labeled inferred</span></div>
        <span class="status-pill ${printer.available ? "status-ok" : "status-error"}">${escapeHtml(formatLabel(printer.status || "unknown"))}</span>
      </div>
      <div class="printer-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Number.isFinite(printer.progress_percent) ? Math.round(printer.progress_percent) : 0}">
        <span style="width:${Number.isFinite(printer.progress_percent) ? Math.max(0, Math.min(100, printer.progress_percent)) : 0}%"></span>
      </div>
      <dl class="printer-facts">
        <div><dt>Normalized state</dt><dd>${escapeHtml(formatLabel(printer.normalized_state || "unknown"))}</dd></div>
        <div><dt>Stage</dt><dd>${escapeHtml(formatLabel(printer.current_stage || "unknown"))}</dd></div>
        <div><dt>Print source</dt><dd>${escapeHtml(formatLabel(printer.print_source || "unknown"))}</dd></div>
        <div><dt>Progress</dt><dd>${escapeHtml(progress)}</dd></div>
        <div><dt>Remaining</dt><dd>${escapeHtml(remaining)}</dd></div>
        <div><dt>Layer</dt><dd>${escapeHtml(layer)}</dd></div>
        <div><dt>Material</dt><dd>${escapeHtml(material)}</dd></div>
        <div><dt>Started</dt><dd>${escapeHtml(printer.print_started_at ? formatDateTime(printer.print_started_at) : "Unknown")}</dd></div>
        <div><dt>Expected finish</dt><dd>${escapeHtml(expectedFinish)} <span class="subtle">(upstream estimate)</span></dd></div>
        <div><dt>Tool / tray</dt><dd>${escapeHtml([printer.active_tool, printer.ams_slot].filter(Boolean).join(" · ") || "Unknown")}</dd></div>
        <div><dt>Temperatures</dt><dd>${escapeHtml(printerTemperatureSummary(printer))}</dd></div>
        <div><dt>SEN66 monitoring</dt><dd>${escapeHtml(sen66MonitoringText)}</dd></div>
        <div><dt>Observed</dt><dd>${escapeHtml(relativeTime(printer.observed_at))}</dd></div>
      </dl>
    </article>`;
}

async function refreshPrinterDashboard(force = false) {
  if ((document.hidden && !force) || state.printerDashboardInFlight) {
    return;
  }
  state.printerDashboardInFlight = true;
  setRefreshButtonBusy(true);
  try {
    // The chart query reads raw telemetry and is by far the slowest of the five
    // -- measured at ~4.9s for the default 24h range on an idle Pi, against the
    // 8s default abort. It gets its own budget so ordinary slowness stops
    // presenting itself as a printer fault.
    const requests = await Promise.allSettled([
      fetchJson(API.printer),
      fetchJson(`${API.printerHistory}?limit=100`),
      fetchJson(API.printerMaintenance),
      fetchJson(
        `${API.printerTelemetry}?range=${encodeURIComponent(state.printerTelemetryRange)}`,
        { timeoutMs: PRINTER_TELEMETRY_TIMEOUT_MS },
      ),
      fetchJson(API.nodes),
    ]);
    const [current, history, maintenance, telemetry, nodes] = requests;
    if (current.status === "fulfilled") {
      state.printerCurrent = current.value;
      renderPrinter(current.value);
      renderPrinterUsage(current.value);
      renderPrinterDetails(current.value);
      renderAms(current.value.ams_units || []);
      setLastUpdated(current.value.observed_at);
    } else {
      renderPrinter({ available: false, status: "unavailable", reason: "Printer state is temporarily unavailable" });
      renderPrinterSectionError("printer-usage", "Tracked print time is temporarily unavailable.");
      renderPrinterSectionError("printer-details", "Printer details are temporarily unavailable.");
      renderPrinterSectionError("printer-ams", "AMS state is temporarily unavailable.");
    }
    if (history.status === "fulfilled") {
      renderPrinterHistory(history.value.history || []);
    } else {
      renderPrinterHistoryError("Print history is temporarily unavailable.");
    }
    if (maintenance.status === "fulfilled") {
      renderMaintenance(maintenance.value);
    } else {
      renderPrinterSectionError("printer-maintenance", "Maintenance records are temporarily unavailable.");
    }
    if (nodes.status === "fulfilled") {
      state.nodesData = nodes.value;
      updateWorkflowSources(nodes.value.nodes || []);
    }
    if (telemetry.status === "fulfilled") {
      state.printerTelemetryData = telemetry.value;
      renderPrinterTelemetrySelector(telemetry.value);
      renderPrinterTelemetryChart(telemetry.value);
    } else {
      renderPrinterTelemetryError("Historical printer telemetry is temporarily unavailable.");
    }
    // This pill is the page's connection indicator, which every other tab uses
    // for "Online" or "API error". Counting rejected fetches here and calling
    // the result "Printer partial" attached a dashboard-refresh problem to the
    // printer, so a healthy printer looked degraded whenever one section timed
    // out. The printer's own state is rendered in its card, from printer.status.
    const failed = PRINTER_SECTIONS.filter((_name, index) => requests[index].status === "rejected");
    setStatus(
      failed.length ? `${failed.length} of ${PRINTER_SECTIONS.length} sections unavailable` : "Online",
      failed.length ? "loading" : "ok",
    );
    const pill = document.getElementById("connection-state");
    if (pill) {
      pill.title = failed.length ? `Could not load: ${failed.join(", ")}` : "";
    }
  } finally {
    state.printerDashboardInFlight = false;
    setRefreshButtonBusy(false);
  }
}

function renderPrinterDetails(printer) {
  const usage = printer.usage || {};
  const container = document.getElementById("printer-details");
  if (!container) return;
  container.innerHTML = `<dl class="printer-facts printer-detail-facts">
    <div><dt>Model / firmware</dt><dd>${escapeHtml(printer.printer_model || "Unknown")} · ${escapeHtml(printer.firmware_version || "Unknown")}</dd></div>
    <div><dt>Connection</dt><dd>${escapeHtml(formatLabel(printer.mqtt_connection_mode || "unknown"))} MQTT · ${printer.mqtt_encryption === true ? "encrypted" : printer.mqtt_encryption === false ? "not encrypted" : "encryption unknown"}</dd></div>
    <div><dt>Hybrid protection</dt><dd>${printer.hybrid_mqtt_control_blocked === true ? "Control blocked" : printer.hybrid_mqtt_control_blocked === false ? "Not reported blocked" : "Unknown"} · Developer LAN ${printer.developer_lan_mode === true ? "on" : printer.developer_lan_mode === false ? "off" : "unknown"}</dd></div>
    <div><dt>Wi-Fi</dt><dd>${Number.isFinite(printer.wifi_signal_dbm) ? `${printer.wifi_signal_dbm} dBm` : "Unknown"}</dd></div>
    <div><dt>Left nozzle</dt><dd>${escapeHtml(nozzleDescription(printer, 1))}</dd></div>
    <div><dt>Right nozzle</dt><dd>${escapeHtml(nozzleDescription(printer, 2))}</dd></div>
    <div><dt>Printer-reported lifetime</dt><dd>${usage.printer_reported_lifetime_hours_available ? `${formatNumber(usage.printer_reported_lifetime_hours, 2, " h")}` : "Not exposed by X2D / ha-bambulab"}</dd></div>
    <div><dt>HA integration estimate</dt><dd>${formatNumber(usage.ha_bambulab_estimated_usage_hours, 2, " h")} <span class="subtle">(not printer lifetime)</span></dd></div>
    <div><dt>Locally observed</dt><dd>${formatNumber(usage.locally_observed_print_hours, 2, " h")} · ${Number(usage.locally_recorded_terminal_job_count || 0)} terminal jobs</dd></div>
    <div><dt>Maintenance position</dt><dd>${formatNumber(usage.maintenance_effective_lifetime_hours, 2, " h")} <span class="subtle">(${escapeHtml(formatLabel(usage.maintenance_effective_provenance || "unknown"))})</span></dd></div>
  </dl>`;
}

function renderAms(units) {
  const container = document.getElementById("printer-ams");
  if (!container) return;
  if (!units.length) {
    container.innerHTML = '<p class="empty-state">No mapped AMS or external spool state is available.</p>';
    return;
  }
  container.innerHTML = units.map((unit) => `<article class="ams-card">
    <div class="air-station-heading"><h3>${escapeHtml(unit.model || unit.ams_id)}</h3><span class="status-pill ${unit.active ? "status-ok" : "status-loading"}">${unit.active ? "Active" : "Standby"}</span></div>
    <p class="subtle">${unit.humidity_percent == null ? "Humidity unavailable" : `Humidity ${escapeHtml(unit.humidity_percent)}%`} · ${unit.temperature == null ? "Temperature unavailable" : `${escapeHtml(unit.temperature)} °C`} · ${unit.drying ? `Drying (${formatDuration(unit.remaining_drying_seconds)} remaining)` : "Not drying"}</p>
    <div class="tray-grid">${(unit.trays || []).map((tray) => `<div class="tray-card ${tray.active ? "is-active" : ""}">
      <span class="tray-color" style="--tray-color:${safeColor(tray.color)}"></span>
      <strong>Slot ${escapeHtml(tray.slot)}</strong><span>${escapeHtml(tray.name || (tray.empty ? "Empty" : "Unknown"))}</span><small>${escapeHtml(tray.material || "Material unknown")}${Number.isInteger(tray.remaining_percent) ? ` · ${tray.remaining_percent}%` : ""}</small>
    </div>`).join("")}</div>
  </article>`).join("");
}

function renderPrinterUsage(printer) {
  const container = document.getElementById("printer-usage");
  if (!container) return;
  const usage = (printer && printer.usage) || {};
  if (!Number.isFinite(Number(usage.tracked_print_seconds))) {
    container.innerHTML = '<p class="empty-state">Tracked print time is unavailable until print history exists.</p>';
    return;
  }
  const jobs = Number(usage.tracked_job_count || 0);
  const perDay = usage.rolling_tracked_print_hours_per_day;
  container.innerHTML = `<div class="usage-hero">
    <div class="usage-headline">
      <span class="usage-label">Tracked Print Time</span>
      <strong class="usage-value">${escapeHtml(formatTrackedRuntime(usage.tracked_print_seconds))}</strong>
      <span class="authority-label">Sum of known actual print-history intervals. Bambu Cloud history may not represent the printer's complete lifetime.</span>
    </div>
    <dl class="printer-facts usage-facts">
      <div><dt>Tracked prints</dt><dd>${jobs}</dd></div>
      <div><dt>Completed</dt><dd>${Number(usage.tracked_completed_count || 0)}</dd></div>
      <div><dt>Failed or cancelled</dt><dd>${Number(usage.tracked_failed_or_cancelled_count || 0)}</dd></div>
      <div><dt>First tracked print</dt><dd>${usage.tracked_first_print_at ? escapeHtml(formatDateTime(usage.tracked_first_print_at)) : "Unknown"}</dd></div>
      <div><dt>Last tracked print</dt><dd>${usage.tracked_last_print_at ? escapeHtml(formatDateTime(usage.tracked_last_print_at)) : "Unknown"}</dd></div>
      <div><dt>Recent average</dt><dd>${perDay == null ? "Unknown" : `${formatNumber(perDay, 2, " h/day")}`} <span class="subtle">(${Number(usage.rolling_window_days || 0)}-day window)</span></dd></div>
      <div><dt>Maintenance mode</dt><dd>${escapeHtml(formatLabel(usage.maintenance_mode || "unknown"))} <span class="subtle">(${escapeHtml(formatLabel(usage.maintenance_mode_reason || "unknown"))})</span></dd></div>
      <div><dt>History provenance</dt><dd>${escapeHtml((usage.tracked_history_provenance || []).map(formatLabel).join(", ") || "None")}</dd></div>
    </dl>
    <p class="subtle">History completeness: ${usage.tracked_history_complete ? "known complete" : "not known to be complete"}${Number(usage.tracked_unknown_interval_job_count || 0) ? ` · ${Number(usage.tracked_unknown_interval_job_count)} job(s) have no usable start/end interval and are excluded` : ""}. This is not a printer lifetime counter.</p>
  </div>`;
}

function formatTrackedRuntime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "Unavailable";
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours} h ${minutes} m`;
}

function renderMaintenanceSummary(payload) {
  const container = document.getElementById("maintenance-summary");
  if (!container) return;
  const summary = payload.summary || {};
  const counts = summary.counts || {};
  const next = summary.next_task;
  if (!summary.task_count) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = `<div class="maintenance-summary maintenance-${escapeHtml(summary.overall_state || "ok")}">
    <div class="air-station-heading">
      <h3>Overall: ${escapeHtml(formatLabel(summary.overall_state || "ok"))}</h3>
      <span class="status-pill ${maintenancePillClass(summary.overall_state)}">${escapeHtml(formatLabel(summary.maintenance_mode || "unknown"))} mode</span>
    </div>
    <p>${next ? `Next: <strong>${escapeHtml(next.name)}</strong>${next.next_due_at ? ` · due ${escapeHtml(formatDateTime(next.next_due_at))}` : ""}${Number.isFinite(Number(next.remaining_days)) ? ` (${Number(next.remaining_days).toFixed(0)} days)` : ""}` : "No scheduled task is pending."}</p>
    <ul class="maintenance-counts">
      <li>Due soon: ${Number(counts.due_soon || 0)}</li>
      <li>Due: ${Number(counts.due || 0)}</li>
      <li>Overdue: ${Number(counts.overdue || 0)}</li>
      <li>Needs baseline: ${Number(counts.baseline_required || 0)}</li>
      <li>Advisory: ${Number(counts.advisory || 0)}</li>
    </ul>
    <p class="subtle">Usage tier ${escapeHtml(formatLabel(summary.maintenance_mode || "unknown"))} from ${summary.rolling_print_hours_per_day == null ? "unknown" : `${Number(summary.rolling_print_hours_per_day).toFixed(2)} h/day`} tracked printing (${escapeHtml(formatLabel(summary.maintenance_mode_reason || "unknown"))}).</p>
  </div>`;
}

function maintenancePillClass(state) {
  if (state === "overdue" || state === "due") return "status-error";
  if (state === "due_soon" || state === "baseline_required") return "status-loading";
  return "status-ok";
}

function renderMaintenance(payload) {
  renderMaintenanceSummary(payload);
  const container = document.getElementById("printer-maintenance");
  const tasks = (payload.tasks || []).filter((task) => task.enabled);
  if (!tasks.length) {
    container.innerHTML = '<p class="empty-state">No maintenance intervals are configured. The project deliberately ships no unsupported manufacturer interval.</p>';
    return;
  }
  container.innerHTML = tasks.map((task) => `<article class="maintenance-card maintenance-${escapeHtml(task.state)}">
    <div class="air-station-heading"><h3>${escapeHtml(task.name)}</h3><span class="status-pill ${maintenancePillClass(task.state)}">${escapeHtml(formatLabel(task.state))}</span></div>
    <p>${escapeHtml(task.description || "No description")}</p>
    <p class="maintenance-cadence"><strong>Manufacturer cadence:</strong> ${escapeHtml(task.cadence || "Not published")}</p>
    ${task.state === "baseline_required" ? '<p class="maintenance-baseline">Needs a baseline: record when this was last physically done before the dashboard can schedule it.</p>' : ""}
    ${task.state === "advisory" ? '<p class="maintenance-baseline">Condition-based guidance. Bambu Lab publishes no numeric interval, so no due date is invented.</p>' : ""}
    <ul>${(task.triggers || []).map((trigger) => `<li>${escapeHtml(maintenanceTriggerText(trigger, task))}</li>`).join("")}</ul>
    <p class="subtle">Last complete: ${task.last_completed_at ? escapeHtml(formatDateTime(task.last_completed_at)) : "Never recorded"}${task.next_due_at ? ` · Next due: ${escapeHtml(formatDateTime(task.next_due_at))}` : ""} · Source: ${escapeHtml(formatLabel(task.provenance || "unknown"))}${task.manufacturer_source_url ? ` (<a href="${escapeHtml(task.manufacturer_source_url)}" rel="noreferrer noopener" target="_blank">Bambu Lab wiki</a>)` : ""}</p>
    ${(task.triggers || []).some((trigger) => Number(trigger.warning_threshold) > 0) ? '<p class="subtle">Local warning lead time only; the interval above is the manufacturer value.</p>' : ""}
    <button class="secondary-button maintenance-complete" type="button" data-maintenance-task="${escapeHtml(task.maintenance_task_id)}">${task.state === "baseline_required" ? "Mark completed today" : "Mark complete locally"}</button>
  </article>`).join("");
}

function maintenanceTriggerText(trigger, task) {
  const kind = String(trigger.trigger_type || "unknown");
  if (kind === "manual_inspection") {
    return `Manual check: ${task.cadence || "no published interval"}`;
  }
  if (kind === "event_after_task") {
    return `Event driven: due after ${(trigger.prerequisite_task_ids || []).map(formatLabel).join(" or ") || "a prerequisite task"}`;
  }
  if (kind === "calendar_months" || kind === "usage_tiered_calendar_months") {
    const applied = trigger.interval == null ? "unknown" : `${trigger.interval} month(s)`;
    const remaining = trigger.remaining == null ? "baseline required" : `${Number(trigger.remaining).toFixed(0)} days remaining`;
    return `${formatLabel(kind)}: every ${applied}${trigger.maintenance_mode_applied ? ` at ${formatLabel(trigger.maintenance_mode_applied)} usage` : ""} — ${remaining}`;
  }
  const current = trigger.current_accumulated_value == null ? "baseline required" : String(trigger.current_accumulated_value);
  const remaining = trigger.remaining == null ? "-" : String(trigger.remaining);
  return `${formatLabel(kind)}: ${current} / ${trigger.interval} (${remaining} remaining)`;
}

function renderPrinterHistory(history) {
  const body = document.getElementById("printer-history");
  if (!history.length) {
    body.innerHTML = '<tr><td colspan="7">No local or imported print history.</td></tr>';
    return;
  }
  body.innerHTML = history.map((item) => `<tr class="${state.selectedPrinterHistoryId === item.history_id ? "is-selected" : ""}">
    <td>${item.started_at ? escapeHtml(formatDateTime(item.started_at)) : "Unknown"}</td>
    <td><strong>${escapeHtml(item.job_name || item.design_title || "Unknown")}</strong><br><small>${escapeHtml(item.device_model || "X2D")}</small></td>
    <td>${item.duration_seconds == null ? "Unknown" : escapeHtml(formatDurationLong(item.duration_seconds))}</td>
    <td>${escapeHtml(formatLabel(item.result || "unknown"))}</td>
    <td>${escapeHtml(item.source === "bambu_cloud_history" ? "Bambu cloud history" : "Local observed")}</td>
    <td>${escapeHtml((item.materials || [item.material]).filter(Boolean).join(", ") || "Unknown")}<br><small>${escapeHtml((item.nozzle_ids || []).length ? `Nozzle ${item.nozzle_ids.join(", ")}` : item.active_tool || "Tool unknown")}</small></td>
    <td><button class="secondary-button inspect-print" type="button" data-history-id="${escapeHtml(item.history_id)}" ${item.started_at && item.ended_at ? "" : "disabled"}>Inspect</button></td>
  </tr>`).join("");
}

function renderPrinterHistoryError(message) {
  document.getElementById("printer-history").innerHTML = `<tr><td colspan="7">${escapeHtml(message)}</td></tr>`;
}

async function inspectPrintEnvironment(historyId) {
  state.selectedPrinterHistoryId = historyId;
  renderPrinterSectionError("printer-environment", "Loading retained raw SEN66 association…");
  try {
    const payload = await fetchJson(`${API.printerEnvironment}?session_id=${encodeURIComponent(historyId)}`);
    renderPrintEnvironment(payload);
  } catch (_error) {
    renderPrinterSectionError("printer-environment", "Environmental summary is temporarily unavailable.");
  }
}

function renderPrintEnvironment(payload) {
  const container = document.getElementById("printer-environment");
  if (!payload.available) {
    const reason = payload.reason === "raw_samples_expired"
      ? "Raw SEN66 samples have expired; permanent downsampled data was not substituted."
      : payload.reason === "print_interval_unknown"
        ? "The print interval is incomplete, so correlation is unavailable."
        : "No retained raw SEN66 samples are available for this interval.";
    container.innerHTML = `<p class="empty-state">${escapeHtml(reason)}</p>`;
    return;
  }
  const windows = payload.windows || {};
  const metrics = payload.metrics || {};
  container.innerHTML = `<div class="interval-markers">
    <div><strong>Baseline</strong><span>${escapeHtml(formatDateTime(windows.baseline_start))} → ${escapeHtml(formatDateTime(windows.print_start))}</span></div>
    <div><strong>Print</strong><span>${escapeHtml(formatDateTime(windows.print_start))} → ${escapeHtml(formatDateTime(windows.print_end))}</span></div>
    <div><strong>Recovery</strong><span>${escapeHtml(formatDateTime(windows.print_end))} → ${escapeHtml(formatDateTime(windows.recovery_end))}</span></div>
  </div><div class="table-wrap"><table class="environment-summary-table"><thead><tr><th>Metric</th><th>Baseline mean</th><th>Print mean</th><th>Print peak</th><th>Delta</th><th>Recovery mean</th></tr></thead><tbody>
    ${Object.entries(metrics).map(([name, values]) => `<tr><th>${escapeHtml(formatLabel(name))}</th><td>${formatMetricValue(values.baseline_mean)}</td><td>${formatMetricValue(values.print_mean)}</td><td>${formatMetricValue(values.print_peak)}</td><td>${formatMetricValue(values.change_from_baseline)}</td><td>${formatMetricValue(values.post_mean)}</td></tr>`).join("")}
  </tbody></table></div><p class="subtle">${escapeHtml((payload.limitations || ["Observational association only."]).join(" "))}</p>`;
}

function setupPrinterActions() {
  document.getElementById("panel-bambu-printer").addEventListener("click", async (event) => {
    const inspect = event.target.closest(".inspect-print");
    if (inspect && !inspect.disabled) {
      await inspectPrintEnvironment(inspect.dataset.historyId);
      return;
    }
    const completeAll = event.target.closest("#maintenance-complete-all");
    if (completeAll) {
      const confirmedAll = window.confirm("Record EVERY enabled maintenance task as completed today in the local dashboard database? This establishes the maintenance baseline and does not send any command to the printer.");
      if (!confirmedAll) return;
      completeAll.disabled = true;
      try {
        await fetchJson(`${API.printerMaintenance}/complete-all`, {
          method: "POST",
          body: JSON.stringify({ confirm: true, notes: "" }),
        });
        renderMaintenance(await fetchJson(API.printerMaintenance));
      } catch (error) {
        showError(error.message || "Could not record maintenance completion");
      } finally {
        completeAll.disabled = false;
      }
      return;
    }
    const complete = event.target.closest(".maintenance-complete");
    if (!complete) return;
    const confirmed = window.confirm("Record this maintenance task as complete in the local dashboard database? This does not send any command to the printer.");
    if (!confirmed) return;
    complete.disabled = true;
    try {
      await fetchJson(`${API.printerMaintenance}/${encodeURIComponent(complete.dataset.maintenanceTask)}/complete`, {
        method: "POST",
        body: JSON.stringify({ confirm: true, notes: "" }),
      });
      renderMaintenance(await fetchJson(API.printerMaintenance));
    } catch (error) {
      showError(error.message || "Could not record maintenance completion");
      complete.disabled = false;
    }
  });
}

function setupPrinterTelemetryControls() {
  for (const button of document.querySelectorAll(".printer-range-button")) {
    button.addEventListener("click", async () => {
      state.printerTelemetryRange = button.dataset.range;
      for (const item of document.querySelectorAll(".printer-range-button")) {
        item.classList.toggle("is-active", item === button);
      }
      await refreshPrinterTelemetry();
    });
  }
}

async function refreshPrinterTelemetry() {
  if (state.printerTelemetryInFlight) return;
  state.printerTelemetryInFlight = true;
  try {
    const data = await fetchJson(
      `${API.printerTelemetry}?range=${encodeURIComponent(state.printerTelemetryRange)}`,
    );
    state.printerTelemetryData = data;
    renderPrinterTelemetrySelector(data);
    renderPrinterTelemetryChart(data);
  } catch (error) {
    renderPrinterTelemetryError(error.message || "Historical printer telemetry is temporarily unavailable.");
  } finally {
    state.printerTelemetryInFlight = false;
  }
}

function renderPrinterTelemetrySelector(data) {
  const container = document.getElementById("printer-telemetry-metrics");
  const sources = Array.from(state.knownSources.values())
    .filter((source) => source.sensor_type === "printer" || source.sensor_type === "ams");
  const providers = new Map();
  for (const source of sources) {
    for (const field of source.available_fields || []) {
      const labels = providers.get(field) || [];
      labels.push(`${source.label}${source.status && source.status !== "online" ? ` (${source.status})` : ""}`);
      providers.set(field, labels);
    }
  }
  for (const series of data.series || []) {
    for (const field of series.available_fields || []) {
      const labels = providers.get(field) || [];
      const label = series.label || series.source_id;
      if (!labels.includes(label)) labels.push(label);
      providers.set(field, labels);
    }
  }
  const definitions = Array.from(state.fieldDefinitions.values())
    .filter((definition) => (
      definition.sensor_types?.some((type) => type === "printer" || type === "ams")
      && providers.has(definition.name)
    ));
  if (!definitions.length) {
    container.innerHTML = '<p class="empty-state">No known printer telemetry capabilities yet.</p>';
    return;
  }
  const groups = new Map();
  for (const definition of definitions) {
    const fields = groups.get(definition.group || "Measurements") || [];
    fields.push(definition);
    groups.set(definition.group || "Measurements", fields);
  }
  container.replaceChildren(...Array.from(groups, ([group, definitionsInGroup]) => {
    const section = document.createElement("section");
    section.className = "field-group";
    const heading = document.createElement("h4");
    heading.textContent = group;
    const grid = document.createElement("div");
    grid.className = "check-grid";
    for (const definition of definitionsInGroup) {
      const option = document.createElement("label");
      option.className = "check-option capability-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = definition.name;
      input.checked = state.selectedPrinterTelemetryFields.has(definition.name);
      input.addEventListener("change", () => {
        if (input.checked) state.selectedPrinterTelemetryFields.add(input.value);
        else state.selectedPrinterTelemetryFields.delete(input.value);
        renderPrinterTelemetryChart(state.printerTelemetryData || { series: [] });
      });
      const text = document.createElement("span");
      const sourceLabels = providers.get(definition.name) || [];
      text.innerHTML = `<strong>${escapeHtml(definition.label)}</strong><small>${escapeHtml(sourceLabels.join(", "))}</small>`;
      option.append(input, text);
      grid.append(option);
    }
    section.append(heading, grid);
    return section;
  }));
}

function renderPrinterTelemetryChart(data) {
  const chart = state.charts.printerTelemetry;
  if (!chart) return;
  const selectedFields = Array.from(state.selectedPrinterTelemetryFields);
  const units = Array.from(new Set(selectedFields
    .map((field) => state.fieldDefinitions.get(field)?.unit)
    .filter(Boolean)));
  const axisForUnit = new Map(units.map((unit, index) => [unit, `y${index}`]));
  const datasets = [];
  for (const source of data.series || []) {
    const sourceLabel = source.label || source.source_id;
    for (const field of selectedFields) {
      const definition = state.fieldDefinitions.get(field);
      if (!definition || !definition.sensor_types?.includes(source.sensor_type)) continue;
      const points = (source.points || [])
        .filter((point) => point[field] !== undefined && point[field] !== null)
        .map((point) => ({
          time: point.time,
          value: typeof point[field] === "boolean" ? (point[field] ? 1 : 0) : point[field],
        }));
      if (!points.length) continue;
      const color = chartPalette[datasets.length % chartPalette.length];
      datasets.push({
        label: `${sourceLabel} · ${definition.label}`,
        sourceLabel,
        measurementLabel: definition.label,
        displayUnit: definition.display_unit || "",
        valueKind: definition.unit === "boolean" ? "boolean" : "numeric",
        data: points,
        borderColor: color,
        backgroundColor: color,
        yAxisID: axisForUnit.get(definition.unit) || "y0",
        showLine: true,
        spanGaps: true,
      });
    }
  }
  chart.options.scales = {
    x: chart.options.scales.x,
    ...Object.fromEntries(units.map((unit, index) => [`y${index}`, {
      type: "linear",
      beginAtZero: unit === "boolean",
      min: unit === "boolean" ? 0 : undefined,
      max: unit === "boolean" ? 1 : undefined,
      position: index === 0 ? "left" : "right",
      offset: index > 1,
      title: { display: true, text: state.fieldDefinitions.size
        ? Array.from(state.fieldDefinitions.values()).find((field) => field.unit === unit)?.display_unit || unit
        : unit },
      grid: index === 0 ? { color: "#edf2ef" } : { drawOnChartArea: false },
    }])),
  };
  updateChart(chart, datasets, state.printerTelemetryRange);
  const tier = data.data_tier || "unknown";
  document.getElementById("printer-telemetry-tier").textContent = tier.startsWith("durable_5m")
    ? "7-day view: permanent five-minute Bambu samples, downsampled for display"
    : "Short-range view: high-resolution live Bambu telemetry, downsampled for display";
  document.getElementById("printer-telemetry-series-count").textContent = `${datasets.length} ${datasets.length === 1 ? "series" : "series"}`;
  document.getElementById("printer-telemetry-empty").hidden = datasets.length > 0;
  document.getElementById("printer-telemetry-frame").hidden = datasets.length === 0;
}

function renderPrinterTelemetryError(message) {
  document.getElementById("printer-telemetry-tier").textContent = message;
  document.getElementById("printer-telemetry-empty").textContent = message;
  document.getElementById("printer-telemetry-empty").hidden = false;
  document.getElementById("printer-telemetry-frame").hidden = true;
}

function printerTemperatureSummary(printer) {
  const parts = [];
  for (const [name, value, target] of [
    ["L", printer.nozzle_1_temperature, printer.nozzle_1_target],
    ["R", printer.nozzle_2_temperature, printer.nozzle_2_target],
    ["Bed", printer.bed_temperature, printer.bed_target],
    ["Chamber", printer.chamber_temperature, printer.chamber_target],
  ]) {
    if (Number.isFinite(value)) parts.push(`${name} ${value} °C${Number.isFinite(target) ? ` → ${target} °C` : ""}`);
  }
  return parts.join(" · ") || "Unknown";
}

function nozzleDescription(printer, index) {
  const name = index === 1 ? "Left" : "Right";
  const type = printer[`nozzle_${index}_type`] || "type unknown";
  const size = Number.isFinite(printer[`nozzle_${index}_size_mm`]) ? `${printer[`nozzle_${index}_size_mm`]} mm` : "size unknown";
  const temperature = Number.isFinite(printer[`nozzle_${index}_temperature`]) ? `${printer[`nozzle_${index}_temperature`]} °C` : "temperature unknown";
  return `${name}: ${type}, ${size}, ${temperature}`;
}

function safeColor(value) {
  const match = String(value || "").match(/^#?([0-9a-f]{6})(?:[0-9a-f]{2})?$/i);
  return match ? `#${match[1]}` : "#d1d5db";
}

function formatMetricValue(value) {
  return Number.isFinite(value) ? escapeHtml(Number(value).toFixed(3)) : "-";
}

function renderPrinterSectionError(id, message) {
  const container = document.getElementById(id);
  if (container) container.innerHTML = `<p class="empty-state">${escapeHtml(message)}</p>`;
}

async function refreshKnownSources(force = false) {
  if (document.hidden && !force) {
    return;
  }
  const latest = await fetchJson(API.latest);
  const nodes = await nodesForLatest(latest);
  state.latestData = latest;
  state.nodesData = nodes;
  updateWorkflowSources(nodes.nodes || []);
}

async function nodesForLatest(latest) {
  if (Array.isArray(latest.nodes)) {
    return { nodes: latest.nodes, generated_at: latest.generated_at };
  }

  // Compatibility fallback while the frontend and backend are being upgraded.
  return await fetchJson(API.nodes);
}

function setupNodeFilter() {
  const select = document.getElementById("node-filter");
  select.addEventListener("change", async () => {
    state.nodeFilter = select.value;
    renderChartMetricSelector();
    await refreshAll();
  });
}

function setupRangeButtons() {
  for (const button of document.querySelectorAll(".range-button")) {
    button.addEventListener("click", async () => {
      state.range = button.dataset.range;
      for (const item of document.querySelectorAll(".range-button")) {
        item.classList.toggle("is-active", item === button);
      }
      await refreshAll();
    });
  }
}

function readingsQueryParams() {
  const params = new URLSearchParams({ range: state.range });
  if (state.nodeFilter.startsWith("environment:")) {
    params.set("sensor_type", "environment");
    params.set("node_id", state.nodeFilter.split(":", 2)[1]);
  } else if (state.nodeFilter.startsWith("air_quality:")) {
    params.set("sensor_type", "air_quality");
    params.set("location", state.nodeFilter.split(":", 2)[1]);
  }
  return params;
}

async function fetchJson(url, options = {}) {
  const controller = new AbortController();
  const { timeoutMs, ...fetchOptions } = options;
  const timeout = window.setTimeout(
    () => controller.abort(),
    Number.isFinite(timeoutMs) ? timeoutMs : FETCH_TIMEOUT_MS,
  );

  try {
    const response = await fetch(url, {
      ...fetchOptions,
      headers: {
        "Accept": "application/json",
        ...(fetchOptions.body ? { "Content-Type": "application/json" } : {}),
        ...(fetchOptions.headers || {}),
      },
      signal: controller.signal,
    });

    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        if (body.message) {
          message = body.message;
        }
      } catch (_error) {
        // Keep the HTTP status message.
      }
      throw new Error(`${url}: ${message}`);
    }

    if (response.status === 204) {
      return null;
    }
    return await response.json();
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(`${url}: request timed out after ${FETCH_TIMEOUT_MS / 1000} seconds`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function setupWorkflowControllers() {
  setDefaultExportInterval();

  const duration = document.getElementById("monitoring-duration");
  duration.addEventListener("change", () => {
    document.getElementById("monitoring-custom-wrap").hidden = duration.value !== "custom";
  });
  for (const prefix of ["monitoring", "export"]) {
    document.getElementById(`${prefix}-sources`).addEventListener("change", () => {
      renderWorkflowFieldSelector(prefix);
      updateResolutionAvailability(prefix);
    });
    document.getElementById(`${prefix}-fields`).addEventListener("change", (event) => {
      if (event.target.matches('input[type="checkbox"]')) {
        const selected = state.workflowSelectedFields[prefix];
        if (event.target.checked) {
          selected.add(event.target.value);
        } else {
          selected.delete(event.target.value);
        }
      }
    });
    document.getElementById(`${prefix}-format`).addEventListener("change", () => {
      updateCsvFormatHelp(prefix);
    });
    document.getElementById(`${prefix}-resolution`).addEventListener("change", () => {
      updateResolutionHelp(prefix);
      renderWorkflowFieldSelector(prefix);
    });
    updateCsvFormatHelp(prefix);
  }
  document.getElementById("monitoring-form").addEventListener("submit", submitMonitoringSession);
  document.getElementById("export-form").addEventListener("submit", submitHistoricalExport);
  document.getElementById("active-monitoring").addEventListener("click", handleMonitoringAction);
  document.getElementById("historical-exports").addEventListener("click", handleExportAction);
}

async function refreshWorkflowOptions() {
  try {
    const data = await fetchJson(API.workflowOptions);
    state.workflowOptions = data;
    updateServerOffset(data.server_time_utc);
    state.fieldDefinitions = new Map((data.fields || []).map((field) => [field.name, field]));
    populateResolutionOptions("monitoring", data.monitoring_resolutions || []);
    populateResolutionOptions("export", data.export_resolutions || []);
    const maximumSeconds = Number(data.raw_retention_seconds);
    if (Number.isFinite(maximumSeconds) && maximumSeconds > 0) {
      document.getElementById("monitoring-custom-hours").max = String(Math.floor(maximumSeconds / 3600));
      document.getElementById("monitoring-duration-help").textContent =
        `Maximum active-monitoring duration: ${formatDurationLong(maximumSeconds)} (raw retention).`;
    }
    renderWorkflowFieldSelector("monitoring");
    renderWorkflowFieldSelector("export");
    // Bambu source visibility depends on these field definitions, so rebuild
    // the Monitoring source picker once the capability catalog has arrived.
    if (state.latestData) {
      updateNodeFilterOptions(state.latestData);
    }
    renderChartMetricSelector();
    if (state.printerTelemetryData) {
      renderPrinterTelemetrySelector(state.printerTelemetryData);
      renderPrinterTelemetryChart(state.printerTelemetryData);
    }
    updateResolutionAvailability("monitoring");
    updateResolutionAvailability("export");
  } catch (_error) {
    // Forms retain safe built-in defaults; status pollers report API availability.
  }
}

function populateResolutionOptions(prefix, options) {
  if (!options.length) {
    return;
  }
  const select = document.getElementById(`${prefix}-resolution`);
  const previous = select.value;
  select.replaceChildren(...options.map((option) => {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    element.dataset.source = option.data_source || "retained_raw";
    return element;
  }));
  select.value = options.some((option) => option.value === previous) ? previous : "raw";
  updateResolutionHelp(prefix);
}

function renderWorkflowFieldSelector(prefix) {
  const container = document.getElementById(`${prefix}-fields`);
  const selectedSources = checkedValues(`${prefix}-sources`)
    .map((key) => state.knownSources.get(key))
    .filter(Boolean);
  const help = document.getElementById(`${prefix}-capability-help`);
  if (!selectedSources.length) {
    container.replaceChildren();
    help.textContent = "Select one or more sources to see their measurements.";
    return;
  }

  const providers = new Map();
  const resolution = document.getElementById(`${prefix}-resolution`).value;
  const capabilitySources = prefix === "export" && resolution === "15m"
    ? selectedSources.filter((source) => (
      source.sensor_type === "air_quality"
      || source.sensor_type === "printer"
      || source.sensor_type === "ams"
    ))
    : selectedSources;
  for (const source of capabilitySources) {
    for (const field of source.available_fields || []) {
      const list = providers.get(field) || [];
      list.push(source.label);
      providers.set(field, list);
    }
  }
  const selectedFields = state.workflowSelectedFields[prefix];
  const definitions = Array.from(state.fieldDefinitions.values())
    .filter((definition) => (
      providers.has(definition.name)
      && (resolution === "raw" || definition.numeric_aggregation === true)
    ));
  if (!definitions.length) {
    container.innerHTML = '<p class="empty-state">No measurements were discovered for these sources.</p>';
    help.textContent = "Capabilities are derived from fields actually recorded for each source.";
    return;
  }
  const groups = new Map();
  for (const definition of definitions) {
    const group = definition.group || "Measurements";
    const list = groups.get(group) || [];
    list.push(definition);
    groups.set(group, list);
  }
  container.replaceChildren(...Array.from(groups, ([group, fields]) => {
    const section = document.createElement("section");
    section.className = "field-group";
    const heading = document.createElement("h4");
    heading.textContent = group;
    const grid = document.createElement("div");
    grid.className = "check-grid";
    for (const definition of fields) {
      const fieldProviders = providers.get(definition.name) || [];
      const label = document.createElement("label");
      label.className = "check-option capability-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = `${prefix}_field`;
      input.value = definition.name;
      input.checked = selectedFields.has(definition.name);
      const text = document.createElement("span");
      const availability = fieldProviders.length === selectedSources.length
        ? "All selected sources"
        : `${fieldProviders.length} of ${selectedSources.length}: ${fieldProviders.join(", ")}`;
      text.innerHTML = `<strong>${escapeHtml(definition.label)}</strong><small>${escapeHtml(availability)}</small>`;
      label.append(input, text);
      grid.append(label);
    }
    section.append(heading, grid);
    return section;
  }));
  help.textContent = "A measurement may be exported only for the selected sources that actually publish it.";
}

function updateWorkflowSources(nodes) {
  let changed = false;
  for (const node of nodes) {
    let source = null;
    if (node.sensor_type === "environment" && Number.isInteger(Number(node.node_id))) {
      source = {
        key: `environment:${Number(node.node_id)}`,
        sensor_type: "environment",
        node_id: Number(node.node_id),
        label: `Environment · Node ${Number(node.node_id)}`,
      };
    } else if (node.sensor_type === "air_quality" && node.location) {
      source = {
        key: `air_quality:${node.location}`,
        sensor_type: "air_quality",
        location: String(node.location),
        label: `Air quality · ${formatLabel(node.location)}`,
      };
    } else if (node.sensor_type === "printer" && node.printer_id) {
      source = {
        key: `printer:${node.printer_id}`,
        sensor_type: "printer",
        printer_id: String(node.printer_id),
        label: node.label || `Printer · ${formatLabel(node.printer_id)}`,
      };
    } else if (node.sensor_type === "ams" && node.printer_id && node.ams_id) {
      source = {
        key: `ams:${node.printer_id}/${node.ams_id}`,
        sensor_type: "ams",
        printer_id: String(node.printer_id),
        ams_id: String(node.ams_id),
        label: node.label || `${formatLabel(node.printer_id)} · ${formatLabel(node.ams_id)}`,
      };
    }
    if (source) {
      source.available_fields = Array.isArray(node.available_fields) ? node.available_fields : [];
      source.status = node.status || "unknown";
      source.last_seen = node.last_seen || null;
      const previous = state.knownSources.get(source.key);
      if (!previous || JSON.stringify(previous) !== JSON.stringify(source)) {
        state.knownSources.set(source.key, source);
        changed = true;
      }
    }
  }
  if (changed || document.querySelectorAll(".source-options input").length === 0) {
    renderSourceSelectors();
    renderWorkflowFieldSelector("monitoring");
    renderWorkflowFieldSelector("export");
  }
}

function renderSourceSelectors() {
  const sources = Array.from(state.knownSources.values())
    .sort((left, right) => left.key.localeCompare(right.key));
  for (const prefix of ["monitoring", "export"]) {
    const container = document.getElementById(`${prefix}-sources`);
    const selected = new Set(
      Array.from(container.querySelectorAll("input:checked"), (input) => input.value),
    );
    if (!sources.length) {
      container.innerHTML = '<span class="subtle">No recently known sources.</span>';
      continue;
    }
    container.replaceChildren(...sources.map((source) => {
      const label = document.createElement("label");
      label.className = "check-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = `${prefix}_source`;
      input.value = source.key;
      input.checked = selected.has(source.key);
      const text = document.createElement("span");
      text.innerHTML = `<strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(formatLabel(source.status || "unknown"))}${source.last_seen ? ` · ${escapeHtml(relativeTime(source.last_seen))}` : ""}</small>`;
      label.append(input, text);
      return label;
    }));
  }
}

function updateCsvFormatHelp(prefix) {
  const format = document.getElementById(`${prefix}-format`).value;
  document.getElementById(`${prefix}-format-help`).textContent = format === "wide"
    ? "Best for Excel, plotting, and most analysis. One sample per row with each measurement in its own column."
    : "Database-style format. Each measurement is a separate row with field/value columns.";
}

function updateResolutionAvailability(prefix) {
  const select = document.getElementById(`${prefix}-resolution`);
  if (prefix === "export") {
    const sources = checkedValues("export-sources")
      .map((key) => state.knownSources.get(key))
      .filter(Boolean);
    const stored = Array.from(select.options).find((option) => option.value === "15m");
    if (stored) {
      const hasDurableTier = sources.some((source) => (
        source.sensor_type === "air_quality"
        || source.sensor_type === "printer"
        || source.sensor_type === "ams"
      ));
      stored.disabled = sources.length > 0 && !hasDurableTier;
      stored.title = stored.disabled
        ? "A permanent 15-minute-capable tier is unavailable for these sources."
        : "Uses permanent SEN66 summaries and/or durable five-minute Bambu samples.";
      if (stored.disabled && select.value === "15m") {
        select.value = "raw";
      }
    }
  }
  updateResolutionHelp(prefix);
}

function updateResolutionHelp(prefix) {
  const select = document.getElementById(`${prefix}-resolution`);
  const selected = select.selectedOptions[0];
  const help = document.getElementById(`${prefix}-resolution-help`);
  if (!help) {
    return;
  }
  if (prefix === "export" && selected?.value === "15m") {
    const sources = checkedValues("export-sources")
      .map((key) => state.knownSources.get(key))
      .filter(Boolean);
    const hasEnvironment = sources.some((source) => source.sensor_type === "environment");
    help.textContent = hasEnvironment
      ? "Uses permanent SEN66 means and durable Bambu samples; selected environment nodes contribute no rows at this tier."
      : "Uses permanent SEN66 means and durable five-minute Bambu samples, with the tier labeled in CSV output.";
  } else if (selected?.value === "raw") {
    help.textContent = "Exports retained individual samples without numeric aggregation.";
  } else if (selected) {
    help.textContent = "Calculates arithmetic means from retained numeric samples. Boolean/status measurements are raw-only.";
  } else {
    help.textContent = "Resolution options are provided by the backend.";
  }
}

async function submitMonitoringSession(event) {
  event.preventDefault();
  const button = document.getElementById("monitoring-start");
  setWorkflowMessage("monitoring-validation", "");
  try {
    const name = document.getElementById("monitoring-name").value.trim();
    const sourceKeys = checkedValues("monitoring-sources");
    const fields = checkedValues("monitoring-fields");
    const durationSelect = document.getElementById("monitoring-duration").value;
    const durationSeconds = monitoringDurationSeconds(durationSelect);
    if (!name) {
      throw new Error("Enter a session name.");
    }
    if (!sourceKeys.length) {
      throw new Error("Select at least one sensor source.");
    }
    if (!fields.length) {
      throw new Error("Select at least one measurement.");
    }
    const minimumSeconds = Number(state.workflowOptions?.minimum_monitoring_seconds || 10);
    const maximumSeconds = Number(state.workflowOptions?.raw_retention_seconds || 72 * 3600);
    if (!Number.isInteger(durationSeconds) || durationSeconds < minimumSeconds) {
      throw new Error(`Monitoring duration must be at least ${formatDurationLong(minimumSeconds)}.`);
    }
    if (durationSeconds > maximumSeconds) {
      throw new Error(`Monitoring duration may not exceed ${formatDurationLong(maximumSeconds)}.`);
    }
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    await fetchJson(API.monitoringSessions, {
      method: "POST",
      body: JSON.stringify({
        name,
        notes: document.getElementById("monitoring-notes").value,
        duration_seconds: durationSeconds,
        sources: sourceKeys.map(sourceFromKey),
        fields,
        resolution: document.getElementById("monitoring-resolution").value,
        csv_format: document.getElementById("monitoring-format").value,
      }),
    });
    document.getElementById("monitoring-name").value = "";
    document.getElementById("monitoring-notes").value = "";
    setWorkflowMessage("monitoring-validation", "Monitoring session started.", true);
    await refreshMonitoringSessions(true);
  } catch (error) {
    setWorkflowMessage("monitoring-validation", error.message || "Could not start monitoring.");
  } finally {
    button.disabled = false;
    button.setAttribute("aria-busy", "false");
  }
}

function monitoringDurationSeconds(durationSelect) {
  if (durationSelect !== "custom") {
    return Number(durationSelect);
  }
  const hoursInput = document.getElementById("monitoring-custom-hours");
  const minutesInput = document.getElementById("monitoring-custom-minutes");
  if (hoursInput.value.trim() === "" || minutesInput.value.trim() === "") {
    throw new Error("Enter both Hours and Minutes for Custom time.");
  }
  const hours = Number(hoursInput.value);
  const minutes = Number(minutesInput.value);
  if (!Number.isInteger(hours) || hours < 0) {
    throw new Error("Custom Hours must be a whole number of 0 or more.");
  }
  if (!Number.isInteger(minutes) || minutes < 0 || minutes > 59) {
    throw new Error("Custom Minutes must be a whole number from 0 to 59.");
  }
  if (hours === 0 && minutes === 0) {
    throw new Error("Custom time must be greater than zero.");
  }
  return hours * 3600 + minutes * 60;
}

async function submitHistoricalExport(event) {
  event.preventDefault();
  const button = document.getElementById("export-create");
  setWorkflowMessage("export-validation", "");
  try {
    const name = document.getElementById("export-name").value.trim();
    const sourceKeys = checkedValues("export-sources");
    const fields = checkedValues("export-fields");
    const start = localInputToIso(document.getElementById("export-start").value);
    const end = localInputToIso(document.getElementById("export-end").value);
    if (!name) {
      throw new Error("Enter an export name.");
    }
    if (!start || !end) {
      throw new Error("Choose valid start and end date-times.");
    }
    if (Date.parse(end) <= Date.parse(start)) {
      throw new Error("End time must be later than start time.");
    }
    if (!sourceKeys.length) {
      throw new Error("Select at least one sensor source.");
    }
    if (!fields.length) {
      throw new Error("Select at least one measurement.");
    }
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    await fetchJson(API.exports, {
      method: "POST",
      body: JSON.stringify({
        name,
        start_time: start,
        end_time: end,
        sources: sourceKeys.map(sourceFromKey),
        fields,
        resolution: document.getElementById("export-resolution").value,
        csv_format: document.getElementById("export-format").value,
      }),
    });
    document.getElementById("export-name").value = "";
    setWorkflowMessage("export-validation", "Export queued. It will continue if this page closes.", true);
    await refreshExportJobs(true);
  } catch (error) {
    setWorkflowMessage("export-validation", error.message || "Could not queue export.");
  } finally {
    button.disabled = false;
    button.setAttribute("aria-busy", "false");
  }
}

async function refreshMonitoringSessions(force = false) {
  if (state.monitoringInFlight
      || ((!force && state.activeTab !== "active-monitoring") || (document.hidden && !force))) {
    return;
  }
  state.monitoringInFlight = true;
  const pollState = document.getElementById("monitoring-poll-state");
  try {
    const data = await fetchJson(API.monitoringSessions);
    updateServerOffset(data.server_time_utc);
    state.monitoringSessions = data.sessions || [];
    renderMonitoringSessions();
    pollState.textContent = "Session status current";
  } catch (error) {
    pollState.textContent = error.message || "Session status unavailable";
  } finally {
    state.monitoringInFlight = false;
  }
}

async function refreshMonitoringPreviews(force = false) {
  if (state.previewInFlight
      || ((!force && state.activeTab !== "active-monitoring") || (document.hidden && !force))) {
    return;
  }
  const running = state.monitoringSessions.filter((session) => session.status === "running");
  if (!running.length) {
    return;
  }
  state.previewInFlight = true;
  try {
    for (const session of running) {
      const updated = await fetchJson(`${API.monitoringSessions}/${session.id}/preview`);
      updateServerOffset(updated.server_time_utc);
      const index = state.monitoringSessions.findIndex((item) => item.id === updated.id);
      if (index >= 0) {
        state.monitoringSessions[index] = updated;
      }
    }
    renderMonitoringSessions();
  } catch (error) {
    document.getElementById("monitoring-poll-state").textContent =
      error.message || "Preview unavailable";
  } finally {
    state.previewInFlight = false;
  }
}

async function refreshExportJobs(force = false) {
  if (state.exportsInFlight
      || ((!force && state.activeTab !== "active-monitoring") || (document.hidden && !force))) {
    return;
  }
  state.exportsInFlight = true;
  const pollState = document.getElementById("export-poll-state");
  try {
    const data = await fetchJson(API.exports);
    updateServerOffset(data.server_time_utc);
    state.exportJobs = data.exports || [];
    renderExportJobs();
    pollState.textContent = "Export status current";
  } catch (error) {
    pollState.textContent = error.message || "Export status unavailable";
  } finally {
    state.exportsInFlight = false;
  }
}

function renderMonitoringSessions() {
  const running = state.monitoringSessions.filter((session) => session.status === "running");
  const current = document.getElementById("monitoring-current");
  current.innerHTML = running.length
    ? running.map(monitoringCardHtml).join("")
    : '<p class="empty-state">No running monitoring sessions.</p>';

  const table = document.getElementById("monitoring-sessions-table");
  table.innerHTML = state.monitoringSessions.length
    ? state.monitoringSessions.map(monitoringTableRowHtml).join("")
    : '<tr><td colspan="6">No monitoring sessions yet.</td></tr>';
  updateVisibleWorkflowClocks();
}

function monitoringCardHtml(session) {
  const preview = session.preview || {};
  const exportJob = session.export;
  const progress = session.duration_seconds
    ? Math.min(100, session.elapsed_seconds / session.duration_seconds * 100)
    : 0;
  return `
    <article class="workflow-card">
      <div class="workflow-card-heading">
        <h4>${escapeHtml(session.name)}</h4>
        ${workflowStatusHtml(session.status)}
      </div>
      <dl class="workflow-facts">
        <div><dt>Started</dt><dd>${escapeHtml(formatDateTime(session.start_time_utc))}</dd></div>
        <div><dt>Scheduled end</dt><dd>${escapeHtml(formatDateTime(session.scheduled_end_time_utc))}</dd></div>
        <div><dt>Elapsed</dt><dd id="monitor-elapsed-${session.id}">${escapeHtml(formatElapsedClock(session.elapsed_seconds))}</dd></div>
        <div><dt>Remaining</dt><dd id="monitor-remaining-${session.id}">${escapeHtml(formatElapsedClock(session.remaining_seconds))}</dd></div>
        <div><dt>Sources</dt><dd>${escapeHtml(sourceSummary(session.selected_sources))}</dd></div>
        <div><dt>Fields</dt><dd>${escapeHtml(session.selected_fields.join(", "))}</dd></div>
        <div><dt>Recent values</dt><dd>${escapeHtml(preview.row_count ?? "Collecting")}${preview.row_count_is_approximate ? " approx." : ""}</dd></div>
        <div><dt>Latest sample</dt><dd>${escapeHtml(formatDateTime(preview.latest_sample_timestamp))}</dd></div>
        <div><dt>CSV</dt><dd>${escapeHtml(exportJob ? `${formatLabel(exportJob.status)} · ${exportJob.rows_written} rows` : "Queued when session ends")}</dd></div>
        <div><dt>CSV elapsed</dt><dd id="session-export-elapsed-${session.id}">${escapeHtml(formatElapsedClock(exportJob?.active_elapsed_seconds || 0))}</dd></div>
      </dl>
      <div class="workflow-progress" aria-label="Monitoring interval progress"><span style="--progress-width: ${progress.toFixed(2)}%"></span></div>
      <div class="workflow-actions">
        <button class="danger-button" type="button" data-action="stop-session" data-id="${session.id}">Stop early</button>
        ${exportJob?.is_download_ready ? downloadLinkHtml(exportJob) : ""}
      </div>
    </article>
  `;
}

function monitoringTableRowHtml(session) {
  const exportJob = session.export;
  const canDelete = session.status !== "running"
    && !["running", "cancel_requested"].includes(exportJob?.status);
  return `
    <tr>
      <td><strong>${escapeHtml(session.name)}</strong>${session.notes ? `<br><span class="subtle">${escapeHtml(session.notes)}</span>` : ""}</td>
      <td>${workflowStatusHtml(session.status)}</td>
      <td>${escapeHtml(formatDateTime(session.start_time_utc))}<br><span class="subtle">to ${escapeHtml(formatDateTime(session.effective_end_time_utc))}<br><span id="table-monitor-elapsed-${session.id}">${escapeHtml(formatElapsedClock(session.elapsed_seconds))}</span></span></td>
      <td>${session.source_count} / ${session.field_count}<br><span class="subtle">${escapeHtml(session.selected_fields.join(", "))}</span></td>
      <td>${exportJob ? `${workflowStatusHtml(exportJob.status)}<br><span class="subtle">${exportJob.rows_written} rows</span>` : "Not queued"}</td>
      <td><div class="workflow-actions">
        ${session.status === "running" ? `<button class="danger-button" type="button" data-action="stop-session" data-id="${session.id}">Stop</button>` : ""}
        ${exportJob?.is_download_ready ? downloadLinkHtml(exportJob) : ""}
        ${canDelete ? `<button class="danger-button" type="button" data-action="delete-session" data-id="${session.id}">Delete</button>` : ""}
      </div></td>
    </tr>
  `;
}

function renderExportJobs() {
  const container = document.getElementById("export-jobs");
  container.innerHTML = state.exportJobs.length
    ? state.exportJobs.map(exportCardHtml).join("")
    : '<p class="empty-state">No export jobs.</p>';
  updateVisibleWorkflowClocks();
}

function exportCardHtml(job) {
  const hasUnits = job.work_units_total > 0;
  const progress = hasUnits ? Math.min(100, job.work_units_completed / job.work_units_total * 100) : 0;
  const active = ["running", "cancel_requested"].includes(job.status);
  const zeroSources = (job.source_results || []).filter((source) => source.status === "zero_data");
  const canDelete = ["completed", "failed", "cancelled"].includes(job.status)
    && !job.monitoring_session_id;
  return `
    <article class="workflow-card">
      <div class="workflow-card-heading">
        <h4>${escapeHtml(job.name)}</h4>
        ${workflowStatusHtml(job.status)}
      </div>
      <dl class="workflow-facts">
        <div><dt>Interval</dt><dd>${escapeHtml(formatDateTime(job.start_time_utc))}<br>to ${escapeHtml(formatDateTime(job.end_time_utc))}</dd></div>
        <div><dt>Origin</dt><dd>${job.monitoring_session_id ? "Active Monitoring" : "Historical export"}</dd></div>
        <div><dt>Phase</dt><dd>${escapeHtml(formatLabel(job.current_phase))}</dd></div>
        <div><dt>Work</dt><dd>${job.work_units_completed} of ${job.work_units_total || "?"} chunks/batches</dd></div>
        <div><dt>Queued</dt><dd id="export-queued-${job.id}">${escapeHtml(formatElapsedClock(job.queued_elapsed_seconds))}</dd></div>
        <div><dt>Active</dt><dd id="export-active-${job.id}">${escapeHtml(formatElapsedClock(job.active_elapsed_seconds))}</dd></div>
        <div><dt>Total</dt><dd id="export-total-${job.id}">${escapeHtml(formatElapsedClock(job.total_elapsed_seconds))}</dd></div>
        <div><dt>Output</dt><dd>${job.rows_written.toLocaleString()} rows · ${escapeHtml(formatBytes(job.output_size_bytes))}</dd></div>
        <div><dt>Sources / fields</dt><dd>${job.source_count} / ${job.field_count}</dd></div>
        <div><dt>Tier / format</dt><dd>${escapeHtml(job.resolution)} · ${escapeHtml(job.csv_format)}</dd></div>
      </dl>
      <div class="workflow-progress ${active && !hasUnits ? "is-indeterminate" : ""}" aria-label="Export work progress"><span style="--progress-width: ${progress.toFixed(2)}%"></span></div>
      ${job.warnings?.length ? `<ul class="workflow-warnings">${job.warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>` : ""}
      ${zeroSources.length ? `<ul class="source-results"><li>Zero data: ${escapeHtml(zeroSources.map((source) => source.source_id).join(", "))}</li></ul>` : ""}
      ${job.error_message ? `<p class="form-message">${escapeHtml(job.error_message)}</p>` : ""}
      <div class="workflow-actions">
        ${["queued", "running"].includes(job.status) ? `<button class="danger-button" type="button" data-action="cancel-export" data-id="${job.id}">Cancel</button>` : ""}
        ${job.is_download_ready ? downloadLinkHtml(job) : ""}
        ${canDelete ? `<button class="danger-button" type="button" data-action="delete-export" data-id="${job.id}">Delete</button>` : ""}
      </div>
    </article>
  `;
}

async function handleMonitoringAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) {
    return;
  }
  const id = button.dataset.id;
  button.disabled = true;
  try {
    if (button.dataset.action === "stop-session") {
      await fetchJson(`${API.monitoringSessions}/${id}/stop`, { method: "POST" });
      setWorkflowMessage("monitoring-validation", "Monitoring session stopped; its CSV was queued.", true);
    } else if (button.dataset.action === "delete-session") {
      await fetchJson(`${API.monitoringSessions}/${id}`, { method: "DELETE" });
      setWorkflowMessage("monitoring-validation", "Monitoring session and associated export were deleted.", true);
    }
    await Promise.all([refreshMonitoringSessions(true), refreshExportJobs(true)]);
  } catch (error) {
    setWorkflowMessage("monitoring-validation", error.message || "Monitoring action failed.");
  } finally {
    button.disabled = false;
  }
}

async function handleExportAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) {
    return;
  }
  const id = button.dataset.id;
  button.disabled = true;
  try {
    if (button.dataset.action === "cancel-export") {
      await fetchJson(`${API.exports}/${id}/cancel`, { method: "POST" });
      setWorkflowMessage("export-validation", "Cancellation requested.", true);
    } else if (button.dataset.action === "delete-export") {
      await fetchJson(`${API.exports}/${id}`, { method: "DELETE" });
      setWorkflowMessage("export-validation", "Export metadata and files were deleted.", true);
    }
    await Promise.all([refreshExportJobs(true), refreshMonitoringSessions(true)]);
  } catch (error) {
    setWorkflowMessage("export-validation", error.message || "Export action failed.");
  } finally {
    button.disabled = false;
  }
}

function updateVisibleWorkflowClocks() {
  const now = Date.now() + state.serverOffsetMs;
  for (const session of state.monitoringSessions) {
    const start = Date.parse(session.start_time_utc);
    const scheduled = Date.parse(session.scheduled_end_time_utc);
    const running = session.status === "running";
    const effective = running
      ? Math.min(now, scheduled)
      : Date.parse(session.effective_end_time_utc);
    const elapsed = Number.isFinite(start) && Number.isFinite(effective)
      ? Math.max(0, Math.floor((effective - start) / 1000))
      : session.elapsed_seconds;
    const remaining = running && Number.isFinite(scheduled)
      ? Math.max(0, Math.floor((scheduled - now) / 1000))
      : 0;
    setTextIfPresent(`monitor-elapsed-${session.id}`, formatElapsedClock(elapsed));
    setTextIfPresent(`table-monitor-elapsed-${session.id}`, formatElapsedClock(elapsed));
    setTextIfPresent(`monitor-remaining-${session.id}`, formatElapsedClock(remaining));
    if (session.export) {
      setTextIfPresent(
        `session-export-elapsed-${session.id}`,
        formatElapsedClock(exportActiveSeconds(session.export, now)),
      );
    }
  }
  for (const job of state.exportJobs) {
    setTextIfPresent(`export-queued-${job.id}`, formatElapsedClock(exportQueuedSeconds(job, now)));
    setTextIfPresent(`export-active-${job.id}`, formatElapsedClock(exportActiveSeconds(job, now)));
    setTextIfPresent(`export-total-${job.id}`, formatElapsedClock(exportTotalSeconds(job, now)));
  }
}

function exportQueuedSeconds(job, now) {
  const created = Date.parse(job.created_at_utc);
  const end = job.started_at_utc
    ? Date.parse(job.started_at_utc)
    : (job.completed_at_utc ? Date.parse(job.completed_at_utc) : now);
  return finiteSeconds(created, end, job.queued_elapsed_seconds);
}

function exportActiveSeconds(job, now) {
  if (!job.started_at_utc) {
    return 0;
  }
  const start = Date.parse(job.started_at_utc);
  const end = job.completed_at_utc ? Date.parse(job.completed_at_utc) : now;
  return finiteSeconds(start, end, job.active_elapsed_seconds);
}

function exportTotalSeconds(job, now) {
  const start = Date.parse(job.created_at_utc);
  const end = job.completed_at_utc ? Date.parse(job.completed_at_utc) : now;
  return finiteSeconds(start, end, job.total_elapsed_seconds);
}

function finiteSeconds(start, end, fallback = 0) {
  return Number.isFinite(start) && Number.isFinite(end)
    ? Math.max(0, Math.floor((end - start) / 1000))
    : Number(fallback || 0);
}

function updateServerOffset(serverTime) {
  const parsed = Date.parse(serverTime);
  if (Number.isFinite(parsed)) {
    state.serverOffsetMs = parsed - Date.now();
  }
}

function checkedValues(containerId) {
  return Array.from(
    document.querySelectorAll(`#${containerId} input[type="checkbox"]:checked`),
    (input) => input.value,
  );
}

function sourceFromKey(key) {
  const source = state.knownSources.get(key);
  if (!source) {
    throw new Error(`Unknown source selection: ${key}`);
  }
  if (source.sensor_type === "environment") {
    return { sensor_type: "environment", node_id: source.node_id };
  }
  if (source.sensor_type === "air_quality") {
    return { sensor_type: "air_quality", location: source.location };
  }
  if (source.sensor_type === "printer") {
    return { sensor_type: "printer", printer_id: source.printer_id };
  }
  return {
    sensor_type: "ams",
    printer_id: source.printer_id,
    ams_id: source.ams_id,
  };
}

function setWorkflowMessage(id, message, success = false) {
  const element = document.getElementById(id);
  element.textContent = message;
  element.hidden = !message;
  element.classList.toggle("is-success", Boolean(message && success));
}

function setDefaultExportInterval() {
  const end = new Date();
  end.setSeconds(0, 0);
  const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
  document.getElementById("export-start").value = localDateTimeValue(start);
  document.getElementById("export-end").value = localDateTimeValue(end);
}

function localDateTimeValue(date) {
  const parts = [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
    String(date.getHours()).padStart(2, "0"),
    String(date.getMinutes()).padStart(2, "0"),
  ];
  return `${parts[0]}-${parts[1]}-${parts[2]}T${parts[3]}:${parts[4]}`;
}

function localInputToIso(value) {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function workflowStatusHtml(status) {
  return `<span class="workflow-status workflow-status-${escapeHtml(status)}">${escapeHtml(formatLabel(status))}</span>`;
}

function downloadLinkHtml(job) {
  return `<a class="link-button" href="${escapeHtml(job.download_url)}" download>Download CSV</a>`;
}

function workflowFieldLabel(field) {
  return state.fieldDefinitions.get(field)?.label || formatLabel(field);
}

function sourceSummary(sources) {
  return (sources || []).map((source) => {
    if (source.sensor_type === "environment") return `Node ${source.node_id}`;
    if (source.sensor_type === "air_quality") return formatLabel(source.location);
    if (source.sensor_type === "printer") return `Printer ${formatLabel(source.printer_id)}`;
    return `${formatLabel(source.printer_id)} · ${formatLabel(source.ams_id)}`;
  }).join(", ");
}

function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function formatElapsedClock(value) {
  const total = Math.max(0, Math.floor(Number(value) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor(total % 86400 / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  const seconds = total % 60;
  const clock = [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
  return days ? `${days}d ${clock}` : clock;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function setTextIfPresent(id, value) {
  const element = document.getElementById(id);
  if (element) {
    element.textContent = value;
  }
}

// The shared Monitoring graph plots numeric lines. A field is graphable there
// only if the backend capability catalog marks it as numerically aggregatable,
// which is what keeps boolean status fields (and therefore an `ams_active`-only
// external spool) out of the pickers without naming any source or field here.
function graphableFieldsFor(entity) {
  const advertised = entity && Array.isArray(entity.available_fields) ? entity.available_fields : [];
  return advertised.filter((name) => state.fieldDefinitions.get(name)?.numeric_aggregation === true);
}

function isBambuSensorType(sensorType) {
  return sensorType === "printer" || sensorType === "ams";
}

function bambuFilterKey(entity) {
  return entity.sensor_type === "printer"
    ? `printer:${entity.printer_id}`
    : `ams:${entity.printer_id}/${entity.ams_id}`;
}

function bambuFilterLabel(entity) {
  return entity.sensor_type === "printer"
    ? `Printer · ${formatLabel(entity.printer_id)}`
    : `AMS · ${formatLabel(entity.printer_id)} · ${formatLabel(entity.ams_id)}`;
}

// Telemetry query for the current source filter, or null when the selection
// cannot contain Bambu data (an environment/air-quality node, or no graphable
// Bambu source discovered yet).
function printerTelemetryChartQuery() {
  const filter = state.nodeFilter;
  if (filter.startsWith("environment:") || filter.startsWith("air_quality:")) {
    return null;
  }
  const params = new URLSearchParams({ range: state.range });
  if (filter.startsWith("printer:")) {
    params.set("sensor_type", "printer");
    params.set("printer_id", filter.slice("printer:".length));
  } else if (filter.startsWith("ams:")) {
    const [printerId, amsId] = filter.slice("ams:".length).split("/", 2);
    if (!printerId || !amsId) return null;
    params.set("sensor_type", "ams");
    params.set("printer_id", printerId);
    params.set("ams_id", amsId);
  } else if (!Array.from(state.knownSources.values())
    .some((source) => isBambuSensorType(source.sensor_type) && graphableFieldsFor(source).length)) {
    return null;
  }
  return `${API.printerTelemetry}?${params.toString()}`;
}

function chartSeriesMatchesFilter(item) {
  const filter = state.nodeFilter;
  if (filter === "all") return true;
  const separator = filter.indexOf(":");
  const type = filter.slice(0, separator);
  const identity = filter.slice(separator + 1);
  if (type === "environment") return item.sensor_type === "environment" && String(item.node_id) === identity;
  if (type === "air_quality") return item.sensor_type === "air_quality" && String(item.location ?? item.id) === identity;
  if (type === "printer") return item.sensor_type === "printer" && String(item.printer_id) === identity;
  if (type === "ams") return item.sensor_type === "ams" && String(item.source_id ?? item.id) === identity;
  return true;
}

function chartSourceLabel(item) {
  if (item.sensor_type === "environment") return `Node ${item.node_id}`;
  if (isBambuSensorType(item.sensor_type)) return item.label || item.source_id || item.id;
  return formatLabel(item.location || item.id);
}

function updateNodeFilterOptions(data) {
  const select = document.getElementById("node-filter");
  const environmentNodes = (data.environment || [])
    .filter((reading) => reading.node_id !== undefined && reading.node_id !== null)
    .map((reading) => ({
      value: `environment:${reading.node_id}`,
      label: `Node ${reading.node_id}`,
    }));
  const airStations = (data.air_quality || [])
    .filter((reading) => reading.location)
    .map((reading) => ({
      value: `air_quality:${reading.location}`,
      label: `SEN66 · ${formatLabel(reading.location)}`,
    }));
  // Printer/AMS are first-class historical sources too. Only those with at
  // least one graphable numeric field are offered here; the rest stay in the
  // shared source catalog for Active Monitoring and exports.
  const bambuSources = [...(data.printer || []), ...(data.ams || [])]
    .filter((entity) => entity.printer_id && graphableFieldsFor(entity).length > 0)
    .map((entity) => ({ value: bambuFilterKey(entity), label: bambuFilterLabel(entity) }));
  const sensorOptions = [...environmentNodes, ...airStations, ...bambuSources];

  const options = [
    { value: "all", label: "All sensors" },
    ...sensorOptions,
  ];

  if (state.nodeFilter !== "all" && !sensorOptions.some((item) => item.value === state.nodeFilter)) {
    options.push({ value: state.nodeFilter, label: state.nodeFilter });
  }

  const currentOptions = Array.from(select.options).map((option) => `${option.value}:${option.textContent}`);
  const nextOptions = options.map((option) => `${option.value}:${option.label}`);
  if (currentOptions.join("|") === nextOptions.join("|")) {
    select.value = state.nodeFilter;
    return;
  }

  select.replaceChildren(...options.map((option) => {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    return element;
  }));
  select.value = state.nodeFilter;
}

function initializeCharts() {
  state.charts.history = createLineChart("history-chart");
  state.charts.printerTelemetry = createLineChart("printer-telemetry-chart");
}

function createLineChart(canvasId) {
  const context = document.getElementById(canvasId).getContext("2d");
  return new Chart(context, {
    type: "line",
    data: {
      labels: [],
      datasets: [],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: "nearest",
        intersect: false,
      },
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            boxWidth: 12,
            boxHeight: 12,
            usePointStyle: true,
          },
        },
        title: {
          display: false,
          text: "Selected historical measurements",
        },
        tooltip: {
          callbacks: {
            title(items) {
              const raw = items[0]?.dataset?.rawTimes?.[items[0].dataIndex];
              return raw ? formatDateTime(raw) : "";
            },
            label(context) {
              const dataset = context.dataset;
              const value = context.parsed.y;
              const formatted = dataset.valueKind === "boolean"
                ? (value === 1 ? "On" : "Off")
                : `${value} ${dataset.displayUnit}`.trim();
              return `${dataset.sourceLabel} · ${dataset.measurementLabel}: ${formatted}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            maxRotation: 0,
            autoSkip: true,
            maxTicksLimit: 8,
          },
          grid: {
            display: false,
          },
        },
        y: {
          beginAtZero: false,
          grid: { color: "#edf2ef" },
        },
      },
      elements: {
        line: {
          borderWidth: 2,
          tension: 0.25,
        },
        point: {
          radius: 0,
          hitRadius: 8,
          hoverRadius: 4,
        },
      },
    },
  });
}

function renderChartMetricSelector() {
  const container = document.getElementById("chart-metrics");
  if (!container || !state.fieldDefinitions.size || !state.knownSources.size) {
    return;
  }
  const applicableSources = state.nodeFilter === "all"
    ? Array.from(state.knownSources.values())
    : [state.knownSources.get(state.nodeFilter)].filter(Boolean);
  const available = new Set(applicableSources.flatMap(graphableFieldsFor));
  const definitions = Array.from(state.fieldDefinitions.values())
    .filter((definition) => available.has(definition.name));
  if (!definitions.length) {
    container.innerHTML = '<span class="subtle">No graphable measurements are available for this source.</span>';
    return;
  }
  container.replaceChildren(...definitions.map((definition) => {
    const label = document.createElement("label");
    label.className = "check-option chart-metric-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = definition.name;
    input.checked = state.selectedChartFields.has(definition.name);
    input.addEventListener("change", () => {
      if (input.checked) {
        state.selectedChartFields.add(definition.name);
      } else {
        state.selectedChartFields.delete(definition.name);
      }
      if (state.readingsData) {
        renderCharts(state.readingsData);
      }
    });
    const text = document.createElement("span");
    text.textContent = `${definition.label}${definition.display_unit ? ` (${definition.display_unit})` : ""}`;
    label.append(input, text);
    return label;
  }));
}

function renderLatest(data) {
  const grid = document.getElementById("current-grid");
  const readings = [
    ...(data.environment || []),
    ...(data.air_quality || []),
  ].sort((left, right) => {
    const rank = { online: 0, stale: 1, offline: 2, unknown: 3 };
    return (rank[left.status] ?? 3) - (rank[right.status] ?? 3);
  });

  if (readings.length === 0) {
    grid.innerHTML = '<div class="empty-state">No readings found in InfluxDB.</div>';
    return;
  }

  grid.replaceChildren(...readings.map(readingCard));
}

function readingCard(reading) {
  const card = document.createElement("article");
  card.className = "reading-card";
  const batteryState = batteryStateFor(reading);
  if (batteryState === "low" || batteryState === "shutdown") {
    card.classList.add(`battery-${batteryState}`);
  }
  const title = reading.sensor_type === "environment"
    ? `Node ${reading.node_id}`
    : formatLabel(reading.location || reading.id);
  const isEnvironment = reading.sensor_type === "environment";
  const current = reading.values_are_current === true;
  const sensorStatus = reading.status || "unknown";
  if (!current) {
    card.classList.add("reading-card-inactive");
  }

  if (!isEnvironment) {
    card.classList.add("reading-card-air");
    const overall = reading.overall_status || {};
    card.innerHTML = `
      <div class="air-station-heading">
        <div>
          <h3>${escapeHtml(title)}</h3>
          <span class="authority-label">SEN66 · ${current ? "live 5-second feed" : "last stored measurements"}</span>
        </div>
        <div class="sensor-card-status">
          <span class="status-pill ${sensorStatusPillClass(sensorStatus)}">${escapeHtml(formatLabel(sensorStatus))}</span>
          <span class="metric-small">Last seen ${escapeHtml(relativeTime(reading.last_seen))}</span>
        </div>
      </div>
      ${current ? `
        <span class="interpretation-status severity-${escapeHtml(overall.severity || "unavailable")}">
          Room summary: ${escapeHtml(overall.category || "Unavailable")}
          ${overall.driving_metric ? ` · driven by ${escapeHtml(formatLabel(overall.driving_metric))}` : ""}
        </span>` : '<span class="reading-warning">Last-known values below are not current.</span>'}
      <div class="air-reading-groups">
        ${AIR_QUALITY_METRIC_GROUPS.map((group) => airMetricGroupHtml(reading, group)).join("")}
      </div>
      ${advancedDiagnosticsHtml(reading)}
      <div class="metric-small">Station last seen ${escapeHtml(relativeTime(reading.last_seen))}</div>
    `;
    return card;
  }

  card.innerHTML = `
    <div class="sensor-card-heading">
      <h3>${escapeHtml(title)}</h3>
      <span class="status-pill ${sensorStatusPillClass(sensorStatus)}">${escapeHtml(formatLabel(sensorStatus))}</span>
    </div>
    ${current ? "" : '<span class="reading-warning">Last-known values below are not current.</span>'}
    <div class="reading-values">
      ${metricHtml("Temp", formatNumber(reading.temperature_c, 1, " °C"))}
      ${metricHtml("Humidity", formatNumber(reading.humidity, 1, "%"))}
      ${metricHtml("Battery", batteryDisplay(reading))}
      ${statusFlagsMetricHtml(reading)}
    </div>
    ${batteryAlertHtml(reading)}
    <div class="metric-small">Last seen ${escapeHtml(relativeTime(reading.last_seen))}</div>
  `;
  return card;
}

function airMetricGroupHtml(reading, group) {
  const metrics = group.metrics
    .map((metric) => airMetricHtml(reading, metric))
    .join("");

  return `
    <section class="air-metric-group" aria-label="${escapeHtml(group.label)}">
      <h4>${escapeHtml(group.label)}</h4>
      <div class="reading-values">${metrics}</div>
    </section>
  `;
}

function airMetricHtml(reading, metric) {
  const interpretation = (reading.interpretations || {})[metric.interpretation] || {};
  const value = formatNumber(reading[metric.field], metric.digits, metric.suffix);
  const severity = interpretation.severity || "unavailable";
  const warnings = [];
  if (interpretation.is_stale) {
    warnings.push('<span class="reading-warning">Stale — status withheld</span>');
  }
  if (reading.sample_valid === false) {
    warnings.push('<span class="reading-warning">Latest sensor sample invalid — status withheld</span>');
  }
  if (interpretation.is_warming_up) {
    warnings.push('<span class="reading-warning">Sensor warming up / adapting</span>');
  }
  const activeEvents = (reading.active_events || [])
    .filter((event) => event.metric === metric.field)
    .map((event) => formatLabel(event.event_type));
  if (activeEvents.length) {
    warnings.push(`<span class="reading-warning">Active event: ${escapeHtml(activeEvents.join(", "))}</span>`);
  }

  return `
    <article class="air-metric-card severity-border-${escapeHtml(severity)}"
      aria-label="${escapeHtml(metric.label)}: ${escapeHtml(value)}; ${escapeHtml(interpretation.category || "Unavailable")}">
      <div class="metric-card-heading">
        <span class="metric-label">${escapeHtml(metric.label)}</span>
        <span class="interpretation-status severity-${escapeHtml(severity)}">${escapeHtml(interpretation.category || "Unavailable")}</span>
      </div>
      <span class="metric-value">${escapeHtml(value)}</span>
      <span class="authority-label">${escapeHtml(interpretation.framework || "Interpretation unavailable")}</span>
      <p class="metric-explanation">${escapeHtml(interpretation.explanation || "No valid current interpretation.")}</p>
      ${airMetricStatsHtml(reading, metric)}
      ${warnings.join("")}
      <span class="metric-small">Updated ${escapeHtml(relativeTime(interpretation.updated_at || reading.last_seen))}</span>
      <details class="metric-details">
        <summary>Source and limitations</summary>
        <p><strong>${escapeHtml(interpretation.source_name || "Source unavailable")}</strong> — ${escapeHtml(interpretation.source_document || "")}, revision ${escapeHtml(interpretation.source_revision || "unknown")}.</p>
        <p>${escapeHtml(interpretation.limitation || "")}</p>
        ${interpretation.source_url ? `<a href="${escapeHtml(interpretation.source_url)}" target="_blank" rel="noopener noreferrer">Open primary source</a>` : ""}
      </details>
    </article>
  `;
}

function airMetricStatsHtml(reading, metric) {
  const summary = reading.summary_15m || {};
  const stats = [];
  const mean = summary[`${metric.field}_mean`];
  const maximum = summary[`${metric.field}_max`];
  const minimum = summary[`${metric.field}_min`];
  if (mean !== undefined) {
    stats.push(`15m mean ${formatNumber(mean, metric.digits, metric.suffix)}`);
  }
  if (["co2", "pm1", "pm25", "pm4", "pm10", "voc_index", "nox_index"].includes(metric.field)
      && maximum !== undefined) {
    stats.push(`15m max ${formatNumber(maximum, metric.digits, metric.suffix)}`);
  }
  if (metric.field === "voc_index" && minimum !== undefined) {
    stats.push(`15m min ${formatNumber(minimum, 0, "")}`);
    if (reading.voc_index !== undefined && reading.voc_index !== null) {
      stats.push(`current − 100: ${signedNumber(Number(reading.voc_index) - 100, 0)}`);
    }
  }
  const change = summary[`${metric.field}_change_from_previous_window`];
  if (["voc_index", "nox_index"].includes(metric.field) && change !== undefined) {
    stats.push(`vs previous 15m: ${signedNumber(change, 1)}`);
  }
  const trend = summary[`${metric.field}_trend`];
  if (trend) {
    stats.push(`trend ${trend}`);
  }
  if (["pm25", "pm10"].includes(metric.field)) {
    const rolling = reading.rolling_24h || {};
    const average = rolling[`${metric.field}_average`];
    stats.push(average === undefined || average === null
      ? `24h estimate unavailable (${formatNumber(rolling.sample_coverage_percent, 0, "% coverage")})`
      : `24h avg ${formatNumber(average, 1, " µg/m³")} (${formatNumber(rolling.sample_coverage_percent, 0, "% coverage")})`);
    const epa = (reading.interpretations || {})[`${metric.field}_24h`];
    const who = (reading.interpretations || {})[`${metric.field}_who_24h`];
    if (epa) {
      stats.push(`EPA: ${epa.category}`);
    }
    if (who) {
      stats.push(`WHO: ${who.category}`);
    }
  }
  if (metric.field === "voc_index") {
    stats.push(`time ≥150: ${formatDuration(summary.voc_duration_above_150_seconds)}`);
  }
  if (metric.field === "nox_index") {
    stats.push(`time ≥20: ${formatDuration(summary.nox_duration_above_20_seconds)}`);
  }
  if (["voc_index", "nox_index"].includes(metric.field)) {
    const active = (reading.active_events || []).some((event) => event.metric === metric.field);
    stats.push(active ? "event active" : "no active event");
  }
  if (metric.field === "co2") {
    const exposure = (reading.interpretations || {}).co2_occupational;
    if (exposure) {
      stats.push(`exposure context: ${exposure.category}`);
    }
  }
  if (!stats.length) {
    return '<div class="metric-context">15-minute context is collecting.</div>';
  }
  return `<div class="metric-context">${stats.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`;
}

function advancedDiagnosticsHtml(reading) {
  return `
    <details class="advanced-diagnostics">
      <summary>Advanced SEN66 diagnostics</summary>
      <p>SRAW values are raw sensor ticks, not pollutant concentrations. They are not converted to ppm, ppb, or µg/m³.</p>
      <dl>
        <div><dt>SRAW_VOC</dt><dd>${escapeHtml(formatNumber(reading.sraw_voc, 0, " ticks"))}</dd></div>
        <div><dt>SRAW_NOx</dt><dd>${escapeHtml(formatNumber(reading.sraw_nox, 0, " ticks"))}</dd></div>
        <div><dt>Sensor uptime</dt><dd>${escapeHtml(formatDuration(reading.sensor_uptime_s))}</dd></div>
        <div><dt>Boot ID</dt><dd>${escapeHtml(reading.boot_id ?? "-")}</dd></div>
      </dl>
    </details>
  `;
}

function metricHtml(label, value) {
  return `
    <div class="metric">
      <span class="metric-label">${escapeHtml(label)}</span>
      <span class="metric-value">${escapeHtml(value)}</span>
    </div>
  `;
}

async function refreshStatusTab(force = false) {
  if (state.statusInFlight || (document.hidden && !force)) {
    return;
  }
  state.statusInFlight = true;
  setRefreshButtonBusy(true);
  try {
    const [status, nodes, health] = await Promise.all([
      fetchJson(API.status),
      fetchJson(API.nodes),
      // The health endpoint reports outages, so a failure here must not blank
      // the rest of the tab. Render what came back and say so.
      fetchJson(API.systemStatus).catch(() => null),
    ]);
    state.nodesData = nodes;
    updateWorkflowSources(nodes.nodes || []);
    renderServiceStatus(status);
    renderNodes(nodes);
    renderSystemHealth(health);
    renderDebugInformation(status);
    setLastUpdated(status.checked_at_utc || nodes.generated_at);
    setStatus("Online", "ok");
    clearError();
  } catch (error) {
    setStatus("Status error", "error");
    showError(error.message || "Status refresh failed");
  } finally {
    state.statusInFlight = false;
    setRefreshButtonBusy(false);
  }
}

const HEALTH_STATE_LABELS = {
  healthy: "Healthy",
  degraded: "Degraded",
  unavailable: "Unavailable",
  unknown: "Unknown",
};

// Every state is announced in words as well as colour, and the icon is a
// redundant cue rather than the only one, so the grid stays readable to anyone
// who cannot distinguish the state colours.
const HEALTH_STATE_ICONS = {
  healthy: "\u25CF",
  degraded: "\u25D0",
  unavailable: "\u25CB",
  unknown: "?",
};

const HEALTH_BASIS_NOTES = {
  process_and_data: "Unit state and data freshness were both checked.",
  process_only: "Only the unit's state was checked; nothing verified end to end.",
  data_only: "Observed only through the data it produces.",
  none: "Nothing was observed for this dependency.",
};

function healthStateLabel(state) {
  return HEALTH_STATE_LABELS[state] || "Unknown";
}

function renderSystemHealth(data) {
  const overall = document.getElementById("health-overall");
  const container = document.getElementById("health-grid");
  const build = document.getElementById("health-build");
  if (!overall || !container || !build) {
    return;
  }
  if (!data) {
    overall.textContent = "Unavailable";
    overall.className = "workflow-poll-state health-unavailable";
    container.innerHTML = '<p class="empty-state">Dependency health could not be read.</p>';
    build.textContent = "";
    return;
  }

  const state = data.overall_state || "unknown";
  overall.textContent = healthStateLabel(state);
  overall.className = `workflow-poll-state health-${escapeHtml(state)}`;
  const dependencies = Array.isArray(data.dependencies) ? data.dependencies : [];
  container.innerHTML = dependencies.length
    ? dependencies.map((item) => {
      const itemState = item.state || "unknown";
      const checks = Array.isArray(item.checks) ? item.checks : [];
      return `
        <article class="health-card health-${escapeHtml(itemState)}">
          <div class="service-heading">
            <h3>${escapeHtml(item.display_name || item.dependency_id)}</h3>
            <span class="health-state">
              <span aria-hidden="true">${escapeHtml(HEALTH_STATE_ICONS[itemState] || "?")}</span>
              ${escapeHtml(healthStateLabel(itemState))}
            </span>
          </div>
          <p>${escapeHtml(item.summary || "")}</p>
          ${item.core === false ? '<p class="subtle">Not required for sensor ingest.</p>' : ""}
          <p class="subtle">${escapeHtml(HEALTH_BASIS_NOTES[item.basis] || "")}</p>
          ${checks.length
            ? `<dl class="service-facts">${checks.map((check) => `
                <div><dt>${escapeHtml(formatLabel(check.name))}</dt>
                <dd>${escapeHtml(healthStateLabel(check.state))}</dd></div>`).join("")}</dl>`
            : ""}
        </article>`;
    }).join("")
    : '<p class="empty-state">No dependencies were reported.</p>';

  const service = data.service || {};
  const probe = data.probe || {};
  const parts = [];
  if (service.source_revision) {
    parts.push(`Deployed revision ${service.source_revision} (${service.source_revision_origin || "unknown"})`);
  }
  if (typeof service.process_uptime_seconds === "number") {
    parts.push(`process up ${formatDurationLong(service.process_uptime_seconds)}`);
  }
  if (Array.isArray(probe.timed_out) && probe.timed_out.length) {
    parts.push(`checks that did not finish in time: ${probe.timed_out.join(", ")}`);
  }
  build.textContent = parts.join(" \u00B7 ");
}

function renderServiceStatus(data) {
  const services = data.services || [];
  document.getElementById("status-checked").textContent =
    `Checked ${relativeTime(data.checked_at_utc)}`;
  const container = document.getElementById("services-grid");
  container.innerHTML = services.length
    ? services.map((service) => {
      const stateName = !service.installed
        ? "unavailable"
        : service.active
          ? "active"
          : service.active_state === "failed"
            ? "failed"
            : "inactive";
      const statusText = !service.installed
        ? "Not installed"
        : `${formatLabel(service.active_state)} · ${formatLabel(service.sub_state)}`;
      return `
        <article class="service-card service-${escapeHtml(stateName)}">
          <div class="service-heading">
            <h3>${escapeHtml(service.display_name)}</h3>
            <span class="service-state">${escapeHtml(statusText)}</span>
          </div>
          <p class="subtle">${escapeHtml(service.unit)}</p>
          ${service.description ? `<p>${escapeHtml(service.description)}</p>` : ""}
          <dl class="service-facts">
            <div><dt>Load state</dt><dd>${escapeHtml(service.load_state)}</dd></div>
            <div><dt>State entered</dt><dd>${escapeHtml(service.state_entered_at || "Unavailable")}</dd></div>
            <div><dt>Uptime</dt><dd>${service.uptime_seconds === null ? "Unavailable" : escapeHtml(formatDurationLong(service.uptime_seconds))}</dd></div>
          </dl>
        </article>`;
    }).join("")
    : '<p class="empty-state">No service status was returned.</p>';
}

function renderDebugInformation(data) {
  const config = data.configuration || {};
  const facts = [
    ["Pi hostname", data.hostname || "Unavailable"],
    ["Server UTC time", data.checked_at_utc || "Unavailable"],
    ["Live dashboard refresh", `${POLL_INTERVAL_MS / 1000} seconds (Monitoring tab only)`],
    ["Session / export polling", `${WORKFLOW_POLL_INTERVAL_MS / 1000} seconds (Active Monitoring tab only)`],
    ["Session preview polling", `${PREVIEW_POLL_INTERVAL_MS / 1000} seconds (Active Monitoring tab only)`],
    ["Frontend request timeout", `${FETCH_TIMEOUT_MS / 1000} seconds; server-side work continues`],
    ["Environment stale threshold", formatDurationLong(config.node_stale_after_seconds)],
    ["SEN66 stale threshold", formatDurationLong(config.air_quality_stale_after_seconds)],
    ["SEN66 expected publish", formatDurationLong(config.sen66_expected_publish_seconds)],
    ["Raw SEN66 retention / max session", formatDurationLong(config.raw_retention_seconds)],
    ["Permanent SEN66 aggregate", formatDurationLong(config.stored_air_quality_resolution_seconds)],
  ];
  document.getElementById("debug-settings").innerHTML = facts.map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
  const commands = (data.services || [])
    .filter((service) => service.installed)
    .flatMap((service) => [service.commands?.status, service.commands?.logs])
    .filter(Boolean);
  document.getElementById("debug-commands").innerHTML = commands.length
    ? commands.map((command) => `<code>${escapeHtml(command)}</code>`).join("")
    : "<code>No installed service commands are available.</code>";
}

function renderNodes(data) {
  const tbody = document.getElementById("nodes-table");
  const nodes = data.nodes || [];

  if (nodes.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7">No node status available.</td></tr>';
    return;
  }

  tbody.replaceChildren(...nodes.map((node) => {
    const row = document.createElement("tr");
    const label = node.sensor_type === "environment"
      ? `Node ${node.node_id}`
      : node.label || formatLabel(node.location || node.id);
    const statusClass = nodeStatusClass(node);
    const availableFields = Array.isArray(node.available_fields) ? node.available_fields : [];
    const hasBattery = availableFields.includes("battery_mv");
    row.innerHTML = `
      <td>${escapeHtml(label)}</td>
      <td>${escapeHtml(formatLabel(node.sensor_type))}</td>
      <td class="${escapeHtml(statusClass)}">${escapeHtml(nodeStatusLabel(node))}</td>
      <td>${escapeHtml(relativeTime(node.last_seen))}</td>
      <td>${hasBattery ? escapeHtml(batteryDisplay(node)) : "—"}</td>
      <td>${node.sensor_type === "environment" ? statusFlagsHtml(node) : "-"}</td>
      <td><span class="capability-list">${availableFields.length
        ? availableFields.map((field) => escapeHtml(workflowFieldLabel(field))).join(", ")
        : "None discovered"}</span></td>
    `;
    return row;
  }));
}

function batteryStateFor(reading) {
  if (reading.sensor_type !== "environment") {
    return null;
  }
  if (reading.battery_shutdown === true) {
    return "shutdown";
  }
  if (reading.battery_low === true) {
    return "low";
  }
  if (reading.battery_measurement_ok !== true) {
    return "unavailable";
  }
  return "ok";
}

function batteryDisplay(reading) {
  if (
    reading.battery_measurement_ok === true
    && reading.battery_mv !== undefined
    && reading.battery_mv !== null
  ) {
    return `${reading.battery_mv} mV`;
  }
  return "Unavailable";
}

function statusFlagsMetricHtml(reading) {
  return `
    <div class="metric metric-flags">
      <span class="metric-label">Status flags</span>
      ${statusFlagsHtml(reading)}
    </div>
  `;
}

function statusFlagsHtml(reading) {
  const statusFlags = normalizedStatusFlags(reading.status_flags);
  if (statusFlags === null) {
    return `
      <div class="status-flags">
        <span class="flags-raw">Raw: unavailable</span>
        <span class="flag-chip flag-unavailable">Battery state unavailable</span>
      </div>
    `;
  }

  const chips = ENVIRONMENT_STATUS_FLAGS
    .filter((flag) => (statusFlags & flag.mask) !== 0)
    .map((flag) => (
      `<span class="flag-chip flag-${flag.className}">${escapeHtml(flag.label)} (BIT${bitIndex(flag.mask)})</span>`
    ));

  if ((statusFlags & (1 << 2)) === 0) {
    chips.push('<span class="flag-chip flag-unavailable">Battery measurement unavailable</span>');
  } else if ((statusFlags & ((1 << 3) | (1 << 4))) === 0) {
    chips.push('<span class="flag-chip flag-ok">No battery alert</span>');
  }

  const unknownMask = (statusFlags & (~KNOWN_ENVIRONMENT_STATUS_MASK)) >>> 0;
  if (unknownMask !== 0) {
    chips.push(
      `<span class="flag-chip flag-info">Unknown bits ${escapeHtml(hex32(unknownMask))}</span>`,
    );
  }

  if (chips.length === 0) {
    chips.push('<span class="flag-chip flag-info">No flags set</span>');
  }

  return `
    <div class="status-flags">
      <span class="flags-raw">Raw: ${statusFlags} (${hex32(statusFlags)})</span>
      <span class="flag-chip-list">${chips.join("")}</span>
    </div>
  `;
}

function normalizedStatusFlags(value) {
  const statusFlags = Number(value);
  if (!Number.isInteger(statusFlags) || statusFlags < 0 || statusFlags > 0xffffffff) {
    return null;
  }
  return statusFlags >>> 0;
}

function hex32(value) {
  return `0x${(value >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;
}

function bitIndex(mask) {
  return 31 - Math.clz32(mask);
}

function batteryAlertHtml(reading) {
  const batteryState = batteryStateFor(reading);
  if (batteryState === "shutdown") {
    return '<div class="battery-alert battery-alert-shutdown">Critical: low-battery shutdown confirmed.</div>';
  }
  if (batteryState === "low") {
    return '<div class="battery-alert battery-alert-low">Warning: battery voltage is low.</div>';
  }
  if (batteryState === "unavailable") {
    return '<div class="battery-alert battery-alert-unavailable">Battery measurement unavailable.</div>';
  }
  return "";
}

function nodeStatusLabel(node) {
  const status = node.status || "unknown";
  if (node.battery_shutdown === true) {
    return ["stale", "offline"].includes(status)
      ? `${status} - battery shutdown`
      : "battery shutdown";
  }
  if (node.battery_low === true) {
    return `${status} - low battery`;
  }
  return status;
}

function nodeStatusClass(node) {
  if (node.battery_shutdown === true) {
    return "node-shutdown";
  }
  if (node.battery_low === true) {
    return "node-low";
  }
  return `node-${node.status || "unknown"}`;
}

function sensorStatusPillClass(status) {
  if (status === "online") return "status-ok";
  if (status === "offline") return "status-error";
  return "status-loading";
}

function renderCharts(data) {
  if (!state.charts.history) {
    return;
  }
  const telemetry = state.chartTelemetryData;
  const series = [...(data.series || []), ...((telemetry && telemetry.series) || [])]
    .filter(chartSeriesMatchesFilter);
  const tier = data.data_tier === "live_1m"
    ? "Short range: 1-minute means from bounded high-resolution live samples"
    : "Long range: persistent 15-minute means (aggregate statistics hidden)";
  // Bambu telemetry has its own tier; report it rather than implying the
  // environmental tier covers it.
  const bambuTier = telemetry && telemetry.data_tier
    ? ` · Bambu: ${String(telemetry.data_tier).startsWith("durable_")
        ? "permanent five-minute samples"
        : "high-resolution live telemetry"}`
    : "";
  document.getElementById("history-tier").textContent = `${tier}${bambuTier}`;
  const selectedFields = Array.from(state.selectedChartFields);
  const units = Array.from(new Set(selectedFields
    .map((field) => state.fieldDefinitions.get(field)?.unit)
    .filter(Boolean)));
  const axisForUnit = new Map(units.map((unit, index) => [unit, `y${index}`]));
  const datasets = [];
  for (const item of series) {
    const sourceLabel = chartSourceLabel(item);
    for (const field of selectedFields) {
      const definition = state.fieldDefinitions.get(field);
      // Only build a dataset when this source family actually provides the
      // field, so a printer never appears to supply AMS humidity, or vice versa.
      if (!definition || !(definition.sensor_types || []).includes(item.sensor_type)) {
        continue;
      }
      const points = (item.points || [])
        .filter((point) => point[field] !== undefined && point[field] !== null)
        .map((point) => ({ time: point.time, value: point[field] }));
      if (!points.length) {
        continue;
      }
      const color = chartPalette[datasets.length % chartPalette.length];
      datasets.push({
        label: `${sourceLabel} · ${definition.label}`,
        sourceLabel,
        measurementLabel: definition.label,
        displayUnit: definition.display_unit || "",
        data: points,
        borderColor: color,
        backgroundColor: color,
        yAxisID: axisForUnit.get(definition.unit) || "y0",
        showLine: true,
        spanGaps: true,
      });
    }
  }
  const chart = state.charts.history;
  chart.options.scales = {
    x: chart.options.scales.x,
    ...Object.fromEntries(units.map((unit, index) => {
      const definition = Array.from(state.fieldDefinitions.values())
        .find((field) => field.unit === unit);
      return [`y${index}`, {
        type: "linear",
        beginAtZero: false,
        position: index === 0 ? "left" : "right",
        offset: index > 1,
        title: { display: true, text: definition?.display_unit || unit },
        grid: index === 0 ? { color: "#edf2ef" } : { drawOnChartArea: false },
      }];
    })),
  };
  updateChart(chart, datasets);
  document.getElementById("chart-series-count").textContent =
    `${datasets.length} ${datasets.length === 1 ? "series" : "series"}`;
  document.getElementById("chart-empty").hidden = datasets.length > 0;
  // Scoped to this canvas: the Bambu tab also renders a .chart-frame-large.
  document.getElementById("history-chart").closest(".chart-frame-large").hidden = datasets.length === 0;
}

function updateChart(chart, datasets, rangeKey = state.range) {
  const labels = sortedUniqueTimes(datasets);
  chart.data.labels = labels.map((value) => formatChartTime(value, rangeKey));
  chart.data.datasets = datasets.map((dataset) => {
    const valueByTime = new Map(dataset.data.map((point) => [point.time, point.value]));
    return {
      label: dataset.label,
      data: labels.map((time) => valueByTime.get(time) ?? null),
      borderColor: dataset.borderColor,
      backgroundColor: dataset.backgroundColor,
      yAxisID: dataset.yAxisID,
      hidden: dataset.hidden,
      borderDash: dataset.borderDash,
      showLine: dataset.showLine,
      spanGaps: dataset.spanGaps,
      pointRadius: 0,
      rawTimes: labels,
      sourceLabel: dataset.sourceLabel,
      measurementLabel: dataset.measurementLabel,
      displayUnit: dataset.displayUnit,
      valueKind: dataset.valueKind,
    };
  });
  chart.update();
}

function sortedUniqueTimes(datasets) {
  const values = new Set();
  for (const dataset of datasets) {
    for (const point of dataset.data) {
      values.add(point.time);
    }
  }
  return Array.from(values).sort();
}

function setStatus(text, stateName) {
  const element = document.getElementById("connection-state");
  element.textContent = text;
  element.className = `status-pill status-${stateName}`;
}

function setRefreshButtonBusy(isBusy) {
  const button = document.getElementById("refresh-button");
  button.disabled = isBusy;
  button.setAttribute("aria-busy", String(isBusy));
  button.title = isBusy ? "Refresh in progress" : "Refresh now";
}

function setLastUpdated(value) {
  const element = document.getElementById("last-updated");
  element.textContent = value ? `Updated ${relativeTime(value)}` : "No update time available";
}

function showError(message) {
  const banner = document.getElementById("error-banner");
  banner.textContent = message;
  banner.hidden = false;
}

function clearError() {
  const banner = document.getElementById("error-banner");
  banner.textContent = "";
  banner.hidden = true;
}

function formatChartTime(value, rangeKey = state.range) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    month: rangeKey === "30d" ? "short" : undefined,
    day: rangeKey === "7d" || rangeKey === "30d" ? "numeric" : undefined,
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function relativeTime(value) {
  if (!value) {
    return "never";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 48) {
    return `${hours}h ago`;
  }
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function formatNumber(value, digits, suffix) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${Number(value).toFixed(digits)}${suffix}`;
}

function signedNumber(value, digits) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "-";
  }
  return `${number >= 0 ? "+" : ""}${number.toFixed(digits)}`;
}

function formatDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) {
    return "unavailable";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)}m`;
  }
  return `${(seconds / 3600).toFixed(1)}h`;
}

function formatDurationLong(value) {
  const total = Number(value);
  if (!Number.isFinite(total) || total < 0) {
    return "Unavailable";
  }
  const seconds = Math.round(total);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  if (remainder || !parts.length) parts.push(`${remainder}s`);
  return parts.join(" ");
}

function formatLabel(value) {
  if (!value) {
    return "-";
  }
  return String(value)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
