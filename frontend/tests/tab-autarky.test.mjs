// Tests fuer die neue "Autarkie"-Ansicht (Balkendiagramm, Autarkiegrad je
// Monat, siehe app.js refreshAutarkyChart) sowie die "Autarkiegrad
// heute"-Kachel in der Uebersicht (refreshSummaryCards).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function backendWithAutarky() {
  const base = makeBackend();
  return async (url) => {
    if (url.pathname === "/api/readings/autarky-monthly") {
      return {
        months: [
          { month: "2026-05", pv_kwh: 40.0, battery_kwh: 10.0, grid_kwh: 50.0, home_kwh: 100.0, autarky_percent: 50.0 },
          { month: "2026-06", pv_kwh: 60.0, battery_kwh: 15.0, grid_kwh: 25.0, home_kwh: 100.0, autarky_percent: 75.0 },
        ],
      };
    }
    if (url.pathname === "/api/readings/daily-home-breakdown") {
      return {
        days: [
          { date: "2026-07-13", pv_kwh: 6.0, battery_kwh: 1.0, grid_kwh: 3.0, autarky_percent: 70.0 },
        ],
      };
    }
    return base(url);
  };
}

test("Autarkie-Tab laedt schon beim Start im Hintergrund und zeigt den Monatsverlauf", async () => {
  const app = await bootApp({ fetchHandler: backendWithAutarky() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.tabsLoaded.has("autarky"), true);
  assert.ok(app.state.autarky.chart, "Autarkiegrad-Chart ist schon vor dem ersten Oeffnen aufgebaut");
  assert.deepEqual(app.state.autarky.chart.data.labels, ["Mai 2026", "Jun 2026"]);
  assert.deepEqual(app.state.autarky.chart.data.datasets[0].data, [50.0, 75.0]);
});

test("Autarkie-Tab wird per Klick sichtbar (kein erneuter Request noetig)", async () => {
  const app = await bootApp({ fetchHandler: backendWithAutarky() });
  await waitFor(() => app.loadingCount() === 0);

  app.clickViewTab("autarky");
  const panel = app.document.querySelector('[data-tab-panel="autarky"]');
  assert.equal(panel.classList.contains("hidden"), false, "Autarkie-Panel ist sichtbar");
  assert.equal(
    app.isLoading("#autarky-chart-wrapper"),
    false,
    "Umschalten auf den schon vorgeladenen Tab loest keinen neuen Ladeindikator aus"
  );
});

test("Monats-Filter (12/24/36/Alle) loest einen neuen Request mit dem passenden Parameter aus", async () => {
  const seenMonthsParams = [];
  const backend = backendWithAutarky();
  const app = await bootApp({
    fetchHandler: async (url) => {
      if (url.pathname === "/api/readings/autarky-monthly") {
        seenMonthsParams.push(url.searchParams.get("months"));
      }
      return backend(url);
    },
  });
  await waitFor(() => app.loadingCount() === 0);

  app.clickViewTab("autarky");
  const btn12 = app.document.querySelector('#autarky-month-buttons button[data-months="12"]');
  btn12.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => seenMonthsParams.includes("12"));

  assert.ok(btn12.classList.contains("active"));
});

test("Autarkiegrad heute erscheint in der Uebersicht", async () => {
  const app = await bootApp({ fetchHandler: backendWithAutarky() });
  await waitFor(() => app.loadingCount() === 0);
  const value = app.document.getElementById("summary-autarky").textContent;
  assert.equal(value, "70 %");
});

test("Fehler beim Autarkiegrad blockiert die bestehenden Tageskacheln nicht", async () => {
  const base = makeBackend();
  const app = await bootApp({
    fetchHandler: async (url) => {
      if (url.pathname === "/api/readings/daily-home-breakdown") {
        throw new Error("Autarkie-Endpunkt voruebergehend nicht erreichbar");
      }
      if (url.pathname === "/api/readings/today-summary") {
        return [
          {
            device_id: "wr1",
            device_name: "WR1",
            yield_day_kwh: 12.3,
            home_consumption_day_kwh: 8.4,
            energy_grid_day_kwh: 2.1,
          },
        ];
      }
      return base(url);
    },
  });
  await waitFor(() => app.document.getElementById("summary-yield").textContent !== "–");

  assert.notEqual(app.document.getElementById("summary-consumption").textContent, "–");
  assert.equal(app.document.getElementById("summary-autarky").textContent, "–");
});
