// Tests fuer den "Speicherstand" (Verlauf > Speicherstand): Ladezustand
// (SoC, %) ueber die Zeit, eine eigene Kurve je Geraet mit Batterie (siehe
// aggregation.build_battery_soc_series und app.js refreshBatterySocChart).
// Wie der Leistungsverlauf standardmaessig 24 Std auf fester 00:00-24:00-
// Achse, zusaetzlich 2/3/7/14 Tage waehlbar - anders als bei
// Prognose/Autarkiegrad KEIN Sonderfall bei der Werte-Anzeige.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function backendWithBatterySoc() {
  const base = makeBackend();
  const seenParams = [];
  const handler = async (url) => {
    if (url.pathname === "/api/readings/battery-soc-history") {
      seenParams.push({
        hours: url.searchParams.get("hours"),
        bucket_minutes: url.searchParams.get("bucket_minutes"),
      });
      const hours = Number(url.searchParams.get("hours"));
      const devices = [
        { device_id: "wr1", device_name: "WR1" },
        { device_id: "wr2", device_name: "WR2" },
      ];
      const points =
        hours <= 24
          ? [
              { timestamp: "2026-07-13T00:00:00Z", values: { wr1: 40, wr2: null } },
              { timestamp: "2026-07-13T12:00:00Z", values: { wr1: 70, wr2: 55 } },
            ]
          : [
              { timestamp: "2026-07-12T00:00:00Z", values: { wr1: 30, wr2: 20 } },
              { timestamp: "2026-07-13T00:00:00Z", values: { wr1: 65, wr2: 50 } },
            ];
      return { devices, points };
    }
    return base(url);
  };
  handler.seenParams = seenParams;
  return handler;
}

test("Speicherstand-Bereich ist genau einmal und als eigene Verlauf-Ansicht vorhanden", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);

  const sections = app.document.querySelectorAll("#trend-view-batterysoc");
  assert.equal(sections.length, 1);
  assert.equal(sections[0].parentElement.id, "tab-panel-trend");
  assert.equal(app.document.querySelectorAll("#batterysoc-chart").length, 1);
  assert.equal(
    app.document.getElementById("yearcompare-chart-wrapper").contains(sections[0]),
    false
  );
});

test("Speicherstand: Standardzeitraum ist 24 Std, entsprechender Button aktiv", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.batterySoc.hours, 24);
  const activeBtn = app.document.querySelector("#batterysoc-hour-buttons button.active");
  assert.equal(activeBtn.dataset.hours, "24");
});

test("Speicherstand-Chart wird schon beim Start im Hintergrund geladen (Liniendiagramm, feste 0-100%-Achse)", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.tabsLoaded.has("trend"), true);
  assert.ok(app.state.batterySoc.chart, "Speicherstand-Chart ist schon vor dem ersten Oeffnen aufgebaut");
  assert.equal(app.state.batterySoc.chart.type, "line");
  assert.equal(app.state.batterySoc.chart.options.scales.y.min, 0);
  assert.equal(app.state.batterySoc.chart.options.scales.y.max, 100);
});

test("Speicherstand: ein Dataset je Geraet, Farben nach dayColor()-Palette", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "batterysoc");

  const { datasets } = app.state.batterySoc.chart.data;
  assert.equal(datasets.length, 2);
  assert.deepEqual(datasets.map((d) => d.label), ["WR1", "WR2"]);

  const dayColor = app.window.dayColor;
  assert.equal(datasets[0].borderColor, dayColor(0));
  assert.equal(datasets[1].borderColor, dayColor(1));
});

test("Speicherstand: Tagesmodus liefert {x,y}-Punkte auf der Minuten-des-Tages-Achse", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "batterysoc");

  // Erwarteten x-Wert ueber dieselbe Funktion berechnen, die auch app.js
  // verwendet - robust gegenueber der Zeitzone der Testumgebung, statt
  // einen fest verdrahteten Minutenwert anzunehmen.
  const expectedX = app.window.minuteOfLocalDay(new Date("2026-07-13T00:00:00Z"));

  const wr1 = app.state.batterySoc.chart.data.datasets.find((d) => d.label === "WR1");
  assert.equal(wr1.data[0].x, expectedX);
  assert.equal(wr1.data[0].y, 40);
  const wr2 = app.state.batterySoc.chart.data.datasets.find((d) => d.label === "WR2");
  assert.equal(wr2.data[0].y, null); // kein Messwert fuer WR2 im ersten Bucket
});

test("Speicherstand: Klick auf '7 Tage' fragt mit passendem Stundenparameter neu ab und wechselt aus dem Tagesmodus", async () => {
  const backend = backendWithBatterySoc();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "batterysoc");

  const btn7 = app.document.querySelector('#batterysoc-hour-buttons button[data-hours="168"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.batterySoc.hours === 168);
  await waitFor(() => backend.seenParams.some((p) => p.hours === "168"));

  assert.ok(btn7.classList.contains("active"));
  assert.equal(
    app.document.querySelector('#batterysoc-hour-buttons button[data-hours="24"]').classList.contains("active"),
    false
  );
  // Im Mehrtagesmodus sind die Punkte einfache Zahlen mit einer Labels-Liste,
  // nicht mehr {x,y} auf der festen Tagesachse.
  await waitFor(() => Array.isArray(app.state.batterySoc.chart.data.labels) && app.state.batterySoc.chart.data.labels.length === 2);
  const wr1 = app.state.batterySoc.chart.data.datasets.find((d) => d.label === "WR1");
  assert.equal(wr1.data[1], 65);
});

test("Speicherstand: Werte-Anzeige an im 24h-Tagesmodus, aus bei mehreren Tagen - kein Sonderfall wie bei Prognose/Autarkiegrad", async () => {
  const backend = backendWithBatterySoc();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "batterysoc");

  const isInteractive = (chart) => Array.isArray(chart.options.events) && chart.options.events.length > 0;
  assert.equal(isInteractive(app.state.batterySoc.chart), true);

  const chartBefore = app.state.batterySoc.chart;
  const btn7 = app.document.querySelector('#batterysoc-hour-buttons button[data-hours="168"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.batterySoc.hours === 168);
  await waitFor(() => isInteractive(app.state.batterySoc.chart) === false);

  // Dasselbe Chart.js-Objekt wird bei einer reinen Datenaktualisierung
  // weiterverwendet, solange der Modus (Tag/Range) sich NICHT aendert -
  // hier aendert sich der Modus (24h -> 168h), also wird das Chart-Objekt
  // neu erzeugt (wie beim Leistungsverlauf, siehe refreshChart()).
  assert.notEqual(app.state.batterySoc.chart, chartBefore);
});

test("Speicherstand: buildBatterySocDatasets liefert im Tagesmodus null statt undefined fuer fehlende Werte", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const build = app.window.buildBatterySocDatasets;

  const devices = [{ device_id: "wr1", device_name: "WR1" }];
  const points = [{ timestamp: "2026-07-13T00:00:00Z", values: {} }];
  const datasets = build(devices, points, true);
  assert.equal(datasets[0].data[0].y, null);
});
