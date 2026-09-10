// Die vier Energie-Leisten in der Uebersicht (PV-Ertrag, Einspeisung,
// Speicher geladen/entnommen): Sichtbarkeit nur im Gesamt-Tab, korrekt
// gefuellte Werte - und dass die beiden Speicher-Leisten aus EINER Anfrage
// an /api/readings/battery-summary gefuellt werden.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

const BAR_IDS = [
  "pv-yield-summary",
  "feed-in-summary",
  "battery-charge-summary",
  "battery-discharge-summary",
];

const period = (key, kwh) => ({
  key,
  from_date: "2026-07-13",
  to_date: "2026-07-13",
  kwh,
});

// Backend mit konkreten Werten fuer "heute" + Zaehler der Aufrufe je Pfad.
function backendWithValues() {
  const base = makeBackend();
  const calls = {};
  const handler = async (url, options) => {
    calls[url.pathname] = (calls[url.pathname] || 0) + 1;
    switch (url.pathname) {
      case "/api/readings/pv-yield-summary":
        return { periods: [period("today", 78.0)] };
      case "/api/readings/feed-in-summary":
        return { periods: [period("today", 60.0)] };
      case "/api/readings/battery-summary":
        return {
          charge_periods: [period("today", 10.6)],
          discharge_periods: [period("today", 8.2)],
        };
      default:
        return base(url, options);
    }
  };
  return { handler, calls };
}

const value = (document, attr) =>
  document.querySelector(`[data-${attr}="today"]`).textContent;

test("alle vier Leisten sind im Gesamt-Tab sichtbar", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.selectedDeviceId === "");
  for (const id of BAR_IDS) {
    assert.equal(
      app.document.getElementById(id).classList.contains("hidden"),
      false,
      id
    );
  }
});

test("alle vier Leisten werden fuer einen einzelnen WR ausgeblendet", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  for (const id of BAR_IDS) {
    assert.equal(
      app.document.getElementById(id).classList.contains("hidden"),
      true,
      id
    );
  }
  // Der Hinweis zur Bilanz gehoert zu den Leisten und verschwindet mit ihnen.
  assert.equal(
    app.document.querySelector(".pvyield-note").classList.contains("hidden"),
    true
  );

  app.clickTab("Alle (Summe)");
  await waitFor(() => app.state.selectedDeviceId === "");
  for (const id of BAR_IDS) {
    assert.equal(
      app.document.getElementById(id).classList.contains("hidden"),
      false,
      id
    );
  }
});

test("Werte landen in der jeweils richtigen Leiste", async () => {
  const { handler, calls } = backendWithValues();
  const app = await bootApp({ fetchHandler: handler });
  await waitFor(() => value(app.document, "pvyield") !== "–");

  assert.equal(value(app.document, "pvyield"), "78.0 kWh");
  assert.equal(value(app.document, "feedin"), "60.0 kWh");
  assert.equal(value(app.document, "batterycharge"), "10.6 kWh");
  assert.equal(value(app.document, "batterydischarge"), "8.2 kWh");

  // Laden und Entladen kommen aus derselben Antwort - eine Anfrage genuegt.
  assert.equal(calls["/api/readings/battery-summary"], 1);
});

test("Zeitraum ohne Daten bleibt leer statt 0", async () => {
  const { handler } = backendWithValues();
  const app = await bootApp({ fetchHandler: handler });
  await waitFor(() => value(app.document, "pvyield") !== "–");

  // Nur "today" wird geliefert - die uebrigen Zeitraeume bleiben "–".
  assert.equal(
    app.document.querySelector('[data-batterycharge="last_year"]').textContent,
    "–"
  );
});
