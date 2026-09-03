// Tests fuer die "Autarkie"-Ansicht (Liniendiagramm mit dynamischer
// Y-Achse, Autarkiegrad je Kalendermonat/-woche gruppiert nach Jahr - wie
// der Jahresvergleich beim PV-Ertrag ein Jahr eine eigene Kurve auf einer
// festen Jan-Dez- bzw. KW1-53-Achse, siehe app.js refreshAutarkyChart) sowie
// die "Autarkiegrad heute"-Kachel in der Uebersicht (refreshSummaryCards).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function backendWithAutarky() {
  const base = makeBackend();
  const seenParams = [];
  const handler = async (url) => {
    if (url.pathname === "/api/readings/autarky-yearly-comparison") {
      seenParams.push({
        granularity: url.searchParams.get("granularity"),
        years: url.searchParams.get("years"),
      });
      const years = Number(url.searchParams.get("years"));
      const granularity = url.searchParams.get("granularity");
      if (granularity === "week") {
        return {
          granularity: "week",
          labels: Array.from({ length: 53 }, (_, i) => `KW ${i + 1}`),
          years: [{ year: 2026, values: [50, 55, null, ...Array(50).fill(null)] }].slice(0, years),
        };
      }
      const allYears = [
        { year: 2025, values: [50.0, null, null, null, null, null, null, null, null, null, null, null] },
        { year: 2026, values: [null, null, null, null, null, 75.0, null, null, null, null, null, null] },
      ];
      return {
        granularity: "month",
        labels: ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"],
        years: allYears.slice(-years),
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
  handler.seenParams = seenParams;
  return handler;
}

test("Autarkie-Tab laedt schon beim Start im Hintergrund und zeigt den Jahresvergleich als Liniendiagramm", async () => {
  const app = await bootApp({ fetchHandler: backendWithAutarky() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.tabsLoaded.has("autarky"), true);
  assert.ok(app.state.autarky.chart, "Autarkiegrad-Chart ist schon vor dem ersten Oeffnen aufgebaut");
  assert.equal(app.state.autarky.chart.type, "line");
  assert.deepEqual(
    app.state.autarky.chart.data.labels,
    ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
  );
});

test("Autarkie: Standardansicht ist Monate/3 Jahre, entsprechende Buttons aktiv", async () => {
  const backend = backendWithAutarky();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.autarky.granularity, "month");
  assert.equal(app.state.autarky.years, 3);
  assert.ok(backend.seenParams.some((p) => p.years === "3"));

  const activeGranularity = app.document.querySelector("#autarky-granularity-buttons button.active");
  assert.equal(activeGranularity.dataset.granularity, "month");
  const activeYears = app.document.querySelector("#autarky-years-buttons button.active");
  assert.equal(activeYears.dataset.years, "3");
});

test("Autarkie: ein Dataset je Jahr, aktuellstes Jahr = Farbindex 0, Y-Achse skaliert dynamisch statt fest 0-100", async () => {
  const app = await bootApp({ fetchHandler: backendWithAutarky() });
  await waitFor(() => app.loadingCount() === 0);
  app.clickViewTab("autarky");

  const { datasets } = app.state.autarky.chart.data;
  assert.equal(datasets.length, 2);
  assert.deepEqual(datasets.map((d) => d.label), ["2025", "2026"]);

  const dayColor = app.window.dayColor;
  // Aktuellstes Jahr (2026) bekommt Farbindex 0 und die dickere Linie.
  assert.equal(datasets[1].borderColor, dayColor(0));
  assert.equal(datasets[0].borderColor, dayColor(1));
  assert.equal(datasets[1].borderWidth, 2.5);
  assert.equal(datasets[0].borderWidth, 1.5);

  // Werte liegen bei 50/75 % - die Y-Achse darf nicht fest bei 0-100 bleiben
  // (sonst waere der Unterschied kaum sichtbar), sondern soll sich mit
  // Marge an den tatsaechlichen Minimal-/Maximalwert anschmiegen.
  const { min, max } = app.state.autarky.chart.options.scales.y;
  assert.ok(min > 0, `Y-Achsen-Minimum sollte ueber 0 liegen (war ${min})`);
  assert.ok(max < 100, `Y-Achsen-Maximum sollte unter 100 liegen (war ${max})`);
  assert.ok(min < 50 && max > 75, "Marge muss den vollen Wertebereich weiterhin abdecken");
});

test("Autarkie: Klick auf 'Wochen' fragt neu ab und aktualisiert Achsentitel/Labels", async () => {
  const backend = backendWithAutarky();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickViewTab("autarky");

  const btnWeek = app.document.querySelector('#autarky-granularity-buttons button[data-granularity="week"]');
  btnWeek.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.autarky.granularity === "week");
  await waitFor(() => backend.seenParams.some((p) => p.granularity === "week"));

  assert.ok(btnWeek.classList.contains("active"));
  await waitFor(() => app.state.autarky.chart.data.labels[0] === "KW 1");
  assert.equal(app.state.autarky.chart.options.scales.x.title.text, "Kalenderwoche");
});

test("Autarkie: Klick auf Jahres-Button aendert state.autarky.years und fragt mit passendem Parameter neu ab", async () => {
  const backend = backendWithAutarky();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickViewTab("autarky");

  const btn1 = app.document.querySelector('#autarky-years-buttons button[data-years="1"]');
  btn1.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.autarky.years === 1);
  await waitFor(() => backend.seenParams.some((p) => p.years === "1"));

  assert.ok(btn1.classList.contains("active"));
  await waitFor(() => app.state.autarky.chart.data.datasets.length === 1);
  assert.equal(app.state.autarky.chart.data.datasets[0].label, "2026");
});

test("Autarkie: Werte-Anzeige nur an, wenn genau ein Jahr dargestellt wird", async () => {
  const backend = backendWithAutarky();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickViewTab("autarky");

  // Standard sind 3 Jahre -> Werte-Anzeige aus.
  const isInteractive = (chart) => Array.isArray(chart.options.events) && chart.options.events.length > 0;
  assert.equal(isInteractive(app.state.autarky.chart), false);

  const chartBefore = app.state.autarky.chart;
  const btn1 = app.document.querySelector('#autarky-years-buttons button[data-years="1"]');
  btn1.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.autarky.years === 1);
  await waitFor(() => isInteractive(app.state.autarky.chart) === true);

  // Dasselbe Chart.js-Objekt wird bei einer reinen Datenaktualisierung
  // weiterverwendet - options.events muss deshalb explizit nachgezogen
  // werden.
  assert.equal(app.state.autarky.chart, chartBefore);
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
