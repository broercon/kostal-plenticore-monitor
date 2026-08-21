// Tests fuer die Pro-Geraet-Aufschluesselung der Prognose-Balkendiagramme
// (buildForecastDeviceDatasets/forecastStackFooter in app.js) - unabhaengig
// von den Integrations-Tests in tab-forecast-device-filter.test.mjs und
// forecast-tomorrow.test.mjs, die dieselben Helfer ueber den vollen
// refreshForecast()/refreshForecastYesterday()-Ablauf pruefen.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend } from "./harness.mjs";

const HOURS = [
  {
    local_hour: "2026-07-13T01:00:00",
    expected_kw: 3.0,
    devices: [
      { device_id: "wr1", device_name: "WR1", expected_kw: 2.0, low_kw: 1.6, high_kw: 2.4 },
      { device_id: "wr2", device_name: "WR2", expected_kw: 1.0, low_kw: 0.8, high_kw: 1.2 },
    ],
  },
  {
    local_hour: "2026-07-13T14:00:00",
    expected_kw: 4.2,
    devices: [
      { device_id: "wr1", device_name: "WR1", expected_kw: 2.7, low_kw: 2.1, high_kw: 3.2 },
      { device_id: "wr2", device_name: "WR2", expected_kw: 1.5, low_kw: 1.3, high_kw: 1.8 },
    ],
  },
];

test("buildForecastDeviceDatasets: ein Prognose-Dataset je Geraet, gestapelt", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const build = app.window.buildForecastDeviceDatasets;

  const datasets = build(HOURS, null);
  assert.equal(datasets.length, 2);
  assert.equal(datasets[0].label, "Prognose WR1");
  assert.deepEqual(datasets[0].data, [2.0, 2.7]);
  assert.equal(datasets[0].stack, "expected");
  assert.equal(datasets[1].label, "Prognose WR2");
  assert.deepEqual(datasets[1].data, [1.0, 1.5]);
  // Ohne getActual (z.B. "morgen") entsteht kein "Tatsaechlich"-Stapel.
  assert.equal(datasets.some((d) => d.label.startsWith("Tatsächlich")), false);
});

test("buildForecastDeviceDatasets: mit getActual zusaetzlich ein Tatsaechlich-Dataset je Geraet", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const build = app.window.buildForecastDeviceDatasets;

  const actuals = { wr1: [1.9, 2.6], wr2: [0.9, 1.4] };
  const getActual = (hour, deviceId) => {
    const idx = HOURS.indexOf(hour);
    return actuals[deviceId][idx];
  };
  const datasets = build(HOURS, getActual);
  assert.equal(datasets.length, 4);
  const actualWr1 = datasets.find((d) => d.label === "Tatsächlich WR1");
  const actualWr2 = datasets.find((d) => d.label === "Tatsächlich WR2");
  assert.deepEqual(actualWr1.data, [1.9, 2.6]);
  assert.equal(actualWr1.stack, "actual");
  assert.deepEqual(actualWr2.data, [0.9, 1.4]);
});

test("buildForecastDeviceDatasets: keine Geraete in den Stunden -> leere Liste", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const build = app.window.buildForecastDeviceDatasets;

  assert.deepEqual([...build([], null)], []);
  assert.deepEqual([...build([{ local_hour: "x", devices: [] }], null)], []);
});

test("forecastStackFooter: undefined ohne gestapelte Datasets (Einzelgeraet-Ansicht)", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const footer = app.window.forecastStackFooter;

  const items = [
    { dataset: { label: "Prognose" }, parsed: { y: 4.2 } },
    { dataset: { label: "Tatsächlich" }, parsed: { y: 3.7 } },
  ];
  assert.equal(footer(items), undefined);
});

test("forecastStackFooter: summiert Prognose- und Tatsaechlich-Stapel im Gesamt-Tab", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const footer = app.window.forecastStackFooter;

  const items = [
    { dataset: { label: "Prognose WR1", stack: "expected" }, parsed: { y: 2.7 } },
    { dataset: { label: "Prognose WR2", stack: "expected" }, parsed: { y: 1.5 } },
    { dataset: { label: "Tatsächlich WR1", stack: "actual" }, parsed: { y: 2.5 } },
    { dataset: { label: "Tatsächlich WR2", stack: "actual" }, parsed: { y: 1.2 } },
  ];
  assert.deepEqual([...footer(items)], [
    "Prognose gesamt: 4.2 kWh",
    "Tatsächlich gesamt: 3.7 kWh",
  ]);
});

test("forecastStackFooter: ohne Tatsaechlich-Stapel (z.B. 'morgen') nur die Prognose-Zeile", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const footer = app.window.forecastStackFooter;

  const items = [
    { dataset: { label: "Prognose WR1", stack: "expected" }, parsed: { y: 3.0 } },
    { dataset: { label: "Prognose WR2", stack: "expected" }, parsed: { y: 2.0 } },
  ];
  assert.deepEqual([...footer(items)], ["Prognose gesamt: 5.0 kWh"]);
});

test("Prognose heute/gestern verdrahten forecastStackFooter als Tooltip-Footer", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  const { waitFor } = await import("./harness.mjs");
  await waitFor(() => app.state.forecastHoursTodayChart !== null);

  const footerCb = app.state.forecastHoursTodayChart.options.plugins.tooltip.callbacks.footer;
  assert.equal(footerCb, app.window.forecastStackFooter);
});
