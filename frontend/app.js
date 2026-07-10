const state = {
  devices: [],
  selectedDeviceId: "", // "" bedeutet: alle Geraete summiert
  hours: 24,
  chart: null,
};

const el = (id) => document.getElementById(id);

function fmtWatt(value) {
  if (value === null || value === undefined) return "–";
  if (Math.abs(value) >= 1000) return (value / 1000).toFixed(2) + " kW";
  return Math.round(value) + " W";
}

function fmtKwh(value) {
  if (value === null || value === undefined) return "–";
  return value.toFixed(1) + " kWh";
}

function fmtPercent(value) {
  if (value === null || value === undefined) return "–";
  return Math.round(value) + " %";
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Request an ${url} fehlgeschlagen: ${res.status}`);
  return res.json();
}

async function loadDevices() {
  state.devices = await fetchJson("/api/devices");
  const select = el("device-select");
  select.innerHTML = "";
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = state.devices.length > 1 ? "Alle (Summe)" : "Wechselrichter";
  select.appendChild(allOption);
  for (const device of state.devices) {
    const opt = document.createElement("option");
    opt.value = device.id;
    opt.textContent = device.name;
    select.appendChild(opt);
  }
  select.value = state.selectedDeviceId;
  select.addEventListener("change", () => {
    state.selectedDeviceId = select.value;
    refreshAll();
  });
}

function sumField(readings, field) {
  const values = readings.map((r) => r[field]).filter((v) => v !== null && v !== undefined);
  if (values.length === 0) return null;
  return values.reduce((a, b) => a + b, 0);
}

async function refreshLiveCards() {
  const latest = await fetchJson("/api/readings/latest");
  const relevant = state.selectedDeviceId
    ? latest.filter((r) => r.device_id === state.selectedDeviceId)
    : latest;

  el("card-home").textContent = fmtWatt(sumField(relevant, "home_power_w"));
  el("card-feedin").textContent = fmtWatt(sumField(relevant, "feed_in_power_w"));
  el("card-griddraw").textContent = fmtWatt(sumField(relevant, "grid_draw_power_w"));
  el("card-pv").textContent = fmtWatt(sumField(relevant, "pv_power_w"));

  const batteryEntries = relevant.filter(
    (r) => r.battery_soc_percent !== null && r.battery_soc_percent !== undefined
  );
  if (batteryEntries.length === 0) {
    el("card-battery-wrapper").style.display = "none";
  } else {
    el("card-battery-wrapper").style.display = "";
    const batteryPower = fmtWatt(sumField(relevant, "battery_power_w"));
    const socText = batteryEntries
      .map((r) => `${r.device_name}: ${fmtPercent(r.battery_soc_percent)}`)
      .join(" · ");
    el("card-battery").textContent = `${batteryPower} (${socText})`;
  }

  if (relevant.length > 0) {
    const newest = relevant.reduce((a, b) => (a.timestamp > b.timestamp ? a : b));
    const t = new Date(newest.timestamp);
    el("last-update").textContent = "Letzte Aktualisierung: " + t.toLocaleTimeString("de-DE");
  }
}

async function refreshSummaryCards() {
  const summaries = await fetchJson("/api/readings/today-summary");
  const relevant = state.selectedDeviceId
    ? summaries.filter((s) => s.device_id === state.selectedDeviceId)
    : summaries;

  el("summary-yield").textContent = fmtKwh(sumField(relevant, "yield_day_kwh"));
  el("summary-consumption").textContent = fmtKwh(sumField(relevant, "home_consumption_day_kwh"));
  el("summary-grid").textContent = fmtKwh(sumField(relevant, "energy_grid_day_kwh"));
}

function bucketMinutesForRange(hours) {
  if (hours <= 24) return 5;
  if (hours <= 168) return 30;
  return 180;
}

async function refreshChart() {
  const bucketMinutes = bucketMinutesForRange(state.hours);
  const params = new URLSearchParams({
    hours: String(state.hours),
    bucket_minutes: String(bucketMinutes),
  });
  if (state.selectedDeviceId) params.set("device_id", state.selectedDeviceId);

  const points = await fetchJson(`/api/readings/history?${params.toString()}`);

  const useDate = state.hours > 24;
  const labels = points.map((p) => {
    const d = new Date(p.timestamp);
    return useDate
      ? d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
      : d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  });
  const datasets = [
    {
      label: "Hausverbrauch",
      data: points.map((p) => p.home_power_w),
      borderColor: "#f87171",
      backgroundColor: "#f8717133",
      tension: 0.25,
      pointRadius: 0,
    },
    {
      label: "Einspeisung",
      data: points.map((p) => p.feed_in_power_w),
      borderColor: "#4ade80",
      backgroundColor: "#4ade8033",
      tension: 0.25,
      pointRadius: 0,
    },
    {
      label: "Netzbezug",
      data: points.map((p) => p.grid_draw_power_w),
      borderColor: "#facc15",
      backgroundColor: "#facc1533",
      tension: 0.25,
      pointRadius: 0,
    },
    {
      label: "PV-Leistung",
      data: points.map((p) => p.pv_power_w),
      borderColor: "#60a5fa",
      backgroundColor: "#60a5fa33",
      tension: 0.25,
      pointRadius: 0,
    },
  ];

  if (state.chart) {
    state.chart.data.labels = labels;
    state.chart.data.datasets = datasets;
    state.chart.update();
    return;
  }

  const ctx = el("power-chart").getContext("2d");
  state.chart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
          grid: { color: "#334155" },
        },
        y: {
          ticks: { color: "#94a3b8", callback: (v) => fmtWatt(v) },
          grid: { color: "#334155" },
        },
      },
      plugins: {
        legend: { labels: { color: "#e2e8f0" } },
        tooltip: {
          callbacks: { label: (item) => `${item.dataset.label}: ${fmtWatt(item.parsed.y)}` },
        },
      },
    },
  });
}

function setupRangeButtons() {
  const container = el("range-buttons");
  container.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-hours]");
    if (!btn) return;
    for (const b of container.querySelectorAll("button")) b.classList.remove("active");
    btn.classList.add("active");
    state.hours = Number(btn.dataset.hours);
    refreshChart();
  });
}

async function refreshAll() {
  try {
    await Promise.all([refreshLiveCards(), refreshSummaryCards(), refreshChart()]);
  } catch (err) {
    console.error(err);
  }
}

async function init() {
  await loadDevices();
  setupRangeButtons();
  await refreshAll();
  setInterval(() => {
    refreshLiveCards().catch(console.error);
    refreshSummaryCards().catch(console.error);
  }, 20000);
  setInterval(() => refreshChart().catch(console.error), 5 * 60 * 1000);
}

init();
