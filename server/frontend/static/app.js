"use strict";

const API = {
  latest: "/api/latest",
  readings: "/api/readings",
  nodes: "/api/nodes",
};

const POLL_INTERVAL_MS = 7000;
const FETCH_TIMEOUT_MS = 8000;

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
  charts: {},
  latestTimer: null,
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
  document.getElementById("refresh-button").addEventListener("click", refreshAll);
  initializeCharts();
  refreshAll();
  state.latestTimer = window.setInterval(refreshLatestOnly, POLL_INTERVAL_MS);
});

async function refreshAll() {
  clearError();
  setStatus("Loading", "loading");
  try {
    const [latest, nodes, readings] = await Promise.all([
      fetchJson(API.latest),
      fetchJson(API.nodes),
      fetchJson(`${API.readings}?range=${encodeURIComponent(state.range)}`),
    ]);

    renderLatest(latest);
    renderNodes(nodes);
    renderCharts(readings);
    setLastUpdated(latest.generated_at || readings.generated_at || nodes.generated_at);
    setStatus("Online", "ok");
  } catch (error) {
    setStatus("API error", "error");
    showError(error.message || "Dashboard refresh failed");
  }
}

async function refreshLatestOnly() {
  try {
    const [latest, nodes] = await Promise.all([
      fetchJson(API.latest),
      fetchJson(API.nodes),
    ]);

    renderLatest(latest);
    renderNodes(nodes);
    setLastUpdated(latest.generated_at || nodes.generated_at);
    setStatus("Online", "ok");
    clearError();
  } catch (error) {
    setStatus("API error", "error");
    showError(error.message || "Latest refresh failed");
  }
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
      throw new Error(message);
    }

    return await response.json();
  } finally {
    window.clearTimeout(timeout);
  }
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
  const title = reading.sensor_type === "environment"
    ? `Node ${reading.node_id}`
    : formatLabel(reading.location || reading.id);

  card.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <div class="reading-values">
      ${metricHtml("Temp", formatNumber(reading.temperature_c, 1, " C"))}
      ${metricHtml("Humidity", formatNumber(reading.humidity, 1, "%"))}
      ${reading.battery_mv !== undefined ? metricHtml("Battery", `${reading.battery_mv} mV`) : ""}
      ${reading.co2 !== undefined ? metricHtml("CO2", `${reading.co2} ppm`) : ""}
      ${reading.pm25 !== undefined ? metricHtml("PM2.5", formatNumber(reading.pm25, 1, " ug/m3")) : ""}
      ${reading.status_flags !== undefined ? metricHtml("Flags", String(reading.status_flags)) : ""}
    </div>
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
    row.innerHTML = `
      <td>${escapeHtml(label)}</td>
      <td>${escapeHtml(formatLabel(node.sensor_type))}</td>
      <td class="node-${escapeHtml(node.status || "unknown")}">${escapeHtml(node.status || "unknown")}</td>
      <td>${escapeHtml(relativeTime(node.last_seen))}</td>
      <td>${node.battery_mv !== undefined ? `${node.battery_mv} mV` : "-"}</td>
      <td>${node.status_flags !== undefined ? escapeHtml(String(node.status_flags)) : "-"}</td>
    `;
    return row;
  }));
}

function renderCharts(data) {
  const series = data.series || [];
  updateChart(state.charts.temperature, buildDatasets(series, "temperature_c", "Temp C"));
  updateChart(state.charts.humidity, buildDatasets(series, "humidity", "Humidity %"));
  updateChart(state.charts.battery, buildDatasets(
    series.filter((item) => item.sensor_type === "environment"),
    "battery_mv",
    "Battery mV",
  ));
  updateChart(state.charts.air, [
    ...buildDatasets(series, "co2", "CO2 ppm"),
    ...buildDatasets(series, "pm25", "PM2.5"),
  ]);
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
