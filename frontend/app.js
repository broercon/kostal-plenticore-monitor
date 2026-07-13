const state = {
  currentUser: null, // {id, username, role, must_change_password}, gesetzt in checkAuth()
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
  hourlyCompare: {
    days: 1,
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

async function fetchJson(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    // Sitzung nicht (mehr) gueltig - z.B. nach Ablauf, Logout in einem
    // anderen Tab, oder Container-Neustart mit geleerter Session-Tabelle -
    // zurueck zur Login-Seite statt kryptischer Fehlermeldungen im Dashboard.
    window.location.href = "login.html";
    // Nie aufloesende Promise, damit nachfolgender Code (der die Antwort
    // verarbeiten wuerde) nicht mehr ausgefuehrt wird, waehrend der Browser
    // zur Login-Seite navigiert.
    return new Promise(() => {});
  }
  if (!res.ok) throw new Error(`Request an ${url} fehlgeschlagen: ${res.status}`);
  return res.json();
}

// --- Anmeldung: Nutzerinfo pruefen, Topbar fuellen, Logout/Passwort-Aenderung
// und (fuer Admins) Benutzerverwaltung einrichten. ---

async function checkAuth() {
  const res = await fetch("/api/auth/me");
  if (!res.ok) {
    window.location.href = "login.html";
    return new Promise(() => {}); // init() haelt hier an, s.o.
  }
  state.currentUser = await res.json();

  const roleLabel = state.currentUser.role === "admin" ? "Admin" : "Betreiber";
  el("user-info").textContent = `${state.currentUser.username} (${roleLabel})`;

  if (state.currentUser.role === "admin") {
    el("admin-panel-btn").classList.remove("hidden");
  }

  if (state.currentUser.must_change_password) {
    openChangePasswordModal(
      "Bitte vergeben Sie jetzt ein eigenes Passwort (aktuell ist noch das " +
        "initiale/zurückgesetzte Passwort aktiv)."
    );
  }
}

function openChangePasswordModal(hintText) {
  el("cp-error").textContent = hintText || "";
  el("cp-current").value = "";
  el("cp-new").value = "";
  el("change-password-overlay").classList.remove("hidden");
  el("cp-current").focus();
}

function closeChangePasswordModal() {
  el("change-password-overlay").classList.add("hidden");
}

function setupChangePassword() {
  el("change-password-btn").addEventListener("click", () => openChangePasswordModal());
  el("cp-cancel").addEventListener("click", closeChangePasswordModal);

  el("change-password-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = el("cp-error");
    const submitBtn = el("cp-submit");
    errorEl.textContent = "";
    submitBtn.disabled = true;
    try {
      const res = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: el("cp-current").value,
          new_password: el("cp-new").value,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        errorEl.textContent = data.detail || "Passwort konnte nicht geändert werden.";
        return;
      }
      state.currentUser.must_change_password = false;
      closeChangePasswordModal();
    } catch (err) {
      console.error(err);
      errorEl.textContent = "Verbindung zum Server fehlgeschlagen.";
    } finally {
      submitBtn.disabled = false;
    }
  });
}

function setupLogout() {
  el("logout-btn").addEventListener("click", async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch (err) {
      console.error(err);
    }
    window.location.href = "login.html";
  });
}

// --- Admin-Panel: Liste aller Nutzer, Passwort-Reset ---

async function loadAdminUserTable() {
  const users = await fetchJson("/api/admin/users");
  const tbody = el("admin-user-table-body");
  tbody.innerHTML = "";
  for (const user of users) {
    const tr = document.createElement("tr");

    const nameTd = document.createElement("td");
    nameTd.textContent = user.username;
    tr.appendChild(nameTd);

    const roleTd = document.createElement("td");
    roleTd.textContent = user.role === "admin" ? "Admin" : "Betreiber";
    tr.appendChild(roleTd);

    const actionTd = document.createElement("td");
    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.textContent = "Passwort zurücksetzen";
    resetBtn.addEventListener("click", async () => {
      if (!confirm(`Neues zufälliges Passwort für "${user.username}" setzen?`)) return;
      resetBtn.disabled = true;
      try {
        const res = await fetch(`/api/admin/users/${user.id}/reset-password`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          el("admin-reset-result").textContent = data.detail || "Fehler beim Zurücksetzen.";
          return;
        }
        el("admin-reset-result").textContent =
          `Neues Passwort für ${data.username}: ${data.new_password} ` +
          `(bitte notieren und weitergeben – wird nicht erneut angezeigt).`;
      } catch (err) {
        console.error(err);
        el("admin-reset-result").textContent = "Verbindung zum Server fehlgeschlagen.";
      } finally {
        resetBtn.disabled = false;
      }
    });
    actionTd.appendChild(resetBtn);
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }
}

function setupAdminPanel() {
  el("admin-panel-btn").addEventListener("click", async () => {
    el("admin-reset-result").textContent = "";
    el("admin-panel-overlay").classList.remove("hidden");
    try {
      await loadAdminUserTable();
    } catch (err) {
      console.error(err);
    }
  });
  el("admin-panel-close").addEventListener("click", () => {
    el("admin-panel-overlay").classList.add("hidden");
  });
}

async function loadDevices() {
  state.devices = await fetchJson("/api/devices");
  const container = el("device-tabs");
  container.innerHTML = "";

  function makeTab(deviceId, label) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.dataset.deviceId = deviceId;
    return btn;
  }

  function applyActiveTab() {
    for (const btn of container.querySelectorAll("button")) {
      btn.classList.toggle("active", btn.dataset.deviceId === state.selectedDeviceId);
    }
  }

  container.appendChild(
    makeTab("", state.devices.length > 1 ? "Alle (Summe)" : "Wechselrichter")
  );
  for (const device of state.devices) {
    container.appendChild(makeTab(device.id, device.name));
  }
  applyActiveTab();

  container.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-device-id]");
    if (!btn) return;
    state.selectedDeviceId = btn.dataset.deviceId;
    applyActiveTab();
    refreshAll({ showLoading: true });
  });
}

function sumField(readings, field) {
  const values = readings.map((r) => r[field]).filter((v) => v !== null && v !== undefined);
  if (values.length === 0) return null;
  return values.reduce((a, b) => a + b, 0);
}

// Spezielle device_id, unter der das Backend bei mehreren Wechselrichtern
// bereits korrekt zusammengefasste Werte liefert (Energiebilanz statt
// naivem Summieren der - bei mehreren Geraeten am selben Hausanschluss
// potenziell falschen - einzelnen Home_P-Werte). Siehe README-Abschnitt
// "Mehrere Wechselrichter: Hausverbrauch/Netz korrekt berechnen".
const COMBINED_DEVICE_ID = "_all_";

function updateHouseWideNotes() {
  // Hausverbrauch/Einspeisung/Netzbezug (und die entsprechenden Tagessummen)
  // sind hausweite Groessen und werden - anders als PV/Batterie - auch bei
  // Auswahl eines einzelnen Wechselrichters bewusst als Hauswert angezeigt
  // (siehe refreshLiveCards). Damit das beim Tab-Wechsel nicht wie eine
  // "haengengebliebene" Anzeige wirkt, wird dann ein kleiner Hinweis an
  // diesen Karten eingeblendet.
  const show = state.selectedDeviceId !== "" && state.devices.length > 1;
  for (const note of document.querySelectorAll("[data-house-note]")) {
    note.classList.toggle("hidden", !show);
  }
}

async function refreshLiveCards() {
  // Snapshot der Auswahl VOR dem Netzwerk-Aufruf: wird waehrenddessen ein
  // anderer Tab gewaehlt (schnelles Klicken), darf die verspaetete Antwort
  // die Anzeige nicht mehr ueberschreiben (Race Condition).
  const dev = state.selectedDeviceId;
  const latest = await fetchJson("/api/readings/latest");
  if (state.selectedDeviceId !== dev) return;
  const combined = latest.find((r) => r.device_id === COMBINED_DEVICE_ID);
  const perDevice = latest.filter((r) => r.device_id !== COMBINED_DEVICE_ID);

  const relevant = state.selectedDeviceId
    ? perDevice.filter((r) => r.device_id === state.selectedDeviceId)
    : perDevice;

  // Fuer "Alle (Summe)" den vom Backend bereits korrekt zusammengefassten
  // Eintrag bevorzugen, falls vorhanden (nur bei mehreren konfigurierten
  // Wechselrichtern geliefert) - sonst wie bisher die einzelnen Geraete
  // client-seitig summieren (z.B. bei nur einem konfigurierten Geraet).
  const cardSource = !state.selectedDeviceId && combined ? [combined] : relevant;

  // Hausverbrauch/Einspeisung/Netzbezug sind hausweite Groessen - bei
  // mehreren Wechselrichtern IMMER den zusammengefassten Wert nutzen, auch
  // wenn oben ein einzelnes Geraet ausgewaehlt ist. Grund: der eigene
  // Home_P-Wert eines einzelnen Wechselrichters kann bei einem zweiten,
  // unbeachteten Wechselrichter am selben Hausanschluss stark falsch/negativ
  // sein (siehe README "Mehrere Wechselrichter ..."). Nur PV-Leistung/
  // Batterie bleiben pro ausgewaehltem Geraet.
  const houseWideSource = combined ? [combined] : relevant;

  el("card-home").textContent = fmtWatt(sumField(houseWideSource, "home_power_w"));
  el("card-feedin").textContent = fmtWatt(sumField(houseWideSource, "feed_in_power_w"));
  el("card-griddraw").textContent = fmtWatt(sumField(houseWideSource, "grid_draw_power_w"));
  el("card-pv").textContent = fmtWatt(sumField(cardSource, "pv_power_w"));

  // Batterie-Ladezustand (SoC) gibt es nur pro echtem Geraet (der
  // zusammengefasste "_all_"-Eintrag hat keinen eigenen SoC-Wert) - dafuer
  // immer die einzelnen Geraete verwenden, auch in der "Alle"-Ansicht.
  const batteryEntries = relevant.filter(
    (r) => r.battery_soc_percent !== null && r.battery_soc_percent !== undefined
  );
  if (batteryEntries.length === 0) {
    el("card-battery-wrapper").style.display = "none";
  } else {
    el("card-battery-wrapper").style.display = "";
    const batteryPower = fmtWatt(sumField(cardSource, "battery_power_w"));
    // Geraetename nur anzeigen, wenn mehrere Batterien gleichzeitig sichtbar
    // sind (z.B. "Alle (Summe)" mit zwei Wechselrichtern mit Batterie) - bei
    // nur einem Eintrag ist die Zuordnung schon durch den Filter oben
    // eindeutig, der Name waere redundant.
    const socText =
      batteryEntries.length > 1
        ? batteryEntries
            .map((r) => `${r.device_name}: ${fmtPercent(r.battery_soc_percent)}`)
            .join(" · ")
        : fmtPercent(batteryEntries[0].battery_soc_percent);
    el("card-battery").textContent = `${batteryPower} (${socText})`;
  }

  if (relevant.length > 0) {
    const newest = relevant.reduce((a, b) => (a.timestamp > b.timestamp ? a : b));
    const t = new Date(newest.timestamp);
    el("last-update").textContent = "Letzte Aktualisierung: " + t.toLocaleTimeString("de-DE");
  }

  updateHouseWideNotes();
}

async function refreshSummaryCards() {
  const dev = state.selectedDeviceId;
  const summaries = await fetchJson("/api/readings/today-summary");
  if (state.selectedDeviceId !== dev) return;
  const combined = summaries.find((s) => s.device_id === COMBINED_DEVICE_ID);
  const perDevice = summaries.filter((s) => s.device_id !== COMBINED_DEVICE_ID);

  const relevant = state.selectedDeviceId
    ? perDevice.filter((s) => s.device_id === state.selectedDeviceId)
    : perDevice;
  // Wie bei refreshLiveCards(): fuer "Alle (Summe)" den vom Backend bereits
  // korrekt berechneten Eintrag bevorzugen, falls vorhanden.
  const summarySource = !state.selectedDeviceId && combined ? [combined] : relevant;
  // Verbrauch/Einspeisung heute sind hausweite Groessen - siehe
  // refreshLiveCards() fuer die Begruendung.
  const houseWideSource = combined ? [combined] : relevant;

  el("summary-yield").textContent = fmtKwh(sumField(summarySource, "yield_day_kwh"));
  el("summary-consumption").textContent = fmtKwh(
    sumField(houseWideSource, "home_consumption_day_kwh")
  );
  el("summary-grid").textContent = fmtKwh(sumField(houseWideSource, "energy_grid_day_kwh"));

  updateHouseWideNotes();
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
  const reqDeviceId = state.selectedDeviceId;
  const reqHours = state.hours;
  const isDayMode = state.hours <= 24;
  const mode = isDayMode ? "day" : "range";
  const bucketMinutes = bucketMinutesForRange(state.hours);
  const params = new URLSearchParams({
    hours: String(state.hours),
    bucket_minutes: String(bucketMinutes),
  });
  if (state.selectedDeviceId) params.set("device_id", state.selectedDeviceId);

  const points = await fetchJson(`/api/readings/history?${params.toString()}`);
  // Auswahl waehrend des Ladens geaendert? Dann Ergebnis verwerfen.
  if (state.selectedDeviceId !== reqDeviceId || state.hours !== reqHours) return;

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
  const reqDeviceId = state.selectedDeviceId;
  const params = new URLSearchParams({
    days: String(days),
    bucket_minutes: "15",
  });
  if (state.selectedDeviceId) params.set("device_id", state.selectedDeviceId);

  const result = await fetchJson(`/api/readings/day-profile?${params.toString()}`);
  if (
    state.selectedDeviceId !== reqDeviceId ||
    state.dayCompare.metric !== metric ||
    state.dayCompare.days !== days
  )
    return;
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

// --- Tagesverbrauch: gestapeltes Saeulendiagramm mit taeglichen kWh-Summen,
// eingefaerbt nach Deckungsanteil (PV/Speicher/Netz). Hausverbrauch ist eine
// hausweite Groesse (siehe refreshLiveCards) - der Endpunkt liefert daher
// immer die ueber alle Wechselrichter korrigierte Gesamt-Aufteilung,
// unabhaengig davon, welcher Tab oben ausgewaehlt ist. ---

const DAILY_BREAKDOWN_COLORS = {
  pv: "#60a5fa",
  battery: "#c084fc",
  grid: "#facc15",
};

async function refreshDailyTotalsChart() {
  const reqDays = state.dailyTotals.days;
  const params = new URLSearchParams({ days: String(state.dailyTotals.days) });
  const result = await fetchJson(`/api/readings/daily-home-breakdown?${params.toString()}`);
  if (state.dailyTotals.days !== reqDays) return;
  const labels = result.days.map((d) => shortDate(d.date));

  const datasets = [
    {
      label: "Aus PV",
      data: result.days.map((d) => d.pv_kwh),
      backgroundColor: DAILY_BREAKDOWN_COLORS.pv + "99",
      borderColor: DAILY_BREAKDOWN_COLORS.pv,
      borderWidth: 1,
      stack: "verbrauch",
    },
    {
      label: "Aus Speicher",
      data: result.days.map((d) => d.battery_kwh),
      backgroundColor: DAILY_BREAKDOWN_COLORS.battery + "99",
      borderColor: DAILY_BREAKDOWN_COLORS.battery,
      borderWidth: 1,
      stack: "verbrauch",
    },
    {
      label: "Aus Netz",
      data: result.days.map((d) => d.grid_kwh),
      backgroundColor: DAILY_BREAKDOWN_COLORS.grid + "99",
      borderColor: DAILY_BREAKDOWN_COLORS.grid,
      borderWidth: 1,
      stack: "verbrauch",
    },
  ];

  if (state.dailyTotals.chart) {
    state.dailyTotals.chart.data.labels = labels;
    state.dailyTotals.chart.data.datasets = datasets;
    state.dailyTotals.chart.update();
    return;
  }

  const ctx = el("dailytotals-chart").getContext("2d");
  state.dailyTotals.chart = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          stacked: true,
          ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 20 },
          grid: { display: false },
        },
        y: {
          stacked: true,
          ticks: { color: "#94a3b8", callback: (v) => `${v} kWh` },
          grid: { color: "#334155" },
          title: { display: true, text: "Hausverbrauch (kWh)", color: "#94a3b8" },
        },
      },
      plugins: {
        legend: { labels: { color: "#e2e8f0" } },
        tooltip: {
          callbacks: {
            label: (item) =>
              item.parsed.y === null
                ? `${item.dataset.label}: keine Daten`
                : `${item.dataset.label}: ${item.parsed.y.toFixed(1)} kWh`,
            footer: (items) => {
              const total = items.reduce((sum, item) => sum + (item.parsed.y || 0), 0);
              return `Gesamt: ${total.toFixed(1)} kWh`;
            },
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

// --- Wechselrichter-Vergleich: gestapeltes Saeulendiagramm, PV-Ertrag pro
// Stunde je Wechselrichter (nicht summiert), damit sich die Geraete direkt
// farblich vergleichen lassen. Nutzt die Metrik "pv" (gesamte erzeugte
// Energie), nicht "feed_in" (nur die Einspeisung) - der Nutzer will den
// kompletten Ertrag sehen, egal ob eingespeist oder direkt im Haus
// verbraucht. Ergibt nur einen Sinn, wenn oben "Alle (Summe)" ausgewaehlt
// ist (mehrere Wechselrichter zum Vergleichen) - bei einem einzelnen
// ausgewaehlten Geraet wird der ganze Abschnitt ausgeblendet, siehe
// updateHourlyCompareVisibility(). ---

function hourLabel(bucketIso, multiDay) {
  // bucketIso z.B. "2026-07-12T14:00:00" (lokale Zeit, ohne Offset)
  const [datePart, timePart] = bucketIso.split("T");
  const hour = timePart.slice(0, 2);
  if (!multiDay) return `${hour} Uhr`;
  const [, m, d] = datePart.split("-");
  return `${d}.${m}. ${hour}h`;
}

function updateHourlyCompareVisibility() {
  // Der Vergleich zwischen Wechselrichtern ergibt nur Sinn, wenn oben
  // "Alle (Summe)" ausgewaehlt ist (selectedDeviceId === "") UND es
  // ueberhaupt mehr als einen Wechselrichter gibt - bei einem einzelnen
  // ausgewaehlten (oder einzigen konfigurierten) Geraet gaebe es nichts zu
  // vergleichen.
  const visible = state.selectedDeviceId === "" && state.devices.length > 1;
  el("hourly-section").classList.toggle("hidden", !visible);
  return visible;
}

async function refreshHourlyCompareChart() {
  if (!updateHourlyCompareVisibility()) return;

  const days = state.hourlyCompare.days;
  const params = new URLSearchParams({ metric: "pv", days: String(days) });

  const result = await fetchJson(`/api/readings/hourly-per-device?${params.toString()}`);
  // Waehrend des Ladens auf einen einzelnen WR gewechselt (Diagramm dann
  // ausgeblendet) oder anderer Zeitraum gewaehlt? Ergebnis verwerfen.
  if (state.selectedDeviceId !== "" || state.hourlyCompare.days !== days) return;
  const multiDay = days > 1;
  const labels = result.buckets.map((b) => hourLabel(b.bucket, multiDay));

  const datasets = result.devices.map((device, i) => ({
    label: device.device_name,
    data: result.buckets.map((b) => b.values[device.device_id]),
    backgroundColor: dayColor(i),
    borderColor: dayColor(i),
    borderWidth: 1,
    borderRadius: 2,
    stack: "ertrag",
  }));

  if (state.hourlyCompare.chart) {
    state.hourlyCompare.chart.data.labels = labels;
    state.hourlyCompare.chart.data.datasets = datasets;
    state.hourlyCompare.chart.update();
    return;
  }

  const ctx = el("hourly-chart").getContext("2d");
  state.hourlyCompare.chart = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      // "index" + intersect:false: beim Hovern ueber eine Stunde (egal auf
      // welchem der gestapelten Balken der Maus-Zeiger genau liegt) werden
      // alle Wechselrichter fuer diese Stunde im Tooltip aufgelistet, nicht
      // nur der eine, direkt getroffene Balken.
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          stacked: true,
          ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 24 },
          grid: { display: false },
        },
        y: {
          stacked: true,
          ticks: { color: "#94a3b8", callback: (v) => `${v} kWh` },
          grid: { color: "#334155" },
          title: { display: true, text: "PV-Ertrag (kWh)", color: "#94a3b8" },
        },
      },
      plugins: {
        legend: { labels: { color: "#e2e8f0" } },
        tooltip: {
          callbacks: {
            label: (item) =>
              item.parsed.y === null
                ? `${item.dataset.label}: keine Daten`
                : `${item.dataset.label}: ${item.parsed.y.toFixed(2)} kWh`,
            // Zeigt zusaetzlich die Summe aller Wechselrichter fuer diese
            // Stunde an, damit man neben den Einzelwerten auch den
            // Gesamtertrag auf einen Blick sieht.
            footer: (items) => {
              const total = items.reduce((sum, item) => sum + (item.parsed.y || 0), 0);
              return `Gesamt: ${total.toFixed(2)} kWh`;
            },
          },
        },
      },
    },
  });
}

function setupHourlyCompareControls() {
  const container = el("hourly-day-buttons");
  container.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-days]");
    if (!btn) return;
    for (const b of container.querySelectorAll("button")) b.classList.remove("active");
    btn.classList.add("active");
    state.hourlyCompare.days = Number(btn.dataset.days);
    refreshHourlyCompareChart().catch(console.error);
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

// Panels, ueber die beim (Neu-)Laden ein Ladeindikator gelegt wird - die
// alten Werte werden dabei abgedimmt, bis die neuen Daten da sind.
const LOADING_PANEL_SELECTORS = ["#live-cards", "#summary-cards", ".chart-canvas-wrapper"];

function setDashboardLoading(on) {
  for (const sel of LOADING_PANEL_SELECTORS) {
    for (const node of document.querySelectorAll(sel)) {
      node.classList.toggle("is-loading", on);
    }
  }
}

// Laufende Nummer des juengsten Lade-Vorgangs: nur der zuletzt ausgeloeste
// (z.B. der zuletzt angeklickte Tab) darf den Ladeindikator wieder
// ausblenden. So bleibt der Spinner bei schnellem Klicken sichtbar, bis die
// tatsaechlich zuletzt gewaehlte Ansicht geladen ist, und bleibt umgekehrt
// nicht faelschlich haengen, wenn eine aeltere Abfrage spaeter fertig wird.
let loadingSeq = 0;

async function refreshAll({ showLoading = false } = {}) {
  let myToken = 0;
  if (showLoading) {
    myToken = ++loadingSeq;
    setDashboardLoading(true);
  }
  try {
    await Promise.all([
      refreshLiveCards(),
      refreshSummaryCards(),
      refreshChart(),
      refreshDayCompareChart(),
      refreshDailyTotalsChart(),
      refreshHourlyCompareChart(),
    ]);
  } catch (err) {
    console.error(err);
  } finally {
    if (showLoading && myToken === loadingSeq) setDashboardLoading(false);
  }
}

async function init() {
  await checkAuth(); // leitet bei fehlender/ungueltiger Sitzung zu login.html um
  setupChangePassword();
  setupLogout();
  setupAdminPanel();
  await loadDevices();
  setupRangeButtons();
  setupDayCompareControls();
  setupDailyTotalsControls();
  setupHourlyCompareControls();
  setupImportTrigger();
  await refreshAll({ showLoading: true });
  setInterval(() => {
    refreshLiveCards().catch(console.error);
    refreshSummaryCards().catch(console.error);
  }, 20000);
  setInterval(() => refreshChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshDayCompareChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshDailyTotalsChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshHourlyCompareChart().catch(console.error), 5 * 60 * 1000);
}

init();
