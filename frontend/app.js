const state = {
  currentUser: null, // {id, username, role, must_change_password}, gesetzt in checkAuth()
  devices: [],
  selectedDeviceId: "", // "" bedeutet: alle Geraete summiert
  hours: 24,
  chart: null,
  chartMode: null, // "day" (feste 00:00-24:00 Achse) | "range" (rollierend, Datumslabels)
  dayCompare: {
    metric: "pv", // "pv" | "solar_battery" | "grid"
    // Standard bewusst auf 1 Tag (nicht mehrere ueberlagerte Tage) - passend
    // zu den anderen Zeitraum-Diagrammen (Leistungsverlauf: 24 Std,
    // Wechselrichter-Vergleich: 1 Tag), damit die Seite beim Start ueberall
    // zunaechst nur den aktuellsten Tag zeigt. Weitere Tage bleiben ueber
    // die Buttons oben weiterhin waehlbar.
    days: 1,
    chart: null,
  },
  dailyTotals: {
    days: 30,
    chart: null,
  },
  autarky: {
    // Standard 24 Monate (2 Jahre) - genug, um saisonale Unterschiede
    // (Sommer/Winter) zu erkennen, ohne bei sehr langer Laufzeit die
    // Beschriftung der X-Achse zu ueberladen. Ueber "Alle" weiterhin die
    // komplette Historie waehlbar.
    months: 24,
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
  forecastHoursTodayChart: null,
  forecastYesterdayChart: null,
  // Welche Ansichts-Tabs (siehe setupViewTabs) schon mindestens einmal
  // geladen wurden - nur fuer diese laeuft ein periodisches Auto-Refresh und
  // nur diese werden bei einem Wechselrichter-Wechsel neu geladen. Ein noch
  // nie besuchter Tab laedt seine Daten erst beim ersten Oeffnen.
  tabsLoaded: new Set(),
  // Aktuell gewaehlte Unteransicht je Ansichts-Tab (siehe setupViewTabs) -
  // ersetzt die frueheren separaten <select>-Elemente pro Tab. Die
  // Vorgabewerte entsprechen den ehemals zuerst ausgewaehlten Optionen.
  subView: {
    trend: "power",
    consumption: "dailytotals",
    forecast: "days",
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

// Voller, gut lesbarer Datums-Text ("Donnerstag, 13. August 2026") statt
// des rohen "YYYY-MM-DD"-Strings vom Backend - fuer Statuszeilen, die sich
// auf einen einzelnen, konkreten Tag beziehen (z.B. "Prognose gestern").
function fmtFullDate(dateStr) {
  if (!dateStr) return "";
  return new Date(`${dateStr}T12:00:00`).toLocaleDateString("de-DE", {
    weekday: "long",
    day: "2-digit",
    month: "long",
    year: "numeric",
  });
}

// Zeichnet fuer den "Prognose"-Datensatz (Floating Bar von low_kw bis
// high_kw, siehe refreshForecast()/refreshForecastYesterday()) zusaetzlich
// einen kurzen, deutlich sichtbaren Strich auf Hoehe des Erwartungswerts
// (dataset.expectedData) direkt IN den Balken ein - der Spannbereich allein
// zeigt sonst nicht, wo innerhalb der Spanne der eigentliche Prognosewert
// lag; im Tooltip war das zwar schon zu sehen, aber eben erst beim Hovern.
// Wird ueber die Balken-Geometrie (BarElement: x = Mitte, width = volle
// Breite) gezeichnet, damit der Strich exakt im richtigen Balken sitzt,
// unabhaengig von der Anzahl gleichzeitig gruppierter Balken.
const forecastExpectedMarkerPlugin = {
  id: "forecastExpectedMarker",
  afterDatasetsDraw(chart) {
    const datasetIndex = chart.data.datasets.findIndex(
      (dataset) => dataset.label === "Prognose"
    );
    if (datasetIndex === -1) return;
    const meta = chart.getDatasetMeta(datasetIndex);
    if (meta.hidden) return;
    const dataset = chart.data.datasets[datasetIndex];
    const yScale = chart.scales[meta.yAxisID];
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "#0f172a";
    ctx.lineWidth = 2;
    meta.data.forEach((bar, index) => {
      const expected = dataset.expectedData?.[index];
      if (expected === null || expected === undefined) return;
      if (!bar || !Number.isFinite(bar.x) || !Number.isFinite(bar.width)) return;
      const y = yScale.getPixelForValue(expected);
      const halfWidth = bar.width / 2;
      ctx.beginPath();
      ctx.moveTo(bar.x - halfWidth, y);
      ctx.lineTo(bar.x + halfWidth, y);
      ctx.stroke();
    });
    ctx.restore();
  },
};

async function refreshForecast() {
  return withLoading(["#forecast-section"], async () => {
    // Prognose- und Ist-Werte werden parallel geladen: die Ist-Werte
    // (heutiger Tag, je Wechselrichter) sind die Grundlage fuer den
    // "Prognose vs. echt"-Vergleich der stuendlichen Ansicht weiter unten.
    // Schlaegt der Ist-Werte-Abruf fehl, soll das die Prognose selbst nicht
    // verhindern - dann zeigt die stuendliche Ansicht eben nur die
    // Prognosebalken ohne Vergleich.
    const [data, actualHourly] = await Promise.all([
      fetchJson("/api/forecast"),
      fetchJson("/api/readings/hourly-per-device?metric=pv&days=1").catch(
        () => ({ devices: [], buckets: [] })
      ),
    ]);
    const status = el("forecast-status");
    const dayContainer = el("forecast-days");
    const hoursTodayStatus = el("forecast-hours-today-status");
    dayContainer.innerHTML = "";
    // Vor allen Rueckspruengen leeren: sonst bleiben beim Wechsel auf ein
    // Geraet ohne eigene Prognose die vorherigen Gesamtwerte sichtbar.
    hoursTodayStatus.textContent = "";
    if (!data.available) {
      status.textContent = data.message;
      if (state.forecastChart) {
        state.forecastChart.destroy();
        state.forecastChart = null;
      }
      if (state.forecastHoursTodayChart) {
        state.forecastHoursTodayChart.destroy();
        state.forecastHoursTodayChart = null;
      }
      return;
    }

    // Bei Auswahl eines einzelnen Wechselrichters (siehe device-tabs, wie
    // bei den anderen Ansichts-Tabs) zeigt die Prognose ausschliesslich
    // dessen Anteil statt der ueber alle Geraete summierten Werte - Kacheln,
    // Diagramm und Status-Zeile filtern dafuer konsistent auf dieselbe
    // device_id, die Backend-Antwort liefert die Aufschluesselung dafuer
    // bereits je Tag UND je Stunde (siehe energy_forecast._summarize).
    const deviceId = state.selectedDeviceId;
    const deviceName = deviceId
      ? state.devices.find((d) => d.id === deviceId)?.name || deviceId
      : null;
    const deviceHasForecast =
      !deviceId ||
      data.days.some((day) => day.devices.some((d) => d.device_id === deviceId));

    if (deviceId && !deviceHasForecast) {
      status.textContent =
        `Für ${deviceName} liegt noch keine eigene Prognose vor (zu wenig Historie).`;
      if (state.forecastChart) {
        state.forecastChart.destroy();
        state.forecastChart = null;
      }
      if (state.forecastHoursTodayChart) {
        state.forecastHoursTodayChart.destroy();
        state.forecastHoursTodayChart = null;
      }
      return;
    }

    status.textContent =
      `${data.message} Grundlage: ${data.training_samples} historische Stunden, ` +
      `aktualisiert ${fmtDateTime(data.generated_at)}.`;
    const relevantModels = deviceId
      ? (data.models || []).filter((model) => model.device_id === deviceId)
      : data.models;
    if (relevantModels?.length) {
      const modelText = relevantModels.map((model) => {
        const method = model.method === "learned" ? "gelernt" : "Standard";
        const error = model.validation_error_percent === null
          ? "noch ohne Rückvergleich"
          : `${model.validation_error_percent.toFixed(1)} % historischer Fehler`;
        return `${model.device_name}: ${method}, ${error}`;
      }).join(" · ");
      status.textContent += ` Modelle: ${modelText}.`;
    }
    for (const day of data.days) {
      const dayValues = deviceId ? day.devices.find((d) => d.device_id === deviceId) : day;
      if (!dayValues) continue;
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
      value.textContent = `${dayValues.expected_kwh.toFixed(1)} kWh`;
      const range = document.createElement("span");
      range.className = "muted";
      range.textContent = `Bereich ${dayValues.low_kwh.toFixed(1)}–${dayValues.high_kwh.toFixed(1)} kWh`;
      const windowText = document.createElement("span");
      windowText.className = "muted";
      windowText.textContent = dayValues.production_start
        ? `${fmtForecastTime(dayValues.production_start)}–${fmtForecastTime(dayValues.production_end)}, Spitze ${dayValues.peak_kw.toFixed(1)} kW`
        : "Keine nennenswerte Erzeugung erwartet";
      const devices = document.createElement("span");
      devices.className = "forecast-day-devices";
      // Die Pro-Geraet-Aufschluesselung nur zeigen, wenn NICHT schon auf ein
      // einzelnes Geraet gefiltert ist (sonst waere sie redundant).
      devices.textContent = deviceId
        ? ""
        : day.devices
            .map((device) => `${device.device_name}: ${device.expected_kwh.toFixed(1)} kWh`)
            .join(" · ");
      card.append(date, value, range, windowText, devices);
      dayContainer.appendChild(card);
    }

    // Stuendliche Prognose NUR fuer heute (bewusst kein weiterer Tag - die
    // Stundenwerte fuer 7 Tage sind ohnehin schon im Diagramm unten
    // enthalten, hier soll gezielt "heute im Detail" sichtbar sein), als
    // Balkendiagramm: je Stunde ein Prognose-Balken (mit dem gelernten
    // Spannbereich als Tooltip-Zusatzinfo) neben dem tatsaechlich
    // gemessenen Ertrag - so ist die Abweichung direkt auf einen Blick
    // sichtbar statt in Zahlen-Kacheln.
    // data.days[0] ist per Backend-Konvention immer der heutige lokale Tag
    // (siehe energy_forecast.forecast_weather_for_local_days).
    const todayKey = data.days[0]?.date;
    const todayHours = todayKey
      ? data.hours.filter((hour) => hour.local_date === todayKey)
      : [];

    // Ist-Werte je Stunde aus /api/readings/hourly-per-device (siehe oben)
    // ueber das lokale Stunden-Bucket (hour.local_hour) zuordnen - dasselbe
    // Bucket-Format wie hour.local_date wird server-seitig berechnet, damit
    // die Zuordnung nicht von der Zeitzone des Browsers abhaengt (siehe
    // schemas.ForecastHourOut.local_hour).
    const actualBuckets = new Map(
      (actualHourly.buckets || []).map((bucket) => [bucket.bucket, bucket])
    );
    function actualKwhFor(hour) {
      const bucket = actualBuckets.get(hour.local_hour);
      if (!bucket) return null;
      if (deviceId) {
        const value = bucket.values[deviceId];
        return value === undefined || value === null ? null : value;
      }
      const values = Object.values(bucket.values).filter(
        (value) => value !== null && value !== undefined
      );
      if (values.length === 0) return null;
      return values.reduce((sum, value) => sum + value, 0);
    }
    // Nur bereits vollstaendig vergangene Stunden bekommen einen Ist-Wert -
    // fuer die laufende und kuenftige Stunden gibt es naturgemaess noch
    // keine (vollstaendige) Messung.
    const nowMs = Date.now();
    function isHourElapsed(hour) {
      return new Date(hour.timestamp).getTime() + 60 * 60 * 1000 <= nowMs;
    }

    const hourLabels = todayHours.map((hour) => fmtForecastTime(hour.timestamp));
    const forecastExpected = [];
    const forecastRanges = [];
    const actualValues = [];
    for (const hour of todayHours) {
      const hourValues = deviceId ? hour.devices.find((d) => d.device_id === deviceId) : hour;
      forecastExpected.push(hourValues ? hourValues.expected_kw : null);
      // Floating-Bar-Format ([low, high]) statt eines einzelnen Zahlenwerts:
      // der Prognose-Balken zeigt damit direkt den gelernten Spannbereich als
      // Balkenhoehe, in derselben Spalte und Farbe wie die Prognose selbst -
      // kein separater dritter Balken noetig.
      forecastRanges.push(hourValues ? [hourValues.low_kw, hourValues.high_kw] : null);
      actualValues.push(isHourElapsed(hour) ? actualKwhFor(hour) : null);
    }

    hoursTodayStatus.textContent = deviceId
      ? `Prognose (mit Spannbereich) vs. tatsächlicher Ertrag von ${deviceName}, je Stunde. Für die laufende und künftige Stunden liegt noch kein Ist-Wert vor.`
      : "Prognose (mit Spannbereich) vs. tatsächlicher Ertrag (alle Wechselrichter), je Stunde. Für die laufende und künftige Stunden liegt noch kein Ist-Wert vor.";

    const hoursTodayDatasets = [
      {
        label: "Prognose",
        data: forecastRanges,
        backgroundColor: "#38bdf8",
        borderColor: "#38bdf8",
        borderWidth: 1,
        borderRadius: 3,
        // Nur fuer den Tooltip mitgefuehrt (kein eigenes Chart.js-Feld) -
        // die Balkenhoehe selbst ist bereits der Spannbereich (data oben),
        // der Erwartungswert wird zusaetzlich im Tooltip genannt.
        expectedData: forecastExpected,
      },
      {
        label: "Tatsächlich",
        data: actualValues,
        backgroundColor: "#facc15",
        borderColor: "#facc15",
        borderWidth: 1,
        borderRadius: 3,
      },
    ];

    if (state.forecastHoursTodayChart) {
      state.forecastHoursTodayChart.data.labels = hourLabels;
      state.forecastHoursTodayChart.data.datasets = hoursTodayDatasets;
      state.forecastHoursTodayChart.update();
    } else {
      state.forecastHoursTodayChart = new Chart(
        el("forecast-hours-today-chart").getContext("2d"),
        {
          type: "bar",
          data: { labels: hourLabels, datasets: hoursTodayDatasets },
          plugins: [forecastExpectedMarkerPlugin],
          options: {
            responsive: true,
            maintainAspectRatio: false,
            events: chartEvents(),
            interaction: { mode: "index", intersect: false },
            scales: {
              x: { ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 24 } },
              y: { beginAtZero: true, title: { display: true, text: "kWh" } },
            },
            plugins: {
              tooltip: {
                callbacks: {
                  label(context) {
                    if (context.dataset.label === "Prognose") {
                      const range = context.raw;
                      const expected = context.dataset.expectedData?.[context.dataIndex];
                      if (!range || expected === null || expected === undefined) {
                        return "Prognose: –";
                      }
                      return (
                        `Prognose: ${expected.toFixed(1)} kWh ` +
                        `(Spannbereich ${range[0].toFixed(1)}–${range[1].toFixed(1)} kWh)`
                      );
                    }
                    const value = context.parsed.y;
                    if (value === null || value === undefined) {
                      return `${context.dataset.label}: –`;
                    }
                    return `${context.dataset.label}: ${value.toFixed(1)} kWh`;
                  },
                },
              },
            },
          },
        }
      );
    }

    const labels = data.hours.map((hour) =>
      new Date(hour.timestamp).toLocaleString("de-DE", {
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    );
    function hourValue(hour, field) {
      if (!deviceId) return hour[field];
      const deviceHour = hour.devices.find((d) => d.device_id === deviceId);
      return deviceHour ? deviceHour[field] : 0;
    }
    const datasets = [
      {
        label: "Unterer Bereich",
        data: data.hours.map((hour) => hourValue(hour, "low_kw")),
        borderColor: "rgba(59, 130, 246, 0)",
        pointRadius: 0,
        fill: false,
      },
      {
        label: "Prognosebereich",
        data: data.hours.map((hour) => hourValue(hour, "high_kw")),
        borderColor: "rgba(59, 130, 246, 0)",
        backgroundColor: "rgba(59, 130, 246, 0.16)",
        pointRadius: 0,
        fill: "-1",
      },
      {
        label: "Erwartete PV-Leistung",
        data: data.hours.map((hour) => hourValue(hour, "expected_kw")),
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

  });
}

// --- "Gestern" (Prognose-Tab): dieselbe stuendliche Balken-Darstellung wie
// "Stuendliche Prognose heute" (Floating-Bar Prognose + Balken Ist-Wert),
// aber fuer den komplett abgeschlossenen Vortag - dort hat inzwischen jede
// Stunde einen Ist-Wert. Anders als bei "heute" liefert das Backend
// (/api/forecast/yesterday, aus den gespeicherten ForecastPrediction-Zeilen
// statt der Wettervorhersage) Prognose UND Ist-Wert bereits gemeinsam je
// Stunde - kein zweiter Fetch/Abgleich noetig wie bei refreshForecast(). ---
async function refreshForecastYesterday() {
  return withLoading(["#forecast-yesterday-chart-wrapper"], async () => {
    const data = await fetchJson("/api/forecast/yesterday");
    const status = el("forecast-yesterday-status");
    const deviceId = state.selectedDeviceId;
    const deviceName = deviceId
      ? state.devices.find((d) => d.id === deviceId)?.name || deviceId
      : null;

    if (!data.available) {
      status.textContent = data.message;
      if (state.forecastYesterdayChart) {
        state.forecastYesterdayChart.destroy();
        state.forecastYesterdayChart = null;
      }
      return;
    }

    // Volles Datum ("Donnerstag, 13. August 2026") statt des rohen
    // "YYYY-MM-DD"-Strings, damit klar erkennbar ist, welcher Tag gemeint
    // ist, ohne das ISO-Format erst gedanklich uebersetzen zu muessen.
    const fullDate = fmtFullDate(data.date);

    const deviceHasData =
      !deviceId ||
      data.hours.some((hour) => hour.devices.some((d) => d.device_id === deviceId));
    if (deviceId && !deviceHasData) {
      status.textContent = `Für ${deviceName} liegen für gestern (${fullDate}) keine gespeicherten Prognosen vor.`;
      if (state.forecastYesterdayChart) {
        state.forecastYesterdayChart.destroy();
        state.forecastYesterdayChart = null;
      }
      return;
    }

    status.textContent = deviceId
      ? `Stündlicher Vergleich für ${deviceName}, gestern, ${fullDate}.`
      : `${data.message} Gestern war ${fullDate}.`;

    const hourLabels = data.hours.map((hour) => fmtForecastTime(hour.timestamp));
    const forecastExpected = [];
    const forecastRanges = [];
    const actualValues = [];
    for (const hour of data.hours) {
      const hourValues = deviceId ? hour.devices.find((d) => d.device_id === deviceId) : hour;
      forecastExpected.push(hourValues ? hourValues.expected_kw : null);
      forecastRanges.push(hourValues ? [hourValues.low_kw, hourValues.high_kw] : null);
      actualValues.push(hourValues ? hourValues.actual_kw : null);
    }

    const datasets = [
      {
        label: "Prognose",
        data: forecastRanges,
        backgroundColor: "#38bdf8",
        borderColor: "#38bdf8",
        borderWidth: 1,
        borderRadius: 3,
        // Nur fuer den Tooltip mitgefuehrt - die Balkenhoehe selbst ist
        // bereits der Spannbereich (data oben), siehe refreshForecast().
        expectedData: forecastExpected,
      },
      {
        label: "Tatsächlich",
        data: actualValues,
        backgroundColor: "#facc15",
        borderColor: "#facc15",
        borderWidth: 1,
        borderRadius: 3,
      },
    ];

    if (state.forecastYesterdayChart) {
      state.forecastYesterdayChart.data.labels = hourLabels;
      state.forecastYesterdayChart.data.datasets = datasets;
      state.forecastYesterdayChart.update();
    } else {
      state.forecastYesterdayChart = new Chart(
        el("forecast-yesterday-chart").getContext("2d"),
        {
          type: "bar",
          data: { labels: hourLabels, datasets },
          plugins: [forecastExpectedMarkerPlugin],
          options: {
            responsive: true,
            maintainAspectRatio: false,
            events: chartEvents(),
            interaction: { mode: "index", intersect: false },
            scales: {
              x: { ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 24 } },
              y: { beginAtZero: true, title: { display: true, text: "kWh" } },
            },
            plugins: {
              tooltip: {
                callbacks: {
                  label(context) {
                    if (context.dataset.label === "Prognose") {
                      const range = context.raw;
                      const expected = context.dataset.expectedData?.[context.dataIndex];
                      if (!range || expected === null || expected === undefined) {
                        return "Prognose: –";
                      }
                      return (
                        `Prognose: ${expected.toFixed(1)} kWh ` +
                        `(Spannbereich ${range[0].toFixed(1)}–${range[1].toFixed(1)} kWh)`
                      );
                    }
                    const value = context.parsed.y;
                    if (value === null || value === undefined) {
                      return `${context.dataset.label}: –`;
                    }
                    return `${context.dataset.label}: ${value.toFixed(1)} kWh`;
                  },
                },
              },
            },
          },
        }
      );
    }
  });
}

async function refreshForecastAccuracy() {
  return withLoading(["#forecast-accuracy-section"], async () => {
    const data = await fetchJson("/api/forecast/accuracy?days=30");
    const status = el("forecast-accuracy-status");
    const container = el("forecast-accuracy-days");
    const todayContainer = el("forecast-accuracy-today");
    const chartWrapper = el("forecast-accuracy-chart").parentElement;
    container.innerHTML = "";
    todayContainer.innerHTML = "";
    todayContainer.classList.add("hidden");
    if (!data.available) {
      status.textContent = data.message;
      chartWrapper.classList.add("hidden");
      if (state.forecastAccuracyChart) {
        state.forecastAccuracyChart.destroy();
        state.forecastAccuracyChart = null;
      }
      return;
    }

    // Wie bei refreshForecast(): bei Auswahl eines einzelnen
    // Wechselrichters nur dessen Vergleichswerte zeigen. Die
    // Gesamtgenauigkeit (data.overall_accuracy_percent) ist eine
    // hausweite Kennzahl ueber alle Geraete und wird deshalb nur in der
    // "Alle"-Ansicht angezeigt, nicht umgerechnet.
    const deviceId = state.selectedDeviceId;
    const deviceName = deviceId
      ? state.devices.find((d) => d.id === deviceId)?.name || deviceId
      : null;

    // "Heute (bisher)" separat VOR den abgeschlossenen Tagen anzeigen -
    // unabhaengig davon, ob unten ueberhaupt schon abgeschlossene Tage fuer
    // das gewaehlte Geraet vorliegen (deshalb hier und nicht erst nach dem
    // cardEntries-Fruehausstieg weiter unten). Die Abweichung kann hier
    // groesser wirken als bei den abgeschlossenen Tagen, weil sich gute und
    // schlechte Stunden noch nicht ueber einen ganzen Tag ausgleichen
    // konnten (siehe forecast_evaluation.get_forecast_accuracy).
    const todayValues = data.today_so_far
      ? deviceId
        ? data.today_so_far.devices.find((d) => d.device_id === deviceId)
        : data.today_so_far
      : null;
    if (todayValues) {
      const card = document.createElement("div");
      card.className = "forecast-accuracy-day";
      const title = document.createElement("strong");
      title.textContent = "Heute (bisher)";
      const values = document.createElement("span");
      values.className = "forecast-accuracy-values";
      values.textContent = `Erwartet ${todayValues.expected_kwh.toFixed(1)} · tatsächlich ${todayValues.actual_kwh.toFixed(1)} kWh`;
      const difference = document.createElement("span");
      difference.className = "muted";
      const sign = todayValues.difference_kwh > 0 ? "+" : "";
      const accuracy = todayValues.accuracy_percent === null
        ? "Genauigkeit –"
        : `Genauigkeit ${todayValues.accuracy_percent.toFixed(1)} %`;
      difference.textContent =
        `Abweichung ${sign}${todayValues.difference_kwh.toFixed(1)} kWh · ${accuracy} · ` +
        `${todayValues.matched_hours} Stundenwerte bisher`;
      const note = document.createElement("span");
      note.className = "forecast-accuracy-today-note";
      note.textContent =
        "Noch kein abgeschlossener Tag – die Abweichung kann hier größer wirken als bei den " +
        "Tagen unten, weil sich gute und schlechte Stunden noch nicht über einen ganzen Tag " +
        "ausgleichen konnten.";
      card.append(title, values, difference, note);
      todayContainer.appendChild(card);
      todayContainer.classList.remove("hidden");
    }

    const cardEntries = [];
    for (const day of data.days) {
      const values = deviceId ? day.devices.find((d) => d.device_id === deviceId) : day;
      if (!values) continue;
      cardEntries.push({ date: day.date, values, originalDay: day });
      if (cardEntries.length >= 7) break;
    }

    if (deviceId && cardEntries.length === 0) {
      status.textContent =
        `Für ${deviceName} liegen noch keine abgeschlossenen Prognosevergleiche vor.`;
      chartWrapper.classList.add("hidden");
      if (state.forecastAccuracyChart) {
        state.forecastAccuracyChart.destroy();
        state.forecastAccuracyChart = null;
      }
      return;
    }

    status.textContent = deviceId || data.overall_accuracy_percent === null
      ? data.message
      : `${data.message} Gesamtgenauigkeit: ${data.overall_accuracy_percent.toFixed(1)} %.`;
    for (const entry of cardEntries) {
      const day = entry.values;
      const card = document.createElement("div");
      card.className = "forecast-accuracy-day";
      const date = document.createElement("strong");
      date.textContent = new Date(`${entry.date}T12:00:00`).toLocaleDateString("de-DE", {
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
      devices.textContent = deviceId
        ? ""
        : entry.originalDay.devices.map((device) => {
            const deviceSign = device.difference_kwh > 0 ? "+" : "";
            return `${device.device_name}: ${device.expected_kwh.toFixed(1)} → ${device.actual_kwh.toFixed(1)} kWh (${deviceSign}${device.difference_kwh.toFixed(1)})`;
          }).join(" · ");
      card.append(date, values, difference, devices);
      container.appendChild(card);
    }

    // cardEntries liegt bereits absteigend nach Datum vor (siehe
    // forecast_evaluation.get_forecast_accuracy: neuester abgeschlossener
    // Tag zuerst) - fuers Diagramm bewusst NICHT umgedreht, damit der
    // aktuellste Tag ganz links steht statt ganz rechts.
    const chronological = cardEntries;
    chartWrapper.classList.remove("hidden");
    if (state.forecastAccuracyChart) state.forecastAccuracyChart.destroy();
    state.forecastAccuracyChart = new Chart(
      el("forecast-accuracy-chart").getContext("2d"),
      {
        type: "bar",
        data: {
          labels: chronological.map((entry) =>
            new Date(`${entry.date}T12:00:00`).toLocaleDateString("de-DE", {
              day: "2-digit",
              month: "2-digit",
            })
          ),
          datasets: [
            {
              label: "Erwartet",
              data: chronological.map((entry) => entry.values.expected_kwh),
              backgroundColor: "rgba(56, 189, 248, 0.55)",
            },
            {
              label: "Tatsächlich",
              data: chronological.map((entry) => entry.values.actual_kwh),
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

  });
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
    // Nur bereits besuchte Ansichts-Tabs neu laden (siehe setupViewTabs) -
    // ein noch nie geoeffneter Tab laedt beim ersten Oeffnen sowieso frisch.
    refreshLoadedTabs().catch(console.error);
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
  return withLoading(["#live-cards"], async () => {
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

  });
}

async function refreshSummaryCards() {
  return withLoading(["#summary-cards"], async () => {
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

  });
}

// Autarkiegrad heute ist hausweit und deutlich teurer zu berechnen als die
// vorhandenen Tageszaehler: das Backend integriert dafuer die heutigen
// Rohmessungen. Deshalb getrennt von refreshSummaryCards(), das alle 20
// Sekunden laeuft. So genuegt fuer diese langsam veraenderliche Kennzahl der
// 5-Minuten-Takt und ein Fehler des Zusatzendpunkts blockiert nicht die drei
// bewaehrten Tageskacheln.
async function refreshAutarkyToday() {
  return withLoading(["#summary-autarky-card"], async () => {
    const breakdown = await fetchJson("/api/readings/daily-home-breakdown?days=1");
    const today = breakdown.days[breakdown.days.length - 1];
    el("summary-autarky").textContent = today ? fmtPercent(today.autarky_percent) : "–";
  });
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
  return withLoading(["#power-chart-wrapper"], async () => {
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
  return withLoading(["#daycompare-chart-wrapper"], async () => {
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

  });
}

// Metrik (PV-Ertrag/Verbrauch aus Batterie & Solar/Verbrauch aus Netz) wird
// seit dem Ansichts-Flyout (siehe setupTrendSubView) nicht mehr ueber
// eigene Buttons gewaehlt, sondern kommt direkt aus state.dayCompare.metric -
// applySolarBatteryDayLimit() bleibt trotzdem eine eigene Funktion, weil sie
// sowohl beim Metrik- als auch beim Zeitraum-Wechsel gebraucht wird.
function applySolarBatteryDayLimit() {
  const dayContainer = el("daycompare-day-buttons");
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

function setupDayCompareControls() {
  const dayContainer = el("daycompare-day-buttons");

  dayContainer.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-days]");
    if (!btn || btn.disabled) return;
    for (const b of dayContainer.querySelectorAll("button")) b.classList.remove("active");
    btn.classList.add("active");
    state.dayCompare.days = Number(btn.dataset.days);
    refreshDayCompareChart().catch(console.error);
  });
}

// --- Ansichts-Auswahl "Verlauf"-Tab: Leistungsverlauf ODER Tagesvergleich
// (mit einer der drei Metriken) - es ist immer nur EIN Diagramm sichtbar,
// gesteuert ueber das Hover-Flyout-Menue am "Verlauf"-Reiter oben (siehe
// setupViewTabs/SUBVIEW_SETTERS) statt eines eigenen Dropdowns im
// Content-Bereich. ---

function applyTrendSubView(view) {
  const isPower = view === "power";
  el("trend-view-power").classList.toggle("hidden", !isPower);
  el("trend-view-daycompare").classList.toggle("hidden", isPower);
}

function setTrendSubView(view) {
  state.subView.trend = view;
  applyTrendSubView(view);
  if (view === "power") {
    state.chart?.resize();
    refreshChart().catch(console.error);
    return;
  }
  state.dayCompare.metric = view; // "pv" | "solar_battery" | "grid"
  updateDayCompareHint(state.dayCompare.metric);
  applySolarBatteryDayLimit();
  // Bei Metrikwechsel muss der Chart neu aufgebaut werden (Achsentitel,
  // Anzahl Datasets pro Tag aendert sich zwischen 1 und 2) - das passiert
  // erst NACHDEM der Abschnitt sichtbar ist, damit Chart.js die richtige
  // Groesse ermitteln kann (ein waehrend "display:none" aufgebautes
  // Diagramm wuerde mit 0x0 Pixeln berechnet).
  if (state.dayCompare.chart) {
    state.dayCompare.chart.destroy();
    state.dayCompare.chart = null;
  }
  refreshDayCompareChart().catch(console.error);
}

function setupTrendSubView() {
  // Beim Einrichten nur die Sichtbarkeit anwenden (kein Fetch) - das
  // eigentliche Laden uebernimmt refreshTrendTab() beim ersten Oeffnen des
  // Tabs (siehe setupViewTabs/TAB_LOADERS), damit noch nie besuchte Tabs
  // weiterhin erst bei Bedarf laden.
  applyTrendSubView(state.subView.trend);
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
  return withLoading(["#dailytotals-chart-wrapper"], async () => {
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

// --- Autarkiegrad je Monat: Balkendiagramm, wie hoch der Anteil des
// Hausverbrauchs war, der aus PV/Speicher statt aus dem Netz gedeckt wurde
// (siehe daily_summary.build_autarky_monthly_summary). Hausweite Groesse -
// unabhaengig vom oben gewaehlten Wechselrichter-Tab, wie beim
// Tagesverbrauch-Diagramm. ---

const AUTARKY_COLOR = "#2dd4bf";
const MONTH_NAMES_SHORT = [
  "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
  "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
];

function monthLabel(monthStr) {
  // "2026-01" -> "Jan 2026"
  const [year, month] = monthStr.split("-");
  return `${MONTH_NAMES_SHORT[Number(month) - 1]} ${year}`;
}

let autarkyMonthsData = [];

// Anders als bei den uebrigen Diagrammen (feste 0-100%- bzw. 0-Start-Achse)
// wird die Y-Achse hier bewusst dynamisch aus den tatsaechlichen Werten
// berechnet: eine feste 0-100%-Skala wuerde Unterschiede zwischen Monaten
// bei ueblicherweise recht aehnlichen Autarkiegraden (z.B. 40-60%) kaum
// sichtbar machen (fast eine gerade Linie am oberen Rand). Die 0 muss dafuer
// nicht zwingend auf der Achse auftauchen. Nur fuer die Autarkie-Ansicht -
// alle anderen Diagramme behalten ihre bisherige Achsenskalierung.
function autarkyYRange(months) {
  const values = months
    .map((m) => m.autarky_percent)
    .filter((v) => v !== null && v !== undefined);
  if (values.length === 0) return { min: 0, max: 100 };
  const dataMin = Math.min(...values);
  const dataMax = Math.max(...values);
  const spread = dataMax - dataMin;
  // Marge von 10 % der Spannweite, mindestens aber 2 Prozentpunkte (sonst
  // waere bei nahezu identischen Monatswerten die Marge selbst nahe 0 und
  // die Linie liefe wieder flach am Rand).
  const margin = Math.max(spread * 0.1, 2);
  return {
    min: Math.max(0, Math.floor(dataMin - margin)),
    max: Math.min(100, Math.ceil(dataMax + margin)),
  };
}

async function refreshAutarkyChart() {
  return withLoading(["#autarky-chart-wrapper"], async () => {
    const reqMonths = state.autarky.months;
    const params = new URLSearchParams();
    if (reqMonths) params.set("months", String(reqMonths));
    const result = await fetchJson(`/api/readings/autarky-monthly?${params.toString()}`);
    if (state.autarky.months !== reqMonths) return;
    autarkyMonthsData = result.months;

    const labels = autarkyMonthsData.map((m) => monthLabel(m.month));
    const data = autarkyMonthsData.map((m) => m.autarky_percent);
    const yRange = autarkyYRange(autarkyMonthsData);

    if (state.autarky.chart) {
      state.autarky.chart.data.labels = labels;
      state.autarky.chart.data.datasets[0].data = data;
      state.autarky.chart.options.scales.y.min = yRange.min;
      state.autarky.chart.options.scales.y.max = yRange.max;
      state.autarky.chart.update();
      return;
    }

    const ctx = el("autarky-chart").getContext("2d");
    state.autarky.chart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Autarkiegrad",
            data,
            borderColor: AUTARKY_COLOR,
            pointBackgroundColor: AUTARKY_COLOR,
            pointRadius: 4,
            pointHoverRadius: 6,
            borderWidth: 2,
            tension: 0,
            // Nur die Linie selbst, keine Flaeche darunter (fill: false) -
            // bei einer dynamisch skalierten Y-Achse (siehe autarkyYRange)
            // wuerde eine gefuellte Flaeche bis zum unteren Achsenrand
            // sonst leicht den Eindruck erwecken, die Flaeche haette eine
            // inhaltliche Bedeutung (z.B. eine Menge), was sie nicht hat.
            fill: false,
            spanGaps: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        events: chartEvents(),
        scales: {
          x: {
            ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 24 },
            grid: { display: false },
          },
          y: {
            min: yRange.min,
            max: yRange.max,
            ticks: { color: "#94a3b8", callback: (v) => `${v} %` },
            grid: { color: "#334155" },
            title: { display: true, text: "Autarkiegrad (%)", color: "#94a3b8" },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (item) =>
                item.parsed.y === null
                  ? "keine Daten"
                  : `Autarkiegrad: ${item.parsed.y.toFixed(1)} %`,
              afterLabel: (item) => {
                const m = autarkyMonthsData[item.dataIndex];
                if (!m) return "";
                const ownKwh = (m.pv_kwh + m.battery_kwh).toFixed(1);
                return `PV + Speicher: ${ownKwh} kWh · Netz: ${m.grid_kwh.toFixed(1)} kWh`;
              },
            },
          },
        },
      },
    });

  });
}

function setupAutarkyControls() {
  const container = el("autarky-month-buttons");
  container.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-months]");
    if (!btn) return;
    for (const b of container.querySelectorAll("button")) b.classList.remove("active");
    btn.classList.add("active");
    state.autarky.months = btn.dataset.months === "all" ? null : Number(btn.dataset.months);
    refreshAutarkyChart().catch(console.error);
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
  // vergleichen. Zusaetzlich muss oben im "Verbrauch & Wechselrichter"-Tab
  // die Ansicht "Wechselrichter-Vergleich" ausgewaehlt sein (siehe
  // setTrendSubView/state.subView.consumption) - es ist immer nur eine der
  // beiden Ansichten gleichzeitig sichtbar.
  const menuWantsHourly = state.subView.consumption === "hourly";
  const deviceOk = state.selectedDeviceId === "" && state.devices.length > 1;
  el("hourly-section").classList.toggle("hidden", !menuWantsHourly);
  el("hourly-chart-content").classList.toggle("hidden", !deviceOk);
  el("hourly-chart-unavailable").classList.toggle("hidden", deviceOk);
  return menuWantsHourly && deviceOk;
}

// --- Ansichts-Auswahl "Verbrauch & Wechselrichter"-Tab: Tagesverbrauch ODER
// Wechselrichter-Vergleich - wie beim Verlauf-Tab immer nur ein Diagramm
// gleichzeitig sichtbar, gesteuert ueber das Hover-Flyout-Menue am
// "Verbrauch & Wechselrichter"-Reiter oben. ---

function applyConsumptionSubView() {
  el("consumption-view-dailytotals").classList.toggle(
    "hidden",
    state.subView.consumption !== "dailytotals"
  );
  // #hourly-section haengt zusaetzlich vom gewaehlten Wechselrichter-Tab
  // ab - updateHourlyCompareVisibility() wertet beides aus.
  updateHourlyCompareVisibility();
}

function setConsumptionSubView(view) {
  state.subView.consumption = view;
  applyConsumptionSubView();
  // Beim erstmaligen Wechsel auf "Wechselrichter-Vergleich" wurde der Chart
  // evtl. noch nie aufgebaut (siehe refreshHourlyCompareChart()'s fruehen
  // Abbruch, wenn die Ansicht nicht gewaehlt war) - jetzt gezielt nachladen.
  refreshHourlyCompareChart().catch(console.error);
}

function setupConsumptionSubView() {
  // Beim Einrichten nur die Sichtbarkeit anwenden (kein Fetch) - das
  // eigentliche Laden uebernimmt refreshConsumptionTab() beim ersten
  // Oeffnen des Tabs.
  applyConsumptionSubView();
}

async function refreshHourlyCompareChart() {
  return withLoading(["#hourly-chart-wrapper"], async () => {
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


// Ladeindikator pro Panel statt global: jede refresh*()-Funktion dimmt nur
// ihr EIGENES Panel ab und zeigt dort den Spinner, bis ihre eigene Antwort
// da ist - fertige Panels bleiben also sofort sichtbar, auch waehrend
// andere (z.B. die Prognose mit Wetter-Abruf) noch laden. Ein Zaehler pro
// Element statt einem einfachen An/Aus erlaubt ueberlappende Aufrufe
// desselben Panels (z.B. schneller Wechselrichter-Wechsel), ohne dass ein
// frueher fertiger Aufruf den Spinner fuer einen noch laufenden ausblendet.
const loadingCounts = new WeakMap();

function beginLoading(nodes) {
  for (const node of nodes) {
    loadingCounts.set(node, (loadingCounts.get(node) || 0) + 1);
    node.classList.add("is-loading");
  }
}

function endLoading(nodes) {
  for (const node of nodes) {
    const count = (loadingCounts.get(node) || 1) - 1;
    if (count <= 0) {
      loadingCounts.delete(node);
      node.classList.remove("is-loading");
    } else {
      loadingCounts.set(node, count);
    }
  }
}

async function withLoading(selectors, fn) {
  const nodes = selectors.flatMap((sel) => [...document.querySelectorAll(sel)]);
  beginLoading(nodes);
  try {
    return await fn();
  } finally {
    endLoading(nodes);
  }
}

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
  return withLoading(["#pv-yield-summary"], async () => {
    if (!updatePvYieldVisibility()) return; // Einzel-WR: nichts anzeigen/laden
    const data = await fetchJson("/api/readings/pv-yield-summary");
    const byKey = {};
    for (const period of data.periods) byKey[period.key] = period;
    for (const cell of document.querySelectorAll("[data-pvyield]")) {
      const period = byKey[cell.dataset.pvyield];
      cell.textContent = period ? fmtKwh(period.kwh) : PV_PERIOD_UNKNOWN;
      if (period) cell.title = `${period.from_date} – ${period.to_date}`;
    }

  });
}

// Ansichts-Tabs (siehe setupViewTabs): jeder Tab hat einen Loader, der die
// zugehoerigen Panels laedt. "overview" wird beim Start immer sofort
// geladen (siehe init()); die anderen erst beim ersten Oeffnen - deshalb
// bekommt der Nutzer die schnellen, immer verfuegbaren Werte zuerst und
// alles andere erst, wenn er es tatsaechlich ansieht.
function refreshOverview() {
  return Promise.allSettled([
    refreshLiveCards(),
    refreshSummaryCards(),
    refreshAutarkyToday(),
    refreshPvYieldSummary(),
  ]);
}

function refreshTrendTab() {
  return Promise.allSettled([refreshChart(), refreshDayCompareChart()]);
}

function refreshConsumptionTab() {
  return Promise.allSettled([refreshDailyTotalsChart(), refreshHourlyCompareChart()]);
}

function refreshForecastTab() {
  return Promise.allSettled([
    refreshForecast(),
    refreshForecastYesterday(),
    refreshForecastAccuracy(),
  ]);
}

// --- Ansichts-Auswahl "Prognose"-Tab: Tagesuebersicht, stuendliche
// Prognose heute, Wochenverlauf-Diagramm oder Prognosekontrolle - wie bei
// Verlauf/Verbrauch immer nur eine Ansicht gleichzeitig sichtbar, gesteuert
// ueber das Hover-Flyout-Menue am "Prognose"-Reiter oben. Die Kopfzeile
// (Titel + Status/Modelle) bleibt bewusst immer sichtbar, da sie sich auf
// die Prognose insgesamt bezieht, nicht nur auf eine der Unteransichten. ---

const FORECAST_SUBVIEW_SECTIONS = {
  days: () => el("forecast-days"),
  "hours-today": () => el("forecast-view-hours-today"),
  yesterday: () => el("forecast-view-yesterday"),
  "week-chart": () => el("forecast-view-week-chart"),
  accuracy: () => el("forecast-accuracy-section"),
};

function applyForecastSubView(view) {
  for (const [key, getSection] of Object.entries(FORECAST_SUBVIEW_SECTIONS)) {
    getSection().classList.toggle("hidden", key !== view);
  }
}

function setForecastSubView(view) {
  state.subView.forecast = view;
  applyForecastSubView(view);
  // Defensiv: forecast-chart/forecast-accuracy-chart/forecast-hours-today-
  // chart werden im Hintergrund (Tab-Intervall) auch aktualisiert, waehrend
  // ihre Ansicht gerade nicht ausgewaehlt ist (also "display:none") - ein
  // erneutes resize() beim Sichtbarwerden stellt sicher, dass Chart.js die
  // richtige Groesse verwendet, statt sich auf 0x0 zu verlassen.
  state.forecastChart?.resize();
  state.forecastAccuracyChart?.resize();
  state.forecastHoursTodayChart?.resize();
  state.forecastYesterdayChart?.resize();
}

function setupForecastSubView() {
  // Beim Einrichten nur die Sichtbarkeit anwenden (kein Fetch) - das
  // eigentliche Laden uebernimmt refreshForecastTab() beim ersten Oeffnen
  // des Tabs.
  applyForecastSubView(state.subView.forecast);
}

const TAB_LOADERS = {
  overview: refreshOverview,
  trend: refreshTrendTab,
  consumption: refreshConsumptionTab,
  autarky: refreshAutarkyChart,
  forecast: refreshForecastTab,
};

// Autarkiegrad aendert sich nur langsam (Kalendertage/-monate lassen sich
// ohnehin erst nach ihrem Abschluss endgueltig auswerten, siehe
// daily_summary.build_autarky_monthly_summary) - taegliches statt
// 5-minuetiges Neuladen reicht daher aus und erspart dem Backend
// unnoetig haeufige Rohdaten-Scans fuer den laufenden Tag.
const AUTARKY_REFRESH_MS = 24 * 60 * 60 * 1000;

// Periodisches Auto-Refresh je Tab, erst gestartet, sobald der Tab zum
// ersten Mal geoeffnet wurde (siehe setupViewTabs). "overview" hat ein
// eigenes, schnelleres Intervall (siehe init()) und steht daher nicht hier.
const TAB_INTERVALS_MS = {
  trend: 5 * 60 * 1000,
  consumption: 5 * 60 * 1000,
  autarky: AUTARKY_REFRESH_MS,
  // An das Backend-Cache-TTL angeglichen (siehe energy_forecast.CACHE_TTL
  // = 30 Minuten) - eine neue Prognose auf dem Server soll ohne
  // Seiten-Reload zuverlaessig binnen derselben Zeitspanne im Frontend
  // ankommen, statt bis zu eine volle Stunde (vorheriger Wert) zu warten.
  forecast: 30 * 60 * 1000,
};

function startTabInterval(tabId) {
  const ms = TAB_INTERVALS_MS[tabId];
  if (!ms) return;
  setInterval(() => TAB_LOADERS[tabId]().catch(console.error), ms);
}

// Bei Wechselrichter-Wechsel nur die Tabs neu laden, die schon mindestens
// einmal besucht wurden - ein noch nie geoeffneter Tab laedt beim ersten
// Oeffnen sowieso mit der aktuellen Auswahl.
function refreshLoadedTabs() {
  const tasks = Object.entries(TAB_LOADERS)
    .filter(([id]) => state.tabsLoaded.has(id))
    .map(([, loader]) => loader());
  return Promise.allSettled(tasks);
}

// Alle Chart.js-Instanzen je Tab (unabhaengig von der gerade gewaehlten
// Unteransicht - siehe FORECAST_SUBVIEW_SECTIONS: pro Tab-Intervall werden
// ohnehin immer ALLE Unteransichten aktualisiert, auch die gerade nicht
// sichtbaren). Wird gebraucht, damit ein Diagramm, das im Hintergrund
// erzeugt wurde, waehrend sein Tab-Panel noch "display:none" war (siehe
// init(): alle Tabs laden inzwischen schon beim Start), beim tatsaechlichen
// Sichtbarwerden per resize() auf die richtige Groesse kommt - ohne dieses
// Nachziehen wuerde Chart.js dauerhaft bei der 0x0-Groesse vom
// Erzeugungszeitpunkt bleiben.
const TAB_CHARTS = {
  overview: () => [],
  trend: () => [state.chart, state.dayCompare.chart],
  consumption: () => [state.dailyTotals.chart, state.hourlyCompare.chart],
  autarky: () => [state.autarky.chart],
  forecast: () => [
    state.forecastChart,
    state.forecastHoursTodayChart,
    state.forecastYesterdayChart,
    state.forecastAccuracyChart,
  ],
};

function resizeTabCharts(tabId) {
  for (const chart of TAB_CHARTS[tabId]?.() ?? []) {
    if (chart && typeof chart.resize === "function") chart.resize();
  }
}

// Setzt bei Klick auf einen Flyout-Menuepunkt (siehe HTML: data-subview
// innerhalb von .view-tab-menu) die passende Unteransicht des jeweiligen
// Tabs - ein Eintrag je Tab-Gruppe (data-tab-group), Schluessel = Wert von
// data-subview.
const SUBVIEW_SETTERS = {
  trend: setTrendSubView,
  consumption: setConsumptionSubView,
  forecast: setForecastSubView,
};

function setupViewTabs() {
  const nav = el("view-tabs");
  if (!nav) return;
  const STORAGE_KEY = "kpm-active-view-tab";
  const validTabIds = Object.keys(TAB_LOADERS);

  let stored = null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch (err) {
    // localStorage kann in seltenen Faellen blockiert sein (z.B. striktes
    // Browser-Datenschutz-Profil) - dann einfach ohne Erinnerung starten.
  }
  const initial = validTabIds.includes(stored) ? stored : "overview";

  function activate(tabId) {
    for (const btn of nav.querySelectorAll("button[data-tab]")) {
      btn.classList.toggle("active", btn.dataset.tab === tabId);
    }
    for (const panel of document.querySelectorAll("[data-tab-panel]")) {
      panel.classList.toggle("hidden", panel.dataset.tabPanel !== tabId);
    }
    try {
      localStorage.setItem(STORAGE_KEY, tabId);
    } catch (err) {
      // s.o. - Erinnerung ist ein Komfortfeature, kein Muss.
    }
    // Regelfall seit dem Hintergrund-Vorladen aller Tabs beim Start (siehe
    // init()): tabsLoaded enthaelt hier bereits alle Tab-IDs, dieser Zweig
    // greift nur noch defensiv (z.B. falls ein neuer Tab zukuenftig nicht
    // in die Vorlade-Liste aufgenommen wird).
    if (!state.tabsLoaded.has(tabId)) {
      state.tabsLoaded.add(tabId);
      TAB_LOADERS[tabId]().catch(console.error);
      startTabInterval(tabId);
    }
    // Siehe TAB_CHARTS-Kommentar: unabhaengig davon, ob der Tab neu geladen
    // oder schon im Hintergrund vorbereitet wurde, muss beim Sichtbarwerden
    // die Diagrammgroesse aktualisiert werden.
    resizeTabCharts(tabId);
  }

  // Blendet ein per Hover offenes Flyout-Menue sofort aus, auch wenn die
  // Maus noch darueber steht (z.B. nach Auswahl eines Menuepunkts per
  // Klick) - .menu-suppress gewinnt per CSS gegen die :hover-Regel und wird
  // erst entfernt, wenn die Maus den Reiter tatsaechlich verlaesst.
  function suppressMenuUntilLeave(wrapper) {
    wrapper.classList.remove("menu-open");
    wrapper.classList.add("menu-suppress");
    wrapper.addEventListener(
      "mouseleave",
      () => wrapper.classList.remove("menu-suppress"),
      { once: true }
    );
  }

  nav.addEventListener("click", (e) => {
    const subBtn = e.target.closest("button[data-subview]");
    if (subBtn) {
      const wrapper = subBtn.closest(".view-tab-with-menu");
      const groupId = wrapper?.dataset.tabGroup;
      if (groupId) {
        activate(groupId);
        SUBVIEW_SETTERS[groupId]?.(subBtn.dataset.subview);
        for (const btn of wrapper.querySelectorAll("button[data-subview]")) {
          btn.classList.toggle("active", btn === subBtn);
        }
      }
      if (wrapper) suppressMenuUntilLeave(wrapper);
      return;
    }

    const tabBtn = e.target.closest("button[data-tab]");
    if (!tabBtn) return;
    activate(tabBtn.dataset.tab);

    // Touch-Fallback: ohne Hover gibt es keine andere Moeglichkeit, das
    // Flyout-Menue ueberhaupt zu oeffnen - ein Tipp auf den Reiter blendet
    // es zusaetzlich zum Tab-Wechsel ein bzw. wieder aus.
    const wrapper = tabBtn.closest(".view-tab-with-menu");
    if (wrapper && isTouchDevice()) {
      wrapper.classList.toggle("menu-open");
    }
  });

  // Klick ausserhalb eines Reiters mit Flyout schliesst ein per Touch
  // geoeffnetes Menue wieder (Hover-Menues schliessen ohnehin automatisch,
  // sobald die Maus den Reiter verlaesst).
  document.addEventListener("click", (e) => {
    if (e.target.closest(".view-tab-with-menu")) return;
    for (const wrapper of nav.querySelectorAll(".view-tab-with-menu.menu-open")) {
      wrapper.classList.remove("menu-open");
    }
  });

  // Markiert im Flyout-Menue den zur aktuellen Unteransicht passenden
  // Eintrag als "active" (z.B. nach einem Seiten-Reload, bei dem
  // state.subView noch die Vorgabewerte hat).
  for (const wrapper of nav.querySelectorAll(".view-tab-with-menu")) {
    const groupId = wrapper.dataset.tabGroup;
    const current = state.subView[groupId];
    for (const btn of wrapper.querySelectorAll("button[data-subview]")) {
      btn.classList.toggle("active", btn.dataset.subview === current);
    }
  }

  activate(initial);
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
    state.forecastHoursTodayChart,
    state.forecastYesterdayChart,
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
  setupTrendSubView();
  setupDayCompareControls();
  setupDailyTotalsControls();
  setupAutarkyControls();
  setupHourlyCompareControls();
  setupConsumptionSubView();
  setupForecastSubView();
  setupChartInteractionToggle();
  // Alle Tabs laden jetzt schon beim Start im Hintergrund (nicht erst beim
  // ersten Anklicken, wie frueher) - der Tab-Wechsel selbst wird dadurch
  // spuerbar schneller, weil Daten/Diagramme meist schon bereitstehen,
  // statt erst bei Bedarf nachgeladen zu werden. "Uebersicht" bleibt die
  // Tab, die sofort sichtbar ist, und wird deshalb zuerst gestartet;
  // die anderen drei folgen unmittelbar danach (parallel, da fetch() nicht
  // blockiert - kein spuerbarer Nachteil fuer die Uebersicht).
  for (const tabId of Object.keys(TAB_LOADERS)) {
    state.tabsLoaded.add(tabId);
    TAB_LOADERS[tabId]().catch(console.error);
    startTabInterval(tabId);
  }
  setupViewTabs();
  setChartsInteractive(state.chartsInteractive);
  const LIVE_REFRESH_MS = 20000;
  const ringEl = document.querySelector(".refresh-ring-progress");
  if (ringEl) ringEl.style.setProperty("--refresh-secs", LIVE_REFRESH_MS / 1000 + "s");
  setInterval(() => {
    refreshLiveCards().catch(console.error);
    refreshSummaryCards().catch(console.error);
    restartRefreshRing();
  }, LIVE_REFRESH_MS);
  setInterval(() => refreshPvYieldSummary().catch(console.error), 5 * 60 * 1000);
  // Wie beim Autarkie-Tab (siehe AUTARKY_REFRESH_MS): "Autarkiegrad heute"
  // muss nicht alle 5 Minuten neu berechnet werden, einmal taeglich reicht.
  setInterval(() => refreshAutarkyToday().catch(console.error), AUTARKY_REFRESH_MS);
}

init();
