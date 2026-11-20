// Tests fuer "Auswertungen > Energiebilanz" (siehe app.js
// refreshEnergyBalance, Backend /api/readings/energy-balance).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function month(key, overrides = {}) {
  const [y, m] = key.split("-").map(Number);
  const daysInMonth = new Date(y, m, 0).getDate();
  return {
    month: key,
    days: daysInMonth,
    days_in_month: daysInMonth,
    pv_kwh: 100,
    feed_in_kwh: 40,
    battery_charge_kwh: 20,
    battery_discharge_kwh: 18,
    home_pv_kwh: 30,
    home_battery_kwh: 18,
    home_grid_kwh: 52,
    autarky_home_kwh: 100,
    autarky_grid_kwh: 52,
    ...overrides,
  };
}

// 2025 komplett, 2026 bis Mai komplett, Juni laeuft (heute = 15.06.2026).
function balancePayload() {
  const months = [];
  for (let m = 1; m <= 12; m++) months.push(month(`2025-${String(m).padStart(2, "0")}`));
  for (let m = 1; m <= 4; m++) months.push(month(`2026-0${m}`));
  months.push(month("2026-05", { pv_kwh: 150, home_grid_kwh: 32, autarky_home_kwh: 80, autarky_grid_kwh: 32 }));
  months.push(month("2026-06", { days: 15 }));
  return { today: "2026-06-15", months };
}

function backendWith(payload) {
  const base = makeBackend();
  return async (url, options) => {
    if (url.pathname === "/api/readings/energy-balance") return payload;
    return base(url, options);
  };
}

async function openBalance(payload = balancePayload()) {
  const app = await bootApp({ fetchHandler: backendWith(payload) });
  app.clickViewTab("evaluation");
  app.clickSubview("evaluation", "balance");
  await waitFor(() => app.document.querySelectorAll("#balance-body tr").length > 0 || !payload.months.length);
  return app;
}

const text = (app, id) => app.document.getElementById(id).textContent;
const rows = (app) => [...app.document.querySelectorAll("#balance-body tr")];
const click = (app, node) => node.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));

test("Energiebilanz: eigene Unteransicht, startet mit dem neuesten Jahr", async () => {
  const app = await openBalance();
  assert.equal(app.state.subView.evaluation, "balance");
  assert.ok(!app.document.getElementById("evaluation-view-balance").classList.contains("hidden"));
  assert.ok(app.document.getElementById("evaluation-view-pvdaily").classList.contains("hidden"));
  assert.equal(app.document.getElementById("balance-period").value, "2026");
  assert.match(text(app, "balance-coverage"), /2026 läuft noch.*166 von 365 Tagen/);
  // Laufendes Jahr: kein Vorjahresvergleich.
  assert.equal(app.document.querySelector("#balance-consumption .balance-delta"), null);
  assert.equal(app.document.getElementById("balance-next").disabled, true);
  assert.equal(app.document.getElementById("balance-prev").disabled, false);

  const table = rows(app).map((r) => r.firstChild.textContent);
  assert.deepEqual(table, ["2025", "2026 (läuft)"]);
});

test("Energiebilanz: Jahressummen, Anteile und Kennzahlen nach Klick auf eine Zeile", async () => {
  const app = await openBalance();
  click(app, rows(app)[0]);
  await waitFor(() => app.document.getElementById("balance-period").value === "2025");

  const consumption = text(app, "balance-consumption");
  assert.match(consumption, /Hausverbrauch\s*1200 kWh/);
  assert.match(consumption, /Direkt von der PV-Anlage\s*360 kWh\s*30 %/);
  assert.match(consumption, /Aus dem Speicher\s*216 kWh\s*18 %/);
  assert.match(consumption, /Aus dem Netz\s*624 kWh\s*52 %/);
  const segs = app.document.querySelectorAll("#balance-consumption .balance-bar-seg");
  assert.equal(segs.length, 3);
  assert.equal(segs[0].style.width, "30%");

  const generation = text(app, "balance-generation");
  assert.match(generation, /Erzeugt\s*1200 kWh/);
  assert.match(generation, /Ins Netz eingespeist\s*480 kWh\s*40 %/);
  // 100 - (30 + 20 + 40) = 10 kWh je Monat bleiben als Verluste.
  assert.match(generation, /Wandlungsverluste & Sonstiges\s*120 kWh\s*10 %/);

  const kpis = text(app, "balance-kpis");
  assert.match(kpis, /Autarkiegrad\s*48 %/);
  assert.match(kpis, /Eigenverbrauch\s*60 %/);
  assert.match(kpis, /Speicher geladen\s*240 kWh/);
  assert.ok(rows(app)[0].classList.contains("balance-row-active"));
});

test("Energiebilanz: Monatsansicht mit Vorjahresvergleich und gleichem Monat in anderen Jahren", async () => {
  const app = await openBalance();
  click(app, app.document.querySelector('#balance-granularity-buttons button[data-granularity="month"]'));
  await waitFor(() => app.document.getElementById("balance-period").value === "2026-06");
  assert.match(text(app, "balance-coverage"), /Juni 2026 läuft noch.*15 von 30 Tagen/);

  click(app, app.document.getElementById("balance-prev"));
  await waitFor(() => app.document.getElementById("balance-period").value === "2026-05");
  assert.match(text(app, "balance-coverage"), /Mai 2026: alle 31 Tage erfasst/);
  assert.match(text(app, "balance-consumption"), /Hausverbrauch\s*80\.0 kWh/);
  assert.match(text(app, "balance-consumption"), /−20 % gegenüber Mai 2025 \(100 kWh\)/);
  assert.match(text(app, "balance-generation"), /\+50 % gegenüber Mai 2025/);

  assert.equal(text(app, "balance-compare-title"), "Monate 2026 im Vergleich");
  const labels = rows(app).map((r) => r.firstChild.textContent);
  assert.deepEqual(labels, [
    "Jan 2026", "Feb 2026", "Mär 2026", "Apr 2026", "Mai 2026", "Jun 2026 (läuft)",
    "Mai in anderen Jahren", "Mai 2025",
  ]);
  // Mischbalken sind auf den groessten Verbrauch der Tabelle skaliert.
  const may2026 = rows(app)[4].querySelector(".balance-bar");
  assert.equal(may2026.style.width, "80%");

  // Zurueck auf Jahr: das Jahr des gewaehlten Monats bleibt.
  click(app, app.document.querySelector('#balance-granularity-buttons button[data-granularity="year"]'));
  await waitFor(() => app.document.getElementById("balance-period").value === "2026");
});

test("Energiebilanz: lückenhafter Zeitraum und fehlende Netzmessung", async () => {
  const app = await openBalance({
    today: "2026-06-15",
    months: [month("2024-11", { days: 10, autarky_home_kwh: null, autarky_grid_kwh: null })],
  });
  assert.match(text(app, "balance-coverage"), /2024: nur 10 von 366 Tagen erfasst/);
  assert.match(text(app, "balance-kpis"), /Autarkiegrad\s*–/);
  assert.equal(rows(app)[0].firstChild.textContent, "2024 (lückenhaft)");
});

test("Energiebilanz: ohne Messdaten ein Hinweis statt leerer Karten", async () => {
  const app = await openBalance({ today: "2026-06-15", months: [] });
  await waitFor(() => /Noch keine Messdaten/.test(text(app, "balance-coverage")));
  assert.ok(app.document.getElementById("balance-cards").classList.contains("hidden"));
  assert.equal(rows(app).length, 0);
});
