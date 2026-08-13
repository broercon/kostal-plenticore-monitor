// Tests fuer die Wechselrichter-Filterung der Prognose (Tab "Prognose"):
// im Gesamt-Tab werden die ueber alle Geraete summierten Werte gezeigt, nach
// Wechsel auf einen einzelnen Wechselrichter nur noch dessen eigener Anteil
// (Kacheln, Diagramm und Prognosekontrolle).
// TZ fest auf UTC setzen, BEVOR harness.mjs/jsdom geladen wird: die Tests
// unten pruefen ueber fmtForecastTime() formatierte lokale Uhrzeiten, die
// sonst je nach Zeitzone des Test-Rechners (siehe TZ-Umgebungsvariable)
// unterschiedlich ausfallen wuerden.
process.env.TZ = "UTC";

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
  assert.equal(dataset.data[1], 4.2); // kombinierter Stundenwert (12 Uhr)
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
  assert.equal(dataset.data[1], 2.7); // nur WR1s Anteil der Stunde (12 Uhr), nicht 4.2

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

test("Stuendliche Prognose zeigt nur die Stunden von heute, nicht von morgen", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.document.querySelectorAll("#forecast-hours-today .forecast-hour").length > 0);

  const rows = [...app.document.querySelectorAll("#forecast-hours-today .forecast-hour")];
  // Der Mock liefert 3 Stunden: zwei am 13.07. (heute) und eine am 14.07.
  // (morgen) - nur die beiden von heute duerfen erscheinen.
  assert.equal(rows.length, 2);
  const times = rows.map((row) => row.querySelector("strong").textContent);
  assert.deepEqual(times.sort(), ["11:00", "12:00"]);
});

test("Stuendliche Prognose heute filtert nach Wechsel auf WR1 auf dessen eigenen Anteil", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.document.querySelectorAll("#forecast-hours-today .forecast-hour").length > 0);

  const combinedValue = app.document.querySelector(
    "#forecast-hours-today .forecast-hour-value"
  );
  assert.equal(combinedValue.textContent, "3.0 kW");
  const combinedDevices = app.document.querySelector(
    "#forecast-hours-today .forecast-hour-devices"
  );
  assert.match(combinedDevices.textContent, /WR1: 2.0 kW/);
  assert.match(combinedDevices.textContent, /WR2: 1.0 kW/);

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(
    () =>
      app.document.querySelector("#forecast-hours-today .forecast-hour-value").textContent ===
      "2.0 kW"
  );

  const filteredValue = app.document.querySelector(
    "#forecast-hours-today .forecast-hour-value"
  );
  assert.equal(filteredValue.textContent, "2.0 kW");
  const filteredDevices = app.document.querySelector(
    "#forecast-hours-today .forecast-hour-devices"
  );
  assert.equal(filteredDevices.textContent, "");
});
