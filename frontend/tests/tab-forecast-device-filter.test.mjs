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

test("Prognosekontrolle zeigt 'Heute (bisher)' separat von den abgeschlossenen Tagen", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => !app.document.getElementById("forecast-accuracy-today").classList.contains("hidden"));

  const today = app.document.getElementById("forecast-accuracy-today");
  assert.match(today.textContent, /Heute \(bisher\)/);
  assert.match(today.textContent, /Erwartet 3\.0 · tatsächlich 4\.5 kWh/);
  assert.match(today.textContent, /3 Stundenwerte bisher/);
  // Muss unabhaengig von der Kachel-Liste abgeschlossener Tage bestehen -
  // andere ".forecast-accuracy-day"-Elemente (siehe #forecast-accuracy-days)
  // duerfen dadurch nicht verschwinden.
  assert.equal(
    app.document.querySelectorAll("#forecast-accuracy-days .forecast-accuracy-day").length,
    1
  );

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(() => today.textContent.includes("2.0"));
  assert.match(today.textContent, /Erwartet 2\.0 · tatsächlich 3\.0 kWh/);
});

test("Stuendliche Prognose (Balkendiagramm) zeigt nur die Stunden von heute, nicht von morgen", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.forecastHoursTodayChart !== null);

  // Der Mock liefert 3 Stunden: zwei gehoeren laut Backend-Anlagenzeit zum
  // 13.07. (heute), obwohl eine davon in UTC noch der 12.07. ist. Die dritte
  // gehoert zum 14.07. - nur die beiden von heute duerfen im Diagramm
  // erscheinen.
  const labels = app.state.forecastHoursTodayChart.data.labels;
  assert.deepEqual(labels.sort(), ["12:00", "23:00"]);

  // Im Gesamt-Tab ("Alle") zeigt das Diagramm die Pro-Geraet-Aufschluesselung
  // (siehe buildForecastDeviceDatasets()) statt eines einzelnen kombinierten
  // Prognose-Balkens - je Wechselrichter ein eigener, gestapelter Balken.
  const datasets = app.state.forecastHoursTodayChart.data.datasets;
  const wr1 = datasets.find((d) => d.label === "Prognose WR1");
  assert.ok(wr1);
  assert.equal(wr1.stack, "expected");
  assert.equal(wr1.data.length, 2);
  for (const value of wr1.data) {
    assert.equal(typeof value, "number");
  }
});

test("Stuendliche Prognose (Balkendiagramm) filtert nach Wechsel auf WR1 auf den kombinierten Floating-Bar-Balken", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.forecastHoursTodayChart !== null);

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(() =>
    app.state.forecastHoursTodayChart.data.datasets.some((d) => d.label === "Prognose")
  );

  // Bei ausgewaehltem Einzelgeraet bleibt es beim bisherigen kombinierten
  // Floating-Bar-Balken (Prognose-Spannbereich) + "Tatsächlich" - die
  // Pro-Geraet-Aufschluesselung ist dort redundant (schon auf ein Geraet
  // gefiltert), siehe hoursTodayDatasets in refreshForecast().
  const datasets = app.state.forecastHoursTodayChart.data.datasets;
  assert.equal(datasets.length, 2);
  const forecastDataset = datasets.find((d) => d.label === "Prognose");
  assert.equal(forecastDataset.data.length, 2);
  for (const range of forecastDataset.data) {
    assert.equal(range.length, 2);
    assert.ok(range[0] <= range[1]);
  }
  assert.equal(forecastDataset.expectedData.length, 2);
});

test("Stuendliche Prognose (Balkendiagramm): Pro-Geraet-Aufschluesselung im Gesamt-Tab, kombiniert nach Wechsel auf WR1", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.forecastHoursTodayChart !== null);

  function datasets() {
    return app.state.forecastHoursTodayChart.data.datasets;
  }
  function byLabel(label) {
    return datasets().find((d) => d.label === label);
  }

  // Gesamt-Tab ("Alle"): je Wechselrichter ein eigener Prognose- und
  // Tatsächlich-Balken (gestapelt), analog zum Wechselrichter-Vergleich.
  // WR1: 2.0 (23 Uhr) und 2.7 (12 Uhr) erwartet, Ist-Wert 0 bzw. 2.5 kWh.
  // WR2: 1.0 und 1.5 erwartet, Ist-Wert 0 bzw. 1.2 kWh.
  assert.deepEqual([...byLabel("Prognose WR1").data].sort((a, b) => a - b), [2.0, 2.7]);
  assert.deepEqual([...byLabel("Prognose WR2").data].sort((a, b) => a - b), [1.0, 1.5]);
  assert.deepEqual([...byLabel("Tatsächlich WR1").data].sort((a, b) => a - b), [0, 2.5]);
  assert.deepEqual([...byLabel("Tatsächlich WR2").data].sort((a, b) => a - b), [0, 1.2]);
  assert.equal(byLabel("Prognose WR1").stack, "expected");
  assert.equal(byLabel("Tatsächlich WR1").stack, "actual");

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(() => datasets().some((d) => d.label === "Prognose"));

  function forecastDataset() {
    return byLabel("Prognose");
  }
  function actualData() {
    return byLabel("Tatsächlich").data;
  }
  function forecastHighValues() {
    return [...forecastDataset().data].map((range) => range[1]);
  }

  // Nach Wechsel auf WR1: wieder der bisherige kombinierte Floating-Bar-
  // Balken, aber nur mit WR1s eigenem Anteil - 2.0 (23 Uhr, Spannbereich
  // 1.6-2.4) und 2.7 (12 Uhr, Spannbereich 2.1-3.2); Ist-Wert nur WR1s
  // gemessener Anteil (0 bzw. 2.5 kWh), nicht die Summe mit WR2.
  assert.deepEqual([...forecastDataset().expectedData].sort((a, b) => a - b), [2.0, 2.7]);
  assert.deepEqual(forecastHighValues().sort((a, b) => a - b), [2.4, 3.2]);
  assert.deepEqual([...actualData()].sort((a, b) => a - b), [0, 2.5]);
});

test("Stuendliche Prognose wird bei Geraet ohne eigene Prognose geleert", async () => {
  const base = makeBackend();
  const app = await bootApp({
    fetchHandler: async (url, options) => {
      if (url.pathname === "/api/devices") {
        return [
          { id: "wr1", name: "WR1", host: "h1" },
          { id: "wr2", name: "WR2", host: "h2" },
          { id: "wr3", name: "WR3 ohne Historie", host: "h3" },
        ];
      }
      return base(url, options);
    },
  });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.forecastHoursTodayChart !== null);

  app.clickTab("WR3 ohne Historie");
  await waitFor(() => app.state.selectedDeviceId === "wr3");
  await waitFor(() =>
    app.document.getElementById("forecast-status").textContent.includes("zu wenig Historie")
  );

  assert.equal(app.state.forecastHoursTodayChart, null);
});

test("Prognose gestern zeigt das Datum vollstaendig formatiert, nicht als rohen ISO-String", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  app.clickSubview("forecast", "yesterday");
  await waitFor(() => app.state.forecastYesterdayChart !== null);

  const status = app.document.getElementById("forecast-yesterday-status").textContent;
  assert.match(status, /Sonntag, 12\. Juli 2026/);
  assert.doesNotMatch(status, /2026-07-12/);
});

test("Prognose gestern zeigt die stuendlichen Werte je Wechselrichter und kombiniert nach Filterwechsel", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.forecastYesterdayChart !== null);

  const labels = app.state.forecastYesterdayChart.data.labels;
  assert.equal(labels.length, 2);

  function datasets() {
    return app.state.forecastYesterdayChart.data.datasets;
  }
  function byLabel(label) {
    return datasets().find((d) => d.label === label);
  }

  // Gesamt-Tab ("Alle"): Pro-Geraet-Aufschluesselung statt eines einzelnen
  // kombinierten Balkens - WR1 erwartet 1.2/3.0, Ist 1.4/2.6; WR2 erwartet
  // 0.8/1.5, Ist 0.9/1.4 (siehe harness.mjs /api/forecast/yesterday-Mock).
  assert.deepEqual([...byLabel("Prognose WR1").data].sort((a, b) => a - b), [1.2, 3.0]);
  assert.deepEqual([...byLabel("Prognose WR2").data].sort((a, b) => a - b), [0.8, 1.5]);
  assert.deepEqual([...byLabel("Tatsächlich WR1").data].sort((a, b) => a - b), [1.4, 2.6]);
  assert.deepEqual([...byLabel("Tatsächlich WR2").data].sort((a, b) => a - b), [0.9, 1.4]);

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(() => datasets().some((d) => d.label === "Prognose"));

  function forecastDataset() {
    return byLabel("Prognose");
  }
  function actualData() {
    return byLabel("Tatsächlich").data;
  }

  // Nach Wechsel auf WR1: wieder der bisherige kombinierte Balken, aber nur
  // mit WR1s eigenem Anteil.
  assert.deepEqual([...forecastDataset().expectedData].sort((a, b) => a - b), [1.2, 3.0]);
  assert.deepEqual([...actualData()].sort((a, b) => a - b), [1.4, 2.6]);
});

test("Prognose gestern wird bei Geraet ohne eigene Prognose geleert", async () => {
  const base = makeBackend();
  const app = await bootApp({
    fetchHandler: async (url, options) => {
      if (url.pathname === "/api/devices") {
        return [
          { id: "wr1", name: "WR1", host: "h1" },
          { id: "wr2", name: "WR2", host: "h2" },
          { id: "wr3", name: "WR3 ohne Historie", host: "h3" },
        ];
      }
      return base(url, options);
    },
  });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.forecastYesterdayChart !== null);

  app.clickTab("WR3 ohne Historie");
  await waitFor(() => app.state.selectedDeviceId === "wr3");
  await waitFor(() =>
    app.document
      .getElementById("forecast-yesterday-status")
      .textContent.includes("keine gespeicherten Prognosen")
  );

  assert.equal(app.state.forecastYesterdayChart, null);
});
