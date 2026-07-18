"use strict";

const API = {
  latest: "/api/latest",
  readings: "/api/readings",
  nodes: "/api/nodes",
};

const POLL_INTERVAL_MS = 7000;
const FETCH_TIMEOUT_MS = 8000;
const KNOWN_ENVIRONMENT_STATUS_MASK = 0x1f;

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
  if (state.nodeFilter !== "all") {
    params.set("sensor_type", "environment");
    params.set("node_id", state.nodeFilter);
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
  const nodes = (data.environment || [])
    .map((reading) => reading.node_id)
    .filter((nodeId) => nodeId !== undefined && nodeId !== null)
    .map(String)
    .sort((left, right) => Number(left) - Number(right));
  const uniqueNodes = Array.from(new Set(nodes));

  const options = [
    { value: "all", label: "All environment nodes" },
    ...uniqueNodes.map((nodeId) => ({ value: nodeId, label: `Node ${nodeId}` })),
  ];

  if (state.nodeFilter !== "all" && !uniqueNodes.includes(state.nodeFilter)) {
    options.push({ value: state.nodeFilter, label: `Node ${state.nodeFilter}` });
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
  state.charts.temperature = createLineChart("temperature-chart", "Temperature C");
  state.charts.humidity = createLineChart("humidity-chart", "Humidity %");
  state.charts.battery = createLineChart("battery-chart", "Battery mV");
  state.charts.air = createLineChart("air-chart", "Air quality");
}

function createLineChart(canvasId, title) {
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
        y: {
          beginAtZero: false,
          grid: {
            color: "#edf2ef",
          },
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

  card.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <div class="reading-values">
      ${metricHtml("Temp", formatNumber(reading.temperature_c, 1, " C"))}
      ${metricHtml("Humidity", formatNumber(reading.humidity, 1, "%"))}
      ${isEnvironment ? metricHtml("Battery", batteryDisplay(reading)) : ""}
      ${reading.co2 !== undefined ? metricHtml("CO2", `${reading.co2} ppm`) : ""}
      ${reading.pm25 !== undefined ? metricHtml("PM2.5", formatNumber(reading.pm25, 1, " ug/m3")) : ""}
      ${isEnvironment ? statusFlagsMetricHtml(reading) : ""}
    </div>
    ${batteryAlertHtml(reading)}
    <div class="metric-small">${escapeHtml(relativeTime(reading.last_seen))}</div>
  `;
  return card;
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

  updateChart(state.charts.temperature, buildDatasets(environmentSeries, "temperature_c", "Temp C"));
  updateChart(state.charts.humidity, buildDatasets(environmentSeries, "humidity", "Humidity %"));
  updateChart(state.charts.battery, buildDatasets(
    environmentSeries,
    "battery_mv",
    "Battery mV",
  ));

  const airDatasets = [
    ...buildDatasets(airQualitySeries, "co2", "CO2 ppm"),
    ...buildDatasets(airQualitySeries, "pm25", "PM2.5"),
  ];
  const airPanel = document.getElementById("air-chart-panel");
  airPanel.hidden = airDatasets.length === 0;
  updateChart(state.charts.air, airDatasets);
}

function buildDatasets(series, field, suffix) {
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
        borderColor: chartPalette[index % chartPalette.length],
        backgroundColor: chartPalette[index % chartPalette.length],
      };
    })
    .filter(Boolean);
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
      spanGaps: true,
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
