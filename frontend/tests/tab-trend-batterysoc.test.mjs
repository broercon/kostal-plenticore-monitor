// Tests fuer den "Speicherstand" (Verlauf > Speicherstand): Ladezustand
// (SoC, %) je Kalendertag, eine eigene Kurve je Tag UND Geraet mit
// Batterie, alle Tage auf einer gemeinsamen 00:00-24:00-Achse
// uebereinandergelegt wie beim Tagesvergleich (siehe
// aggregation.build_battery_soc_day_series und app.js
// refreshBatterySocChart). Standardmaessig 1 Tag, zusaetzlich 2/3/7/14
// Tage waehlbar - wie Tagesvergleich/Jahresvergleich KEIN Sonderfall bei
// der Werte-Anzeige (nur bei genau einem dargestellten Tag automatisch
// an).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function backendWithBatterySoc() {
  const base = makeBackend();
  const seenParams = [];
  const handler = async (url) => {
    if (url.pathname === "/api/readings/battery-soc-history") {
      seenParams.push({
        days: url.searchParams.get("days"),
        bucket_minutes: url.searchParams.get("bucket_minutes"),
      });
      const days = Number(url.searchParams.get("days"));
      const devices = [
        { device_id: "wr1", device_name: "WR1" },
        { device_id: "wr2", device_name: "WR2" },
      ];
      const daysData =
        days <= 1
          ? [
              {
                date: "2026-07-13",
                points: [
                  { minute: 0, values: { wr1: 40, wr2: null } },
                  { minute: 720, values: { wr1: 70, wr2: 55 } },
                ],
              },
            ]
          : [
              { date: "2026-07-12", points: [{ minute: 0, values: { wr1: 30, wr2: 20 } }] },
              { date: "2026-07-13", points: [{ minute: 0, values: { wr1: 65, wr2: 50 } }] },
            ];
      return { devices, days: daysData };
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

test("Speicherstand: Standardzeitraum ist 1 Tag, entsprechender Button aktiv", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.batterySoc.days, 1);
  const activeBtn = app.document.querySelector("#batterysoc-day-buttons button.active");
  assert.equal(activeBtn.dataset.days, "1");
});

test("Speicherstand-Chart wird schon beim Start im Hintergrund geladen (Liniendiagramm, feste Achsen)", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.tabsLoaded.has("trend"), true);
  assert.ok(app.state.batterySoc.chart, "Speicherstand-Chart ist schon vor dem ersten Oeffnen aufgebaut");
  assert.equal(app.state.batterySoc.chart.type, "line");
  assert.equal(app.state.batterySoc.chart.options.scales.y.min, 0);
  assert.equal(app.state.batterySoc.chart.options.scales.y.max, 100);
  assert.equal(app.state.batterySoc.chart.options.scales.x.min, 0);
  assert.equal(app.state.batterySoc.chart.options.scales.x.max, 1440);
});

test("Speicherstand: ein Dataset je Tag und Geraet, Farbe nach Tag statt Geraet, Geraete per Strichstil unterschieden", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "batterysoc");

  const { datasets } = app.state.batterySoc.chart.data;
  // 1 Tag (Standard) x 2 Geraete = 2 Datasets.
  assert.equal(datasets.length, 2);
  assert.equal(datasets[0].label, "13.07. · WR1");
  assert.equal(datasets[1].label, "13.07. · WR2");

  const dayColor = app.window.dayColor;
  // Nur ein Tag dargestellt -> derselbe (aktuellste) Tag fuer beide Geraete.
  assert.equal(datasets[0].borderColor, dayColor(0));
  assert.equal(datasets[1].borderColor, dayColor(0));
  assert.equal(datasets[0].borderDash.length, 0);
  assert.equal(datasets[1].borderDash[0], 6);
  assert.equal(datasets[1].borderDash[1], 4);
});

test("Speicherstand: Punkte liegen als {x,y} auf der Minuten-des-Tages-Achse, fehlende Werte werden null", async () => {
  const app = await bootApp({ fetchHandler: backendWithBatterySoc() });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "batterysoc");

  const wr1 = app.state.batterySoc.chart.data.datasets.find((d) => d.label === "13.07. · WR1");
  assert.equal(wr1.data[0].x, 0);
  assert.equal(wr1.data[0].y, 40);
  const wr2 = app.state.batterySoc.chart.data.datasets.find((d) => d.label === "13.07. · WR2");
  assert.equal(wr2.data[0].y, null); // kein Messwert fuer WR2 im ersten Bucket
});

test("Speicherstand: Klick auf '7 Tage' fragt mit passendem Tagesparameter neu ab und ueberlagert mehrere Tage", async () => {
  const backend = backendWithBatterySoc();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "batterysoc");

  const btn7 = app.document.querySelector('#batterysoc-day-buttons button[data-days="7"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.batterySoc.days === 7);
  await waitFor(() => backend.seenParams.some((p) => p.days === "7"));

  assert.ok(btn7.classList.contains("active"));
  assert.equal(
    app.document.querySelector('#batterysoc-day-buttons button[data-days="1"]').classList.contains("active"),
    false
  );
  // Zwei zurueckgegebene Tage x zwei Geraete = 4 Datasets, je eine eigene
  // Farbe pro Tag (aktuellster Tag = colorIndex 0).
  await waitFor(() => app.state.batterySoc.chart.data.datasets.length === 4);
  const dayColor = app.window.dayColor;
  const yesterday = app.state.batterySoc.chart.data.datasets.find((d) => d.label === "12.07. · WR1");
  const today = app.state.batterySoc.chart.data.datasets.find((d) => d.label === "13.07. · WR1");
  assert.equal(today.borderColor, dayColor(0));
  assert.equal(yesterday.borderColor, dayColor(1));
});

test("Speicherstand: Werte-Anzeige an bei 1 Tag, aus bei mehreren Tagen - dasselbe Chart-Objekt bleibt bestehen", async () => {
  const backend = backendWithBatterySoc();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "batterysoc");

  const isInteractive = (chart) => Array.isArray(chart.options.events) && chart.options.events.length > 0;
  assert.equal(isInteractive(app.state.batterySoc.chart), true);

  const chartBefore = app.state.batterySoc.chart;
  const btn7 = app.document.querySelector('#batterysoc-day-buttons button[data-days="7"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.batterySoc.days === 7);
  await waitFor(() => isInteractive(app.state.batterySoc.chart) === false);

  // Anders als frueher (Tages-/Range-Modus mit unterschiedlicher Achse)
  // aendert sich die Achse jetzt nie mehr - das Chart.js-Objekt wird daher
  // wie beim Tagesvergleich immer weiterverwendet, nur Daten/Optionen
  // werden aktualisiert.
  assert.equal(app.state.batterySoc.chart, chartBefore);
});

test("Speicherstand: buildBatterySocDatasets liefert null statt undefined fuer fehlende Werte", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const build = app.window.buildBatterySocDatasets;

  const devices = [{ device_id: "wr1", device_name: "WR1" }];
  const days = [{ date: "2026-07-13", points: [{ minute: 0, values: {} }] }];
  const datasets = build(devices, days);
  assert.equal(datasets[0].data[0].y, null);
});
