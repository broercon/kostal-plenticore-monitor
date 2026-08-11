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
  // Werte-Anzeige der Diagramme (Tooltip/Hover). Auf Touch-Geraeten
  // standardmaessig AUS, damit die Seite frei scrollt; per Umschalter aktivierbar.
  chartsInteractive: true,
  forecastChart: null,
  forecastAccuracyChart: null,
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

// Reine PV-Erzeugung in Watt (ohne die ggf. am PV3-String haengende
// Batterie): pv_power_w - battery_power_w, auf >= 0 begrenzt. Passt zur
// serverseitigen PV-Ertrag-Berechnung (integrate_pure_pv_kwh). Geraete ohne
// Batterie (battery_power_w null) liefern schlicht pv_power_w.
function purePvWatt(r) {
  if (r.pv_power_w === null || r.pv_power_w === undefined) return null;
  return Math.max(0, r.pv_power_w - (r.battery_power_w || 0));
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
    el("admin-area-btn").classList.remove("hidden");
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
      // Der Server invalidiert nach einem Passwortwechsel bewusst alle
      // bisherigen Sessions. Direkt zur erneuten Anmeldung wechseln.
      window.location.href = "login.html";
      return;
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

// --- Mail-Report-Panel: komplette Konfiguration des taeglichen
// Zusammenfassungs-Reports (aktiv/inaktiv, Uhrzeit, Empfaenger,
// Mail-Service-URL/API-Key/Absendername) - siehe README "Taeglicher
// Mail-Report". Nur fuer Rolle admin sichtbar (Button bleibt sonst
// versteckt), zusaetzlich serverseitig ueber /api/admin/daily-report/...
// abgesichert. ---

function fmtDailyReportStatus(status) {
  const parts = [];
  if (status.enabled) {
    parts.push(`Aktiv, taeglich ${status.scheduled_time} Uhr an ${status.recipients.join(", ") || "(keine Empfänger)"}`);
  } else {
    parts.push("Deaktiviert oder unvollständig konfiguriert");
  }
  if (status.last_sent_at) {
    const when = fmtDateTime(status.last_sent_at);
    const outcome = status.last_status === "ok" ? "erfolgreich" : "fehlgeschlagen";
    parts.push(`Letzter Versand: ${when} (${outcome})`);
    if (status.last_status !== "ok" && status.last_message) {
      parts.push(status.last_message);
    }
  } else {
    parts.push("Noch nie verschickt.");
  }
  return parts.join(" · ");
}

async function refreshDailyReportStatusText() {
  const status = await fetchJson("/api/admin/daily-report/status");
  el("dr-status-text").textContent = fmtDailyReportStatus(status);
}

async function loadDailyReportConfigIntoForm() {
  const cfg = await fetchJson("/api/admin/daily-report/config");
  el("dr-enabled").checked = cfg.enabled;
  el("dr-time").value = cfg.report_time;
  el("dr-recipients").value = cfg.recipients.join(", ");
  el("dr-url").value = cfg.mail_service_url;
  el("dr-from-name").value = cfg.mail_service_from_name;
  el("dr-api-key").value = "";
  el("dr-api-key-hint").textContent = cfg.mail_service_api_key_set
    ? "Ein API-Key ist hinterlegt. Leer lassen, um ihn unverändert zu übernehmen – nur ausfüllen, um ihn zu ersetzen."
    : "Noch kein API-Key hinterlegt.";
}

function parseRecipients(raw) {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

async function extractErrorMessage(res) {
  const data = await res.json().catch(() => ({}));
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    // FastAPI/Pydantic-Validierungsfehler (422): Liste von {loc, msg, ...}.
    return data.detail.map((d) => d.msg).join("; ");
  }
  return "Speichern fehlgeschlagen.";
}

function optionalNumber(value) {
  const trimmed = String(value ?? "").trim();
  if (trimmed === "") return null;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : null;
}

async function loadForecastConfigIntoForm() {
  const cfg = await fetchJson("/api/admin/forecast/config");
  el("fc-enabled").checked = cfg.enabled;
  el("fc-latitude").value = cfg.latitude ?? "";
  el("fc-longitude").value = cfg.longitude ?? "";
  el("fc-source-hint").textContent = cfg.source === "database"
    ? "Quelle: im Admin-Bereich gespeicherte Datenbankkonfiguration."
    : "Quelle: Startwerte aus inverters.json. Beim Speichern übernimmt die Datenbank.";
}

function fmtForecastTime(value) {
  if (!value) return "–";
  return new Date(value).toLocaleTimeString("de-DE", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

async function refreshForecast() {
  const data = await fetchJson("/api/forecast");
  const status = el("forecast-status");
  const dayContainer = el("forecast-days");
  dayContainer.innerHTML = "";
  if (!data.available) {
    status.textContent = data.message;
    if (state.forecastChart) {
      state.forecastChart.destroy();
      state.forecastChart = null;
    }
    return;
  }

  status.textContent =
    `${data.message} Grundlage: ${data.training_samples} historische Stunden, ` +
    `aktualisiert ${fmtDateTime(data.generated_at)}.`;
  if (data.models?.length) {
    const modelText = data.models.map((model) => {
      const method = model.method === "learned" ? "gelernt" : "Standard";
      const error = model.validation_error_percent === null
        ? "noch ohne Rückvergleich"
        : `${model.validation_error_percent.toFixed(1)} % historischer Fehler`;
      return `${model.device_name}: ${method}, ${error}`;
    }).join(" · ");
    status.textContent += ` Modelle: ${modelText}.`;
  }
  for (const day of data.days) {
    const card = document.createElement("div");
    card.className = "forecast-day";
    const date = document.createElement("strong");
    date.textContent = new Date(`${day.date}T12:00:00`).toLocaleDateString("de-DE", {
      weekday: "short",
      day: "2-digit",
      month: "2-digit",
    });
    const value = document.createElement("span");
    value.className = "forecast-day-value";
    value.textContent = `${day.expected_kwh.toFixed(1)} kWh`;
    const range = document.createElement("span");
    range.className = "muted";
    range.textContent = `Bereich ${day.low_kwh.toFixed(1)}–${day.high_kwh.toFixed(1)} kWh`;
    const windowText = document.createElement("span");
    windowText.className = "muted";
    windowText.textContent = day.production_start
      ? `${fmtForecastTime(day.production_start)}–${fmtForecastTime(day.production_end)}, Spitze ${day.peak_kw.toFixed(1)} kW`
      : "Keine nennenswerte Erzeugung erwartet";
    const devices = document.createElement("span");
    devices.className = "forecast-day-devices";
    devices.textContent = day.devices
      .map((device) => `${device.device_name}: ${device.expected_kwh.toFixed(1)} kWh`)
      .join(" · ");
    card.append(date, value, range, windowText, devices);
    dayContainer.appendChild(card);
  }

  const labels = data.hours.map((hour) =>
    new Date(hour.timestamp).toLocaleString("de-DE", {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    })
  );
  const datasets = [
    {
      label: "Unterer Bereich",
      data: data.hours.map((hour) => hour.low_kw),
      borderColor: "rgba(59, 130, 246, 0)",
      pointRadius: 0,
      fill: false,
    },
    {
      label: "Prognosebereich",
      data: data.hours.map((hour) => hour.high_kw),
      borderColor: "rgba(59, 130, 246, 0)",
      backgroundColor: "rgba(59, 130, 246, 0.16)",
      pointRadius: 0,
      fill: "-1",
    },
    {
      label: "Erwartete PV-Leistung",
      data: data.hours.map((hour) => hour.expected_kw),
      borderColor: "#38bdf8",
      backgroundColor: "#38bdf8",
      pointRadius: 0,
      borderWidth: 2,
      tension: 0.2,
    },
  ];
  if (state.forecastChart) state.forecastChart.destroy();
  state.forecastChart = new Chart(el("forecast-chart").getContext("2d"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      events: chartEvents(),
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 14 } },
        y: { beginAtZero: true, title: { display: true, text: "kW" } },
      },
    },
  });
}

async function refreshForecastAccuracy() {
  const data = await fetchJson("/api/forecast/accuracy?days=30");
  const status = el("forecast-accuracy-status");
  const container = el("forecast-accuracy-days");
  const chartWrapper = el("forecast-accuracy-chart").parentElement;
  container.innerHTML = "";
  if (!data.available) {
    status.textContent = data.message;
    chartWrapper.classList.add("hidden");
    if (state.forecastAccuracyChart) {
      state.forecastAccuracyChart.destroy();
      state.forecastAccuracyChart = null;
    }
    return;
  }

  status.textContent = data.overall_accuracy_percent === null
    ? data.message
    : `${data.message} Gesamtgenauigkeit: ${data.overall_accuracy_percent.toFixed(1)} %.`;
  for (const day of data.days.slice(0, 7)) {
    const card = document.createElement("div");
    card.className = "forecast-accuracy-day";
    const date = document.createElement("strong");
    date.textContent = new Date(`${day.date}T12:00:00`).toLocaleDateString("de-DE", {
      weekday: "short",
      day: "2-digit",
      month: "2-digit",
    });
    const values = document.createElement("span");
    values.className = "forecast-accuracy-values";
    values.textContent = `Erwartet ${day.expected_kwh.toFixed(1)} · tatsächlich ${day.actual_kwh.toFixed(1)} kWh`;
    const difference = document.createElement("span");
    difference.className = "muted";
    const sign = day.difference_kwh > 0 ? "+" : "";
    const accuracy = day.accuracy_percent === null
      ? "Genauigkeit –"
      : `Genauigkeit ${day.accuracy_percent.toFixed(1)} %`;
    difference.textContent = `Abweichung ${sign}${day.difference_kwh.toFixed(1)} kWh · ${accuracy}`;
    difference.textContent += ` · ${day.matched_hours} Stundenwerte verglichen`;
    const devices = document.createElement("span");
    devices.className = "forecast-accuracy-devices";
    devices.textContent = day.devices.map((device) => {
      const deviceSign = device.difference_kwh > 0 ? "+" : "";
      return `${device.device_name}: ${device.expected_kwh.toFixed(1)} → ${device.actual_kwh.toFixed(1)} kWh (${deviceSign}${device.difference_kwh.toFixed(1)})`;
    }).join(" · ");
    card.append(date, values, difference, devices);
    container.appendChild(card);
  }

  const chronological = [...data.days].reverse();
  chartWrapper.classList.remove("hidden");
  if (state.forecastAccuracyChart) state.forecastAccuracyChart.destroy();
  state.forecastAccuracyChart = new Chart(
    el("forecast-accuracy-chart").getContext("2d"),
    {
      type: "bar",
      data: {
        labels: chronological.map((day) =>
          new Date(`${day.date}T12:00:00`).toLocaleDateString("de-DE", {
            day: "2-digit",
            month: "2-digit",
          })
        ),
        datasets: [
          {
            label: "Erwartet",
            data: chronological.map((day) => day.expected_kwh),
            backgroundColor: "rgba(56, 189, 248, 0.55)",
          },
          {
            label: "Tatsächlich",
            data: chronological.map((day) => day.actual_kwh),
            backgroundColor: "rgba(34, 197, 94, 0.65)",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        events: chartEvents(),
        scales: { y: { beginAtZero: true, title: { display: true, text: "kWh" } } },
      },
    }
  );
}

function setupAdminArea() {
  // Eine konsolidierte Admin-Seite (Benutzerverwaltung, Mail-Report,
  // Logdaten-Abgleich). Nur fuer Admins sichtbar; die zugehoerigen Endpunkte
  // sind zusaetzlich serverseitig ueber require_admin abgesichert.
  const overlay = el("admin-area-overlay");

  el("admin-area-btn").addEventListener("click", async () => {
    el("admin-reset-result").textContent = "";
    el("fc-save-result").textContent = "";
    el("dr-save-result").textContent = "";
    el("dr-trigger-result").textContent = "";
    overlay.classList.remove("hidden");
    try {
      await loadAdminUserTable();
    } catch (err) {
      console.error(err);
    }
    try {
      await loadForecastConfigIntoForm();
    } catch (err) {
      console.error(err);
      el("fc-save-result").textContent = "Prognosekonfiguration konnte nicht geladen werden.";
    }
    try {
      await loadDailyReportConfigIntoForm();
      await refreshDailyReportStatusText();
    } catch (err) {
      console.error(err);
    }
    // Import-Status laden + (nur bei laufendem Abgleich) Polling starten -
    // ausschliesslich waehrend das Admin-Overlay offen ist.
    updateImportStatusUI().catch(console.error);
  });

  el("admin-area-close").addEventListener("click", () => {
    overlay.classList.add("hidden");
    stopImportPolling();
  });

  // --- PV-Prognose: Aktivierung und Standort speichern ---
  el("forecast-config-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = el("fc-save-result");
    result.textContent = "Speichert …";
    const payload = {
      enabled: el("fc-enabled").checked,
      latitude: optionalNumber(el("fc-latitude").value),
      longitude: optionalNumber(el("fc-longitude").value),
    };
    try {
      const res = await fetch("/api/admin/forecast/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        result.textContent = await extractErrorMessage(res);
        return;
      }
      result.textContent = "Gespeichert.";
      await loadForecastConfigIntoForm();
      await refreshForecast();
    } catch (err) {
      console.error(err);
      result.textContent = "Verbindung zum Server fehlgeschlagen.";
    }
  });

  // --- Mail-Report: Speichern + Testmail ---
  el("daily-report-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    el("dr-save-result").textContent = "Speichert …";
    const payload = {
      enabled: el("dr-enabled").checked,
      report_time: el("dr-time").value,
      recipients: parseRecipients(el("dr-recipients").value),
      mail_service_url: el("dr-url").value.trim(),
      mail_service_api_key: el("dr-api-key").value || null,
      mail_service_from_name: el("dr-from-name").value.trim(),
    };
    try {
      const res = await fetch("/api/admin/daily-report/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        el("dr-save-result").textContent = await extractErrorMessage(res);
        return;
      }
      el("dr-save-result").textContent = "Gespeichert.";
      await loadDailyReportConfigIntoForm();
      await refreshDailyReportStatusText();
    } catch (err) {
      console.error(err);
      el("dr-save-result").textContent = "Verbindung zum Server fehlgeschlagen.";
    }
  });

  el("dr-trigger-btn").addEventListener("click", async () => {
    const btn = el("dr-trigger-btn");
    btn.disabled = true;
    el("dr-trigger-result").textContent = "Wird gesendet …";
    try {
      const res = await fetch("/api/admin/daily-report/trigger", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        el("dr-trigger-result").textContent = data.detail || "Fehler beim Senden.";
      } else {
        el("dr-trigger-result").textContent = data.message;
      }
      await refreshDailyReportStatusText();
    } catch (err) {
      console.error(err);
      el("dr-trigger-result").textContent = "Verbindung zum Server fehlgeschlagen.";
    } finally {
      btn.disabled = false;
    }
  });

  // --- Logdaten-Abgleich manuell anstossen ---
  el("trigger-import-btn").addEventListener("click", async () => {
    const btn = el("trigger-import-btn");
    btn.disabled = true;
    el("import-status-text").textContent = "Wird gestartet …";
    try {
      const res = await fetch("/api/admin/import-history", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!data.started) {
        el("import-status-text").textContent = data.message || "Läuft bereits – bitte warten.";
      }
    } catch (err) {
      console.error(err);
      el("import-status-text").textContent = "Fehler beim Starten.";
    }
    updateImportStatusUI().catch(console.error);
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
    updatePvYieldVisibility();
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
    refreshPvYieldSummary().catch(console.error);
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
  const pvPureSum = cardSource.reduce((acc, r) => {
    const v = purePvWatt(r);
    return v === null ? acc : acc === null ? v : acc + v;
  }, null);
  el("card-pv").textContent = fmtWatt(pvPureSum);

  // Batterie-Ladezustand (SoC) gibt es nur pro echtem Geraet (der
  // zusammengefasste "_all_"-Eintrag hat keinen eigenen SoC-Wert) - dafuer
  // immer die einzelnen Geraete verwenden, auch in der "Alle"-Ansicht. Die
  // Kachel wird angezeigt, sobald ueberhaupt ein Batteriewert vorliegt -
  // Leistung ODER SoC. Nachts meldet der Wechselrichter zeitweise keinen SoC
  // mehr (SoC = null), waehrend die Batterie durchaus noch Leistung abgibt;
  // frueher verschwand die Kachel dann ganz. Jetzt bleibt sie sichtbar und
  // zeigt den SoC als "-" an, solange nur die Leistung bekannt ist.
  const hasBattery = (r) =>
    (r.battery_soc_percent !== null && r.battery_soc_percent !== undefined) ||
    (r.battery_power_w !== null && r.battery_power_w !== undefined);
  const batteryEntries = relevant.filter(hasBattery);
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
  battery: "#c084fc",
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
  const fieldFor = { home: "home_power_w", feedin: "feed_in_power_w", griddraw: "grid_draw_power_w", pv: "pv_power_w", battery: "battery_power_w" };
  const metricLabel = { home: "Hausverbrauch", feedin: "Einspeisung", griddraw: "Netzbezug", pv: "PV-Leistung", battery: "Batterie" };

  let datasets;
  if (isDayMode) {
    // Feste 00:00-24:00-Achse (wie beim Tagesvergleich): das Diagramm zeigt
    // also immer den ganzen Tag, auch wenn aktuell erst z.B. 14 Uhr ist -
    // der restliche Tag bleibt dann leer, statt dass die Achse "dynamisch"
    // beim jeweils letzten Messwert endet.
    datasets = Object.keys(fieldFor).map((key) => ({
      label: metricLabel[key],
      data: points.map((p) => ({ x: minuteOfLocalDay(new Date(p.timestamp)), y: key === "pv" ? purePvWatt(p) : p[fieldFor[key]] })),
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
      data: points.map((p) => (key === "pv" ? purePvWatt(p) : p[fieldFor[key]])),
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
      events: chartEvents(),
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
    // Farbe an die Aktualitaet koppeln (heute = 0, gestern = 1, ...) statt an
    // die absolute Listenposition. So hat der aktuellste Tag (heute) immer
    // dieselbe Farbe (der erste Palettenton, Blau) und jeder n-t-juengste Tag
    // dieselbe Farbe - unabhaengig davon, ob 3, 7, 14 oder 30 Tage angezeigt
    // werden. Das erleichtert das Vergleichen ueber die Zeitraeume hinweg.
    const colorIndex = total - 1 - i;

    if (metric === "pv") {
      datasets.push({
        label: dateLabel,
        data: day.points.map((p) => ({ x: p.minute, y: p.pv_power_w })),
        borderColor: dayColor(colorIndex),
        backgroundColor: "transparent",
        borderWidth: width,
        tension: 0.25,
        pointRadius: 0,
      });
    } else if (metric === "grid") {
      datasets.push({
        label: dateLabel,
        data: day.points.map((p) => ({ x: p.minute, y: p.grid_draw_power_w })),
        borderColor: dayColor(colorIndex),
        backgroundColor: "transparent",
        borderWidth: width,
        tension: 0.25,
        pointRadius: 0,
      });
    } else {
      // solar_battery: zwei Kurven pro Tag - durchgezogen = Solaranteil,
      // gestrichelt = Batterieanteil, jeweils in der gleichen Tagesfarbe.
      const color = dayColor(colorIndex);
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
        // Vorzeichenbehaftete Batterieleistung wie im Leistungsverlauf:
        // negativ = Laden, positiv = Entladen (statt nur des ins Haus
        // abgegebenen Anteils, der das Laden nicht zeigte).
        data: day.points.map((p) => ({ x: p.minute, y: p.battery_power_w })),
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
      `Farbe) = Batterieleistung (negativ = Laden, positiv = Entladen), wie im ` +
      `Leistungsverlauf. Nur bei live erfassten Daten verfügbar (nicht bei ` +
      `importierten Altdaten ohne Netzmessung) und auf ${SOLAR_BATTERY_MAX_DAYS} ` +
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

  const yLabel = metric === "pv" ? "PV-Leistung" : metric === "grid" ? "Netzbezug" : "Solar / Batterie";

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
      events: chartEvents(),
      // Wie beim Leistungsverlauf: beim Hovern alle Tage an dieser Uhrzeit
      // anzeigen (nicht nur den naechsten Punkt), damit sich die Werte
      // vergleichen lassen.
      interaction: { mode: "index", intersect: false },
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
          itemSort: (a, b) => b.parsed.y - a.parsed.y,
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
      events: chartEvents(),
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
      events: chartEvents(),
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

// PV-Ertrag (kWh) je Zeitraum in der Leiste oben. Hausweite Groesse (Summe
// ueber alle Wechselrichter) und daher nur im Gesamt-Tab ("Alle (Summe)")
// sinnvoll - fuer einen einzelnen Wechselrichter wird die Leiste
// ausgeblendet. Geladen wird beim Start, beim Zurueckwechseln auf den
// Gesamt-Tab und periodisch.
const PV_PERIOD_UNKNOWN = "–";

function updatePvYieldVisibility() {
  const show = state.selectedDeviceId === "";
  const section = el("pv-yield-summary");
  if (section) section.classList.toggle("hidden", !show);
  return show;
}

async function refreshPvYieldSummary() {
  if (!updatePvYieldVisibility()) return; // Einzel-WR: nichts anzeigen/laden
  const data = await fetchJson("/api/readings/pv-yield-summary");
  const byKey = {};
  for (const period of data.periods) byKey[period.key] = period;
  for (const cell of document.querySelectorAll("[data-pvyield]")) {
    const period = byKey[cell.dataset.pvyield];
    cell.textContent = period ? fmtKwh(period.kwh) : PV_PERIOD_UNKNOWN;
    if (period) cell.title = `${period.from_date} – ${period.to_date}`;
  }
}

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

function chartEvents() {
  // Leeres Array = Chart.js reagiert auf keinerlei Zeiger-/Touch-Events
  // (kein Tooltip/Hover). So faengt das Diagramm auf dem Handy die
  // Scroll-Geste nicht ab, solange die Werte-Anzeige aus ist.
  return state.chartsInteractive
    ? ["mousemove", "mouseout", "click", "touchstart", "touchmove"]
    : [];
}


function isTouchDevice() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(hover: none), (pointer: coarse)").matches
  );
}

// Tooltip/Hover (Werte-Anzeige) fuer ALLE Diagramme ein-/ausschalten. Aus =
// keine Events -> das Diagramm faengt die Touch-Geste nicht ab, die Seite
// scrollt frei; an = Werte lassen sich per Tippen/Hovern ablesen.
function setChartsInteractive(on) {
  state.chartsInteractive = on;
  const charts = [
    state.chart,
    state.dayCompare.chart,
    state.dailyTotals.chart,
    state.hourlyCompare.chart,
    state.forecastChart,
    state.forecastAccuracyChart,
  ];
  for (const c of charts) {
    if (!c || !c.options) continue;
    c.options.events = chartEvents();
    if (typeof c.update === "function") c.update();
  }
  for (const btn of document.querySelectorAll(".chart-interaction-toggle")) {
    btn.textContent = on ? "Werte anzeigen: an" : "Werte anzeigen: aus";
    btn.classList.toggle("active", on);
  }
}

function setupTopbarMenu() {
  const toggle = el("menu-toggle");
  const menu = el("topbar-actions");
  if (!toggle || !menu) return;
  const close = () => {
    menu.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  };
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = menu.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  // Auswahl im Menue schliesst es wieder (dann oeffnet sich z.B. der Dialog).
  menu.addEventListener("click", (e) => {
    if (e.target.closest("button")) close();
  });
  // Tippen/Klicken ausserhalb schliesst das Menue.
  document.addEventListener("click", (e) => {
    if (!menu.contains(e.target) && e.target !== toggle) close();
  });
}

function setupChartInteractionToggle() {
  for (const btn of document.querySelectorAll(".chart-interaction-toggle")) {
    btn.addEventListener("click", () => setChartsInteractive(!state.chartsInteractive));
  }
}


// Aktualisierungs-Ring in der Topbar bei jeder Kopf-Aktualisierung neu
// starten (synchron zum Auto-Refresh-Intervall).
function restartRefreshRing() {
  const ring = document.querySelector(".refresh-ring-progress");
  if (!ring) return;
  ring.style.animation = "none";
  void ring.getBoundingClientRect(); // Reflow erzwingen -> Animation startet neu
  ring.style.animation = "";
}

async function init() {
  await checkAuth(); // leitet bei fehlender/ungueltiger Sitzung zu login.html um
  // Auf Touch-Geraeten die Diagramm-Interaktion standardmaessig ausschalten
  // (Scrollen soll Vorrang haben); am Desktop (Maus) an lassen.
  state.chartsInteractive = !isTouchDevice();
  setupChangePassword();
  setupLogout();
  setupTopbarMenu();
  setupAdminArea();
  await loadDevices();
  setupRangeButtons();
  setupDayCompareControls();
  setupDailyTotalsControls();
  setupHourlyCompareControls();
  setupChartInteractionToggle();
  await refreshAll({ showLoading: true });
  refreshForecast().catch(console.error);
  refreshForecastAccuracy().catch(console.error);
  refreshPvYieldSummary().catch(console.error);
  setChartsInteractive(state.chartsInteractive);
  const LIVE_REFRESH_MS = 20000;
  const ringEl = document.querySelector(".refresh-ring-progress");
  if (ringEl) ringEl.style.setProperty("--refresh-secs", LIVE_REFRESH_MS / 1000 + "s");
  setInterval(() => {
    refreshLiveCards().catch(console.error);
    refreshSummaryCards().catch(console.error);
    restartRefreshRing();
  }, LIVE_REFRESH_MS);
  setInterval(() => refreshChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshDayCompareChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshDailyTotalsChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshHourlyCompareChart().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshPvYieldSummary().catch(console.error), 5 * 60 * 1000);
  setInterval(() => refreshForecast().catch(console.error), 60 * 60 * 1000);
  setInterval(() => refreshForecastAccuracy().catch(console.error), 60 * 60 * 1000);
}

init();
