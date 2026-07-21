"use strict";

const API = {
  latest: "/api/latest",
  readings: "/api/readings",
  nodes: "/api/nodes",
};

const POLL_INTERVAL_MS = 7000;
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
};

document.addEventListener("DOMContentLoaded", () => {
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

async function fetchJson(url) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

  try {
    const response = await fetch(url, {
      headers: { "Accept": "application/json" },
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
    : "Long range: persistent 15-minute aggregates (legacy raw history remains included)";
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
