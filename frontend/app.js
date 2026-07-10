const state = {
  devices: [],
  selectedDeviceId: "", // "" bedeutet: alle Geraete summiert
  hours: 24,
  chart: null,
  chartMode: null, // "day" (feste 00:00-24:00 Achse) | "range" (rollierend, Datumslabels)
  dayCompare: {
    metric: "pv", // "pv" | "solar_battery" | "grid"
    days: 7,
    chart: null,
  },
  dailyTotals: {
    days: 30,
    chart: null,
  },
};

// Maximale Tage, bei denen die Solar/Batterie-Aufteilung (2 Kurven pro Tag)
// noch lesbar bleibt. Bei mehr Tagen wuerde die Legende zu unuebersichtlich.
const SOLAR_BATTERY_MAX_DAYS = 7;

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

  const gridRaw = sumField(relevant, "grid_power_w");
  el("card-grid-raw").textContent =
    gridRaw === null ? "–" : (gridRaw >= 0 ? "+" : "") + fmtWatt(gridRaw);

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

const CHART_METRIC_COLORS = {
  home: "#f87171",
  feedin: "#4ade80",
  griddraw: "#facc15",
  pv: "#60a5fa",
};

function minuteOfLocalDay(d) {
  return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;
}

async function refreshChart() {
  const isDayMode = state.hours <= 24;
  const mode = isDayMode ? "day" : "range";
  const bucketMinutes = bucketMinutesForRange(state.hours);
  const params = new URLSearchParams({
    hours: String(state.hours),
    bucket_minutes: String(bucketMinutes),
  });
  if (state.selectedDeviceId) params.set("device_id", state.selectedDeviceId);

  const points = await fetchJson(`/api/readings/history?${params.toString()}`);

  let labels = null;
  const fieldFor = { home: "home_power_w", feedin: "feed_in_power_w", griddraw: "grid_draw_power_w", pv: "pv_power_w" };
  const metricLabel = { home: "Hausverbrauch", feedin: "Einspeisung", griddraw: "Netzbezug", pv: "PV-Leistung" };

  let datasets;
  if (isDayMode) {
    // Feste 00:00-24:00-Achse (wie beim Tagesvergleich): das Diagramm zeigt
    // also immer den ganzen Tag, auch wenn aktuell erst z.B. 14 Uhr ist -
    // der restliche Tag bleibt dann leer, statt dass die Achse "dynamisch"
    // beim jeweils letzten Messwert endet.
    datasets = Object.keys(fieldFor).map((key) => ({
      label: metricLabel[key],
      data: points.map((p) => ({ x: minuteOfLocalDay(new Date(p.timestamp)), y: p[fieldFor[key]] })),
      borderColor: CHART_METRIC_COLORS[key],
      backgroundColor: CHART_METRIC_COLORS[key] + "33",
      tension: 0.25,
      pointRadius: 0,
    }));
  } else {
    labels = points.map((p) => {
      const d = new Date(p.timestamp);
      return d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    });
    datasets = Object.keys(fieldFor).map((key) => ({
      label: metricLabel[key],
      data: points.map((p) => p[fieldFor[key]]),
      borderColor: CHART_METRIC_COLORS[key],
      backgroundColor: CHART_METRIC_COLORS[key] + "33",
      tension: 0.25,
      pointRadius: 0,
    }));
  }

  if (state.chart && state.chartMode === mode) {
    state.chart.data.labels = labels;
    state.chart.data.datasets = datasets;
    state.chart.update();
    return;
  }

  if (state.chart) {
    state.chart.destroy();
    state.chart = null;
  }
  state.chartMode = mode;

  const ctx = el("power-chart").getContext("2d");
  const xScale = isDayMode
    ? {
        type: "linear",
        min: 0,
        max: 1440,
        ticks: { color: "#94a3b8", stepSize: 120, callback: (v) => minutesToLabel(v) },
        grid: { color: "#334155" },
      }
    : {
        ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
        grid: { color: "#334155" },
      };

  state.chart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: xScale,
        y: {
          ticks: { color: "#94a3b8", callback: (v) => fmtWatt(v) },
          grid: { color: "#334155" },
        },
      },
      plugins: {
        legend: { labels: { color: "#e2e8f0" } },
        tooltip: {
          callbacks: {
            title: (items) =>
              isDayMode && items.length ? minutesToLabel(items[0].parsed.x) : undefined,
            label: (item) => `${item.dataset.label}: ${fmtWatt(item.parsed.y)}`,
          },
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

// --- Tagesvergleich: mehrere Tage auf einer festen 00:00-24:00-Achse ---

function minutesToLabel(minute) {
  const h = Math.floor(minute / 60);
  const m = minute % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function shortDate(dateStr) {
  const [y, m, d] = dateStr.split("-");
  return `${d}.${m}.`;
}

// Klar unterscheidbare Farbpalette, eine feste Farbe pro Tag (statt
// verblassendem Farbverlauf) - bei mehr Tagen als Farben wird zyklisch
// wiederverwendet.
const DAY_COLORS = [
  "#60a5fa", // blau
  "#f87171", // rot
  "#4ade80", // gruen
  "#facc15", // gelb
  "#c084fc", // lila
  "#22d3ee", // cyan
  "#fb923c", // orange
  "#f472b6", // pink
  "#a3e635", // limette
  "#94a3b8", // grau-blau
];

function dayColor(index) {
  return DAY_COLORS[index % DAY_COLORS.length];
}

function buildDayCompareDatasets(days, metric) {
  const datasets = [];
  const total = days.length;

  days.forEach((day, i) => {
    const isLatest = i === total - 1;
    const width = isLatest ? 2.5 : 1.5;
    const dateLabel = shortDate(day.date);

    if (metric === "pv") {
      datasets.push({
        label: dateLabel,
        data: day.points.map((p) => ({ x: p.minute, y: p.pv_power_w })),
        borderColor: dayColor(i),
        backgroundColor: "transparent",
        borderWidth: width,
        tension: 0.25,
        pointRadius: 0,
      });
    } else if (metric === "grid") {
      datasets.push({
        label: dateLabel,
        data: day.points.map((p) => ({ x: p.minute, y: p.grid_draw_power_w })),
        borderColor: dayColor(i),
        backgroundColor: "transparent",
        borderWidth: width,
        tension: 0.25,
        pointRadius: 0,
      });
    } else {
      // solar_battery: zwei Kurven pro Tag - durchgezogen = Solaranteil,
      // gestrichelt = Batterieanteil, jeweils in der gleichen Tagesfarbe.
      const color = dayColor(i);
      datasets.push({
        label: `${dateLabel} · Solar`,
        data: day.points.map((p) => ({ x: p.minute, y: p.home_from_solar_w })),
        borderColor: color,
        backgroundColor: "transparent",
        borderWidth: width,
        borderDash: [],
        tension: 0.25,
        pointRadius: 0,
      });
      datasets.push({
        label: `${dateLabel} · Batterie`,
        data: day.points.map((p) => ({ x: p.minute, y: p.home_from_battery_w })),
        borderColor: color,
        backgroundColor: "transparent",
        borderWidth: width,
        borderDash: [6, 4],
        tension: 0.25,
        pointRadius: 0,
      });
    }
  });

  return datasets;
}

function updateDayCompareHint(metric) {
  const hint = el("daycompare-hint");
  if (metric === "solar_battery") {
    hint.textContent =
      `Durchgezogene Linie = Hausverbrauch aus Solar, gestrichelte Linie (gleiche ` +
      `Farbe) = aus der Batterie. Nur bei live erfassten Daten verfügbar ` +
      `(nicht bei importierten Altdaten ohne Netzmessung) und auf ${SOLAR_BATTERY_MAX_DAYS} ` +
      `Tage begrenzt, damit die Legende lesbar bleibt.`;
  } else {
    hint.textContent =
      "Tipp: Auf einen Tag in der Legende klicken, um ihn ein- oder auszublenden – " +
      "hilfreich, um z.B. nur zwei bestimmte Tage gegenüberzustellen. Jeder Tag hat " +
      "eine eigene, feste Farbe (der aktuellste Tag etwas dicker gezeichnet).";
  }
}

async function refreshDayCompareChart() {
  const { metric, days } = state.dayCompare;
  const params = new URLSearchParams({
    days: String(days),
    bucket_minutes: "15",
  });
  if (state.selectedDeviceId) params.set("device_id", state.selectedDeviceId);

  const result = await fetchJson(`/api/readings/day-profile?${params.toString()}`);
  const datasets = buildDayCompareDatasets(result.days, metric);

  const yLabel = metric === "pv" ? "PV-Leistung" : metric === "grid" ? "Netzbezug" : "Hausverbrauch";

  if (state.dayCompare.chart) {
    state.dayCompare.chart.data.datasets = datasets;
    state.dayCompare.chart.update();
    return;
  }

  const ctx = el("daycompare-chart").getContext("2d");
  state.dayCompare.chart = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: {
          type: "linear",
          min: 0,
          max: 1440,
          ticks: {
            color: "#94a3b8",
            stepSize: 120,
            callback: (v) => minutesToLabel(v),
          },
          grid: { color: "#334155" },
          title: { display: true, text: "Uhrzeit", color: "#94a3b8" },
        },
        y: {
          ticks: { color: "#94a3b8", callback: (v) => fmtWatt(v) },
          grid: { color: "#334155" },
          title: { display: true, text: yLabel, color: "#94a3b8" },
        },
      },
      plugins: {
        legend: { labels: { color: "#e2e8f0", boxWidth: 20 } },
        tooltip: {
          callbacks: {
            title: (items) => (items.length ? minutesToLabel(items[0].parsed.x) : ""),
            label: (item) => `${item.dataset.label}: ${fmtWatt(item.parsed.y)}`,
          },
        },
      },
    },
  });
}

function setupDayCompareControls() {
  const metricContainer = el("daycompare-metric-buttons");
  const dayContainer = el("daycompare-day-buttons");

  function applySolarBatteryDayLimit() {
    const isSolarBattery = state.dayCompare.metric === "solar_battery";
    for (const b of dayContainer.querySelectorAll("button")) {
      const days = Number(b.dataset.days);
      b.disabled = isSolarBattery && days > SOLAR_BATTERY_MAX_DAYS;
    }
    if (isSolarBattery && state.dayCompare.days > SOLAR_BATTERY_MAX_DAYS) {
      state.dayCompare.days = SOLAR_BATTERY_MAX_DAYS;
      for (const b of dayContainer.querySelectorAll("button")) {
        b.classList.toggle("active", Number(b.dataset.days) === SOLAR_BATTERY_MAX_DAYS);
      }
    }
  }

  metricContainer.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-metric]");
    if (!btn) return;
    for (const b of metricContainer.querySelectorAll("button")) b.classList.remove("active");
    btn.classList.add("active");
    state.dayCompare.metric = btn.dataset.metric;
    updateDayCompareHint(state.dayCompare.metric);
    applySolarBatteryDayLimit();
    // Bei Metrikwechsel muss der Chart neu aufgebaut werden (Achsentitel,
    // Anzahl Datasets pro Tag aendert sich zwischen 1 und 2).
    if (state.dayCompare.chart) {
      state.dayCompare.chart.destroy();
      state.dayCompare.chart = null;
    }
    refreshDayCompareChart().catch(console.error);
  });

  dayContainer.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-days]");
    if (!btn || btn.disabled) return;
    for (const b of dayContainer.querySelectorAll("button")) b.classList.remove("active");
    btn.classList.add("active");
    state.dayCompare.days = Number(btn.dataset.days);
    refreshDayCompareChart().catch(console.error);
  });
}

// --- Tagesverbrauch: Saeulendiagramm mit taeglichen kWh-Summen ---

async function refreshDailyTotalsChart() {
  const params = new URLSearchParams({
    metric: "home",
    days: String(state.dailyTotals.days),
  });
  if (state.selectedDeviceId) params.set("device_id", state.selectedDeviceId);

  const result = await fetchJson(`/api/readings/daily-totals?${params.toString()}`);
  const labels = result.days.map((d) => shortDate(d.date));
  const values = result.days.map((d) => d.kwh);

  const dataset = {
    label: "Hausverbrauch",
    data: values,
    backgroundColor: "#f8717199",
    borderColor: "#f87171",
    borderWidth: 1,
    borderRadius: 3,
  };

  if (state.dailyTotals.chart) {
    state.dailyTotals.chart.data.labels = labels;
    state.dailyTotals.chart.data.datasets = [dataset];
    state.dailyTotals.chart.update();
    return;
  }

  const ctx = el("dailytotals-chart").getContext("2d");
  state.dailyTotals.chart = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets: [dataset] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 20 },
          grid: { display: false },
        },
        y: {
          ticks: { color: "#94a3b8", callback: (v) => `${v} kWh` },
          grid: { color: "#334155" },
          title: { display: true, text: "Hausverbrauch (kWh)", color: "#94a3b8" },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (item) =>
              item.parsed.y === null ? "keine Daten" : `${item.parsed.y.toFixed(1)} kWh`,
          },
        },
      },
    },
  });
}

function setupDailyTotalsControls() {
  const container = el("dailytotals-day-buttons");
  container.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-days]");
    if (!btn) return;
    for (const b of container.querySelectorAll("button")) b.classList.remove("active");
    btn.classList.add("active");
    state.dailyTotals.days = Number(btn.dataset.days);
    refreshDailyTotalsChart().catch(console.error);
  });
}

// --- Logdaten-Abgleich manuell anstossen + Status anzeigen ---

let importPollTimer = null;

function stopImportPolling() {
  if (importPollTimer) {
    clearInterval(importPollTimer);
    importPollTimer = null;
  }
}

function fmtDateTime(iso) {
  if (!iso) return null;
  return new Date(iso).toLocaleString("de-DE");
}

function summarizeImportResults(results) {
  if (!results || results.length === 0) return "";
  return results
    .map((r) => {
      // Zeigt den tatsaechlich abgefragten Zeitraum mit an, damit sich sofort
      // erkennen laesst, ob z.B. AUTO_IMPORT_DAYS wirklich "unbegrenzt" bzw.
      // der erwartete Wert war (statt Logs durchsuchen zu muessen).
      const range = ` (Zeitraum ${r.range_begin} bis ${r.range_end})`;
      if (r.status === "ok") {
        return `${r.device_name}: ${r.inserted} neu, ${r.updated} befüllt, ${r.skipped} unverändert${range}`;
      }
      if (r.status === "timeout") {
        return `${r.device_name}: Zeitüberschreitung beim Download${range}`;
      }
      return `${r.device_name}: Fehler (${r.message ?? "unbekannt"})${range}`;
    })
    .join(" · ");
}

async function updateImportStatusUI() {
  const status = await fetchJson("/api/admin/import-history/status");
  const btn = el("trigger-import-btn");
  const text = el("import-status-text");

  if (status.running) {
    btn.disabled = true;
    const since = fmtDateTime(status.last_started_at);
    text.textContent = `Logdaten-Abgleich läuft${since ? " (gestartet " + since + ")" : ""} …`;
    if (!importPollTimer) {
      importPollTimer = setInterval(() => updateImportStatusUI().catch(console.error), 4000);
    }
    return;
  }

  stopImportPolling();
  btn.disabled = false;
  if (!status.last_finished_at) {
    text.textContent = "Noch nicht gelaufen.";
    return;
  }
  const summary = summarizeImportResults(status.results);
  text.textContent =
    `Zuletzt abgeschlossen: ${fmtDateTime(status.last_finished_at)}` +
    (summary ? ` – ${summary}` : "");
}

function setupImportTrigger() {
  const btn = el("trigger-import-btn");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    el("import-status-text").textContent = "Wird gestartet …";
    try {
      const res = await fetch("/api/admin/import-history", { method: "POST" });
      const data = await res.json();
      if (!data.started) {
        el("import-status-text").textContent = data.message || "Läuft bereits – bitte warten.";
      }
    } catch (err) {
      console.error(err);
      el("import-status-text").textContent = "Fehler beim Starten.";
    }
    updateImportStatusUI().catch(console.error);
  });
  updateImportStatusUI().catch(console.error);
}

async function refreshAll() {
  try {
    await Promise.all([
      refreshLiveCards(),
      refreshSummaryCards(),
      refreshChart(),
      refreshDayCompareChart(),
      refreshDailyTotalsChart(),
    ]);
  } catch (err) {
    console.error(err);
  }
}

async function init() {
  await loadDevices();
  setupRangeButtons();
  setupDayCompareControls();
  setupDailyTotalsControls();
  setupImportTrigger();
  await refreshAll();
  setInterval(() => {
    refreshLiveCards().catch(console.error);
    refreshSummaryCards().catch(console.error);
  }, 20000);
  setInterval(() => refreshChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshDayCompareChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshDailyTotalsChart().catch(console.error), 5 * 60 * 1000);
}

init();
