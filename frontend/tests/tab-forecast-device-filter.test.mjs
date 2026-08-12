// Tests fuer die Wechselrichter-Filterung der Prognose (Tab "Prognose"):
// im Gesamt-Tab werden die ueber alle Geraete summierten Werte gezeigt, nach
// Wechsel auf einen einzelnen Wechselrichter nur noch dessen eigener Anteil
// (Kacheln, Diagramm und Prognosekontrolle).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

test("Prognose zeigt im Gesamt-Tab die ueber alle Geraete summierten Werte", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.document.querySelector("#forecast-days .forecast-day-value"));

  const value = app.document.querySelector("#forecast-days .forecast-day-value");
  assert.equal(value.textContent, "12.4 kWh");
  const devices = app.document.querySelector("#forecast-days .forecast-day-devices");
  assert.match(devices.textContent, /WR1: 8\.0 kWh/);
  assert.match(devices.textContent, /WR2: 4\.4 kWh/);

  const dataset = app.state.forecastChart.data.datasets.find(
    (d) => d.label === "Erwartete PV-Leistung"
  );
  assert.equal(dataset.data[0], 4.2); // kombinierter Stundenwert
});

test("Prognose zeigt nach Wechsel auf WR1 nur dessen eigenen Anteil", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.document.querySelector("#forecast-days .forecast-day-value"));

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(
    () => app.document.querySelector("#forecast-days .forecast-day-value").textContent === "8.0 kWh"
  );

  const value = app.document.querySelector("#forecast-days .forecast-day-value");
  assert.equal(value.textContent, "8.0 kWh");
  const range = app.document.querySelector("#forecast-days .muted");
  assert.equal(range.textContent, "Bereich 6.5–9.4 kWh");
  // Die Pro-Geraet-Aufschluesselung ist jetzt redundant (schon auf ein
  // Geraet gefiltert) und wird deshalb ausgeblendet.
  const devices = app.document.querySelector("#forecast-days .forecast-day-devices");
  assert.equal(devices.textContent, "");

  const dataset = app.state.forecastChart.data.datasets.find(
    (d) => d.label === "Erwartete PV-Leistung"
  );
  assert.equal(dataset.data[0], 2.7); // nur WR1s Anteil der Stunde, nicht 4.2

  // Zurueck auf "Alle (Summe)": wieder die kombinierten Werte.
  app.clickTab("Alle (Summe)");
  await waitFor(() => app.state.selectedDeviceId === "");
  await waitFor(
    () => app.document.querySelector("#forecast-days .forecast-day-value").textContent === "12.4 kWh"
  );
});

test("Prognosekontrolle zeigt nach Wechsel auf WR1 nur dessen eigenen Vergleich", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() =>
    app.document.querySelector("#forecast-accuracy-days .forecast-accuracy-values")
  );

  const combined = app.document.querySelector(
    "#forecast-accuracy-days .forecast-accuracy-values"
  );
  assert.match(combined.textContent, /11\.5/);

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(() =>
    app.document
      .querySelector("#forecast-accuracy-days .forecast-accuracy-values")
      .textContent.includes("7.5")
  );

  const filtered = app.document.querySelector(
    "#forecast-accuracy-days .forecast-accuracy-values"
  );
  assert.match(filtered.textContent, /Erwartet 7\.5 · tatsächlich 8\.0 kWh/);
  const devices = app.document.querySelector(
    "#forecast-accuracy-days .forecast-accuracy-devices"
  );
  assert.equal(devices.textContent, "");
});
