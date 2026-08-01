"use strict";

const API = {
  latest: "/api/latest",
  readings: "/api/readings",
  nodes: "/api/nodes",
  workflowOptions: "/api/workflows/options",
  monitoringSessions: "/api/monitoring/sessions",
  exports: "/api/exports",
};

const POLL_INTERVAL_MS = 7000;
const WORKFLOW_POLL_INTERVAL_MS = 5000;
const PREVIEW_POLL_INTERVAL_MS = 15000;
const FETCH_TIMEOUT_MS = 8000;
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

const EXPORT_FIELD_GROUPS = [
  { label: "Climate", fields: ["temperature_c", "humidity"] },
  { label: "Particulate", fields: ["pm1", "pm25", "pm4", "pm10"] },
  { label: "Gas and indices", fields: ["co2", "voc_index", "nox_index"] },
  { label: "Battery", fields: ["battery_mv"] },
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
  charts: {},
  latestTimer: null,
  fullRefreshInFlight: false,
  latestRefreshInFlight: false,
  knownSources: new Map(),
  monitoringSessions: [],
  exportJobs: [],
  monitoringTimer: null,
  exportTimer: null,
  previewTimer: null,
  clockTimer: null,
  monitoringInFlight: false,
  exportsInFlight: false,
  previewInFlight: false,
  serverOffsetMs: 0,
};

document.addEventListener("DOMContentLoaded", () => {
  setupWorkflowControllers();

  if (!window.Chart) {
    showError("Chart.js is not available. Run scripts/install_frontend_assets.sh on the Raspberry Pi.");
    setStatus("Chart.js missing", "error");
    return;
  }

  Chart.defaults.font.family = 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  Chart.defaults.color = "#42534a";

  setupRangeButtons();
  setupNodeFilter();
  document.getElementById("refresh-button").addEventListener("click", refreshAll);
  initializeCharts();
  refreshAll();
  state.latestTimer = window.setInterval(refreshLatestOnly, POLL_INTERVAL_MS);
});

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
    const [latest, readings] = await Promise.all([
      fetchJson(API.latest),
      fetchJson(readingsUrl),
    ]);
    const nodes = await nodesForLatest(latest);

    updateWorkflowSources(nodes.nodes || []);
    updateNodeFilterOptions(latest);
    renderLatest(latest);
    renderNodes(nodes);
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
  if (state.fullRefreshInFlight || state.latestRefreshInFlight) {
    return;
  }

  state.latestRefreshInFlight = true;
  try {
    const latest = await fetchJson(API.latest);
    const nodes = await nodesForLatest(latest);

    updateWorkflowSources(nodes.nodes || []);
    updateNodeFilterOptions(latest);
    renderLatest(latest);
    renderNodes(nodes);
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
  const timeout = window.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

  try {
    const response = await fetch(url, {
      ...options,
      headers: {
        "Accept": "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
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
  renderWorkflowFieldSelectors();
  setDefaultExportInterval();

  const duration = document.getElementById("monitoring-duration");
  duration.addEventListener("change", () => {
    document.getElementById("monitoring-custom-wrap").hidden = duration.value !== "custom";
  });
  document.getElementById("monitoring-form").addEventListener("submit", submitMonitoringSession);
  document.getElementById("export-form").addEventListener("submit", submitHistoricalExport);
  document.getElementById("active-monitoring").addEventListener("click", handleMonitoringAction);
  document.getElementById("historical-exports").addEventListener("click", handleExportAction);

  void refreshWorkflowOptions();
  void refreshMonitoringSessions(true);
  void refreshExportJobs(true);
  state.monitoringTimer = window.setInterval(refreshMonitoringSessions, WORKFLOW_POLL_INTERVAL_MS);
  state.exportTimer = window.setInterval(refreshExportJobs, WORKFLOW_POLL_INTERVAL_MS);
  state.previewTimer = window.setInterval(refreshMonitoringPreviews, PREVIEW_POLL_INTERVAL_MS);
  state.clockTimer = window.setInterval(updateVisibleWorkflowClocks, 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      void refreshMonitoringSessions(true);
      void refreshExportJobs(true);
      void refreshMonitoringPreviews(true);
    }
  });
}

async function refreshWorkflowOptions() {
  try {
    const data = await fetchJson(API.workflowOptions);
    updateServerOffset(data.server_time_utc);
    const maximumMinutes = Math.floor(Number(data.raw_retention_seconds) / 60);
    const custom = document.getElementById("monitoring-custom-minutes");
    if (Number.isFinite(maximumMinutes) && maximumMinutes > 0) {
      custom.max = String(maximumMinutes);
    }
  } catch (_error) {
    // Forms retain safe built-in defaults; status pollers report API availability.
  }
}

function renderWorkflowFieldSelectors() {
  for (const prefix of ["monitoring", "export"]) {
    const container = document.getElementById(`${prefix}-fields`);
    container.replaceChildren(...EXPORT_FIELD_GROUPS.map((group) => {
      const section = document.createElement("section");
      section.className = "field-group";
      const heading = document.createElement("h4");
      heading.textContent = group.label;
      const grid = document.createElement("div");
      grid.className = "check-grid";
      for (const field of group.fields) {
        const label = document.createElement("label");
        label.className = "check-option";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = `${prefix}_field`;
        input.value = field;
        input.checked = ["temperature_c", "humidity"].includes(field);
        const text = document.createElement("span");
        text.textContent = workflowFieldLabel(field);
        label.append(input, text);
        grid.append(label);
      }
      section.append(heading, grid);
      return section;
    }));
  }
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
    }
    if (source && !state.knownSources.has(source.key)) {
      state.knownSources.set(source.key, source);
      changed = true;
    }
  }
  if (changed || document.querySelectorAll(".source-options input").length === 0) {
    renderSourceSelectors();
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
      text.textContent = source.label;
      label.append(input, text);
      return label;
    }));
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
    const durationSeconds = durationSelect === "custom"
      ? Math.round(Number(document.getElementById("monitoring-custom-minutes").value) * 60)
      : Number(durationSelect);
    if (!name) {
      throw new Error("Enter a session name.");
    }
    if (!sourceKeys.length) {
      throw new Error("Select at least one sensor source.");
    }
    if (!fields.length) {
      throw new Error("Select at least one measurement.");
    }
    if (!Number.isInteger(durationSeconds) || durationSeconds < 60) {
      throw new Error("Monitoring duration must be at least one minute.");
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
  if (state.monitoringInFlight || (document.hidden && !force)) {
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
  if (state.previewInFlight || (document.hidden && !force)) {
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
  if (state.exportsInFlight || (document.hidden && !force)) {
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
  return source.sensor_type === "environment"
    ? { sensor_type: "environment", node_id: source.node_id }
    : { sensor_type: "air_quality", location: source.location };
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
  const labels = {
    temperature_c: "Temperature",
    humidity: "Humidity",
    battery_mv: "Battery voltage",
    co2: "CO₂",
    pm1: "PM1.0",
    pm25: "PM2.5",
    pm4: "PM4.0",
    pm10: "PM10",
    voc_index: "VOC Index",
    nox_index: "NOx Index",
  };
  return labels[field] || formatLabel(field);
}

function sourceSummary(sources) {
  return (sources || []).map((source) => (
    source.sensor_type === "environment"
      ? `Node ${source.node_id}`
      : formatLabel(source.location)
  )).join(", ");
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
  const sensorOptions = [...environmentNodes, ...airStations];

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
  state.charts.temperature = createLineChart("temperature-chart", "Temperature °C");
  state.charts.humidity = createLineChart("humidity-chart", "Humidity %");
  state.charts.battery = createLineChart("battery-chart", "Battery mV");
  state.charts.gas = createLineChart("gas-chart", "SEN66 gas and indices", {
    y: {
      beginAtZero: false,
      position: "left",
      title: { display: true, text: "CO₂ (ppm)" },
      grid: { color: "#edf2ef" },
    },
    yIndex: {
      beginAtZero: false,
      position: "right",
      title: { display: true, text: "VOC / NOx index" },
      grid: { drawOnChartArea: false },
    },
  });
  state.charts.particulate = createLineChart(
    "particulate-chart",
    "SEN66 particulate matter",
  );
}

function createLineChart(canvasId, title, yScales = null) {
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
          text: title,
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
        ...(yScales || {
          y: {
            beginAtZero: false,
            grid: {
              color: "#edf2ef",
            },
          },
        }),
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

function renderLatest(data) {
  const grid = document.getElementById("current-grid");
  const readings = [
    ...(data.environment || []),
    ...(data.air_quality || []),
  ];

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

  if (!isEnvironment) {
    card.classList.add("reading-card-air");
    const overall = reading.overall_status || {};
    card.innerHTML = `
      <div class="air-station-heading">
        <div>
          <h3>${escapeHtml(title)}</h3>
          <span class="authority-label">SEN66 · live 5-second feed</span>
        </div>
        <span class="interpretation-status severity-${escapeHtml(overall.severity || "unavailable")}">
          Room summary: ${escapeHtml(overall.category || "Unavailable")}
          ${overall.driving_metric ? ` · driven by ${escapeHtml(formatLabel(overall.driving_metric))}` : ""}
        </span>
      </div>
      <div class="air-reading-groups">
        ${AIR_QUALITY_METRIC_GROUPS.map((group) => airMetricGroupHtml(reading, group)).join("")}
      </div>
      ${advancedDiagnosticsHtml(reading)}
      <div class="metric-small">Station updated ${escapeHtml(relativeTime(reading.last_seen))}</div>
    `;
    return card;
  }

  card.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <div class="reading-values">
      ${metricHtml("Temp", formatNumber(reading.temperature_c, 1, " °C"))}
      ${metricHtml("Humidity", formatNumber(reading.humidity, 1, "%"))}
      ${metricHtml("Battery", batteryDisplay(reading))}
      ${statusFlagsMetricHtml(reading)}
    </div>
    ${batteryAlertHtml(reading)}
    <div class="metric-small">${escapeHtml(relativeTime(reading.last_seen))}</div>
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

function renderNodes(data) {
  const tbody = document.getElementById("nodes-table");
  const nodes = data.nodes || [];

  if (nodes.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6">No node status available.</td></tr>';
    return;
  }

  tbody.replaceChildren(...nodes.map((node) => {
    const row = document.createElement("tr");
    const label = node.sensor_type === "environment"
      ? `Node ${node.node_id}`
      : formatLabel(node.location || node.id);
    const statusClass = nodeStatusClass(node);
    row.innerHTML = `
      <td>${escapeHtml(label)}</td>
      <td>${escapeHtml(formatLabel(node.sensor_type))}</td>
      <td class="${escapeHtml(statusClass)}">${escapeHtml(nodeStatusLabel(node))}</td>
      <td>${escapeHtml(relativeTime(node.last_seen))}</td>
      <td>${node.sensor_type === "environment" ? escapeHtml(batteryDisplay(node)) : "-"}</td>
      <td>${node.sensor_type === "environment" ? statusFlagsHtml(node) : "-"}</td>
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
    return status === "stale"
      ? "stale - battery shutdown"
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

function renderCharts(data) {
  const series = data.series || [];
  const environmentSeries = series.filter((item) => item.sensor_type === "environment");
  const airQualitySeries = series.filter((item) => item.sensor_type === "air_quality");
  const climateSeries = [...environmentSeries, ...airQualitySeries];
  const tier = data.data_tier === "live_1m"
    ? "Short range: 1-minute means from bounded high-resolution live samples"
    : "Long range: persistent 15-minute aggregates";
  document.getElementById("history-tier").textContent = tier;

  updateChart(state.charts.temperature, buildDatasets(climateSeries, "temperature_c", "Temp °C"));
  updateChart(state.charts.humidity, buildDatasets(climateSeries, "humidity", "Humidity %"));
  updateChart(state.charts.battery, buildDatasets(
    environmentSeries,
    "battery_mv",
    "Battery mV",
  ));

  const gasDatasets = [
    ...buildDatasets(airQualitySeries, "co2", "CO₂ mean (ppm)", { colorOffset: 0 }),
    ...buildDatasets(airQualitySeries, "co2_max", "CO₂ maximum (ppm)", {
      colorOffset: 0, hidden: true, borderDash: [5, 4],
    }),
    ...buildDatasets(airQualitySeries, "co2_p95", "CO₂ p95 (ppm)", {
      colorOffset: 0, hidden: true, borderDash: [2, 3],
    }),
    ...buildDatasets(airQualitySeries, "voc_index", "VOC Index mean", {
      colorOffset: 2,
      yAxisID: "yIndex",
    }),
    ...buildDatasets(airQualitySeries, "voc_index_max", "VOC Index maximum", {
      colorOffset: 2, yAxisID: "yIndex", hidden: true, borderDash: [5, 4],
    }),
    ...buildDatasets(airQualitySeries, "voc_index_p95", "VOC Index p95", {
      colorOffset: 2, yAxisID: "yIndex", hidden: true, borderDash: [2, 3],
    }),
    ...buildDatasets(airQualitySeries, "nox_index", "NOx Index mean", {
      colorOffset: 4,
      yAxisID: "yIndex",
    }),
    ...buildDatasets(airQualitySeries, "nox_index_max", "NOx Index maximum", {
      colorOffset: 4, yAxisID: "yIndex", hidden: true, borderDash: [5, 4],
    }),
    ...buildDatasets(airQualitySeries, "nox_index_p95", "NOx Index p95", {
      colorOffset: 4, yAxisID: "yIndex", hidden: true, borderDash: [2, 3],
    }),
    ...eventDatasets(data.events || [], ["co2", "voc_index", "nox_index"]),
  ];
  const gasPanel = document.getElementById("gas-chart-panel");
  gasPanel.hidden = gasDatasets.length === 0;
  updateChart(state.charts.gas, gasDatasets);

  const particulateDatasets = [
    ...buildDatasets(airQualitySeries, "pm1", "PM1.0 mean", { colorOffset: 0 }),
    ...buildDatasets(airQualitySeries, "pm25", "PM2.5 mean", { colorOffset: 2 }),
    ...buildDatasets(airQualitySeries, "pm25_max", "PM2.5 maximum", {
      colorOffset: 2, hidden: true, borderDash: [5, 4],
    }),
    ...buildDatasets(airQualitySeries, "pm25_p95", "PM2.5 p95", {
      colorOffset: 2, hidden: true, borderDash: [2, 3],
    }),
    ...buildDatasets(airQualitySeries, "pm4", "PM4.0 mean", { colorOffset: 4 }),
    ...buildDatasets(airQualitySeries, "pm10", "PM10 mean", { colorOffset: 6 }),
    ...buildDatasets(airQualitySeries, "pm10_max", "PM10 maximum", {
      colorOffset: 6, hidden: true, borderDash: [5, 4],
    }),
    ...buildDatasets(airQualitySeries, "pm10_p95", "PM10 p95", {
      colorOffset: 6, hidden: true, borderDash: [2, 3],
    }),
    ...eventDatasets(data.events || [], ["pm25", "pm10"]),
  ];
  const particulatePanel = document.getElementById("particulate-chart-panel");
  particulatePanel.hidden = particulateDatasets.length === 0;
  updateChart(state.charts.particulate, particulateDatasets);
}

function buildDatasets(series, field, suffix, options = {}) {
  const colorOffset = options.colorOffset || 0;
  const yAxisID = options.yAxisID || "y";
  return series
    .map((item, index) => {
      const points = (item.points || [])
        .filter((point) => point[field] !== undefined && point[field] !== null)
        .map((point) => ({
          time: point.time,
          value: point[field],
        }));

      if (points.length === 0) {
        return null;
      }

      const labelBase = item.sensor_type === "environment"
        ? `Node ${item.node_id}`
        : formatLabel(item.location || item.id);
      return {
        label: `${labelBase} ${suffix}`,
        data: points,
        borderColor: chartPalette[(colorOffset + index) % chartPalette.length],
        backgroundColor: chartPalette[(colorOffset + index) % chartPalette.length],
        yAxisID,
        hidden: Boolean(options.hidden),
        borderDash: options.borderDash || [],
        showLine: true,
        spanGaps: true,
      };
    })
    .filter(Boolean);
}

function eventDatasets(events, metrics) {
  const byMetric = new Map();
  for (const event of events) {
    if (!metrics.includes(event.metric) || event.trigger_value === undefined) {
      continue;
    }
    const list = byMetric.get(event.metric) || [];
    list.push({ time: event.time, value: event.trigger_value });
    byMetric.set(event.metric, list);
  }
  return Array.from(byMetric.entries()).map(([metric, points], index) => ({
    label: `${formatLabel(metric)} event`,
    data: points,
    borderColor: chartPalette[(index + 5) % chartPalette.length],
    backgroundColor: chartPalette[(index + 5) % chartPalette.length],
    yAxisID: ["voc_index", "nox_index"].includes(metric) ? "yIndex" : "y",
    hidden: false,
    borderDash: [],
    showLine: false,
    spanGaps: false,
    pointRadius: 5,
  }));
}

function updateChart(chart, datasets) {
  const labels = sortedUniqueTimes(datasets);
  chart.data.labels = labels.map(formatChartTime);
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
      pointRadius: dataset.pointRadius ?? 0,
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

function formatChartTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    month: state.range === "30d" ? "short" : undefined,
    day: state.range === "7d" || state.range === "30d" ? "numeric" : undefined,
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
