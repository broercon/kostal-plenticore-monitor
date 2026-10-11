// Tests fuer den "Jahresvergleich" (Verlauf > Jahresvergleich): PV-Erzeugung
// je Kalendermonat oder ISO-Kalenderwoche, ein Jahr eine eigene Kurve auf
// einer festen Jan-Dez- bzw. KW1-53-Achse (siehe daily_summary.
// build_yearly_comparison und app.js refreshYearCompareChart). Analog zum
// Tagesvergleich, nur auf Jahresebene statt Tagesebene.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function backendWithYearCompare() {
  const base = makeBackend();
  const seenParams = [];
  const handler = async (url) => {
    if (url.pathname === "/api/readings/yearly-comparison") {
      seenParams.push({
        granularity: url.searchParams.get("granularity"),
        years: url.searchParams.get("years"),
        per_device: url.searchParams.get("per_device"),
      });
      const years = Number(url.searchParams.get("years"));
      const granularity = url.searchParams.get("granularity");
      if (granularity === "week") {
        return {
          granularity: "week",
          labels: Array.from({ length: 53 }, (_, i) => `KW ${i + 1}`),
          years: [
            { year: 2026, values: [10, 12, null, ...Array(50).fill(null)] },
          ].slice(0, years),
        };
      }
      // Standard: Monatsansicht mit so vielen Jahren wie angefragt (max. 3
      // Beispieljahre hinterlegt, mehr braucht keiner der Tests hier).
      const allYears = [
        { year: 2024, values: [100, 110, 90, ...Array(9).fill(null)] },
        { year: 2025, values: [120, 130, 100, ...Array(9).fill(null)] },
        { year: 2026, values: [140, null, null, ...Array(9).fill(null)] },
      ];
      const perDevice = url.searchParams.get("per_device") === "true";
      const withDevices = (y) => ({
        ...y,
        device_values: {
          wr1: y.values.map((v) => (v === null ? null : v * 0.75)),
          wr2: y.values.map((v) => (v === null ? null : v * 0.25)),
        },
      });
      return {
        granularity: "month",
        labels: ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"],
        devices: perDevice
          ? [
              { device_id: "wr1", device_name: "WR1" },
              { device_id: "wr2", device_name: "WR2" },
            ]
          : [],
        years: allYears.slice(-years).map((y) => (perDevice ? withDevices(y) : y)),
      };
    }
    return base(url);
  };
  handler.seenParams = seenParams;
  return handler;
}

test("Jahresvergleich: Standardansicht ist Monate/3 Jahre, entsprechende Buttons aktiv", async () => {
  const app = await bootApp({ fetchHandler: backendWithYearCompare() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.yearCompare.granularity, "month");
  assert.equal(app.state.yearCompare.years, 3);

  const activeGranularity = app.document.querySelector("#yearcompare-granularity-buttons button.active");
  assert.equal(activeGranularity.dataset.granularity, "month");
  const activeYears = app.document.querySelector("#yearcompare-years-buttons button.active");
  assert.equal(activeYears.dataset.years, "3");
});

test("Jahresvergleich-Chart wird schon beim Start im Hintergrund geladen (Liniendiagramm)", async () => {
  const app = await bootApp({ fetchHandler: backendWithYearCompare() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.tabsLoaded.has("trend"), true);
  assert.ok(app.state.yearCompare.chart, "Jahresvergleich-Chart ist schon vor dem ersten Oeffnen aufgebaut");
  assert.equal(app.state.yearCompare.chart.type, "line");
  assert.deepEqual(
    app.state.yearCompare.chart.data.labels,
    ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
  );
});

test("Jahresvergleich: ein Dataset je Jahr, aktuellstes Jahr = Farbindex 0 und dickere Linie", async () => {
  const app = await bootApp({ fetchHandler: backendWithYearCompare() });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "yearcompare");

  const { datasets } = app.state.yearCompare.chart.data;
  assert.equal(datasets.length, 3);
  assert.deepEqual(datasets.map((d) => d.label), ["2024", "2025", "2026"]);

  const dayColor = app.window.dayColor;
  // Aeltestes Jahr (2024) zuerst im Array, aber am wenigsten aktuell ->
  // hoechster colorIndex; aktuellstes Jahr (2026) bekommt Farbindex 0 und
  // die dickere Linie (siehe buildYearCompareDatasets-Kommentar in app.js).
  assert.equal(datasets[2].borderColor, dayColor(0));
  assert.equal(datasets[1].borderColor, dayColor(1));
  assert.equal(datasets[0].borderColor, dayColor(2));
  assert.equal(datasets[2].borderWidth, 2.5);
  assert.equal(datasets[0].borderWidth, 1.5);
  assert.equal(datasets[1].borderWidth, 1.5);
});

test("Jahresvergleich: Klick auf 'Wochen' fragt neu ab und aktualisiert Achsentitel/Labels", async () => {
  const backend = backendWithYearCompare();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "yearcompare");

  const btnWeek = app.document.querySelector('#yearcompare-granularity-buttons button[data-granularity="week"]');
  btnWeek.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.yearCompare.granularity === "week");
  await waitFor(() => backend.seenParams.some((p) => p.granularity === "week"));

  assert.ok(btnWeek.classList.contains("active"));
  assert.equal(
    app.document.querySelector('#yearcompare-granularity-buttons button[data-granularity="month"]').classList.contains("active"),
    false
  );
  await waitFor(() => app.state.yearCompare.chart.data.labels[0] === "KW 1");
  assert.equal(app.state.yearCompare.chart.options.scales.x.title.text, "Kalenderwoche");
});

test("Jahresvergleich: Klick auf Jahres-Button aendert state.yearCompare.years und fragt mit passendem Parameter neu ab", async () => {
  const backend = backendWithYearCompare();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "yearcompare");

  const btn1 = app.document.querySelector('#yearcompare-years-buttons button[data-years="1"]');
  btn1.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.yearCompare.years === 1);
  await waitFor(() => backend.seenParams.some((p) => p.years === "1"));

  assert.ok(btn1.classList.contains("active"));
  await waitFor(() => app.state.yearCompare.chart.data.datasets.length === 1);
  assert.equal(app.state.yearCompare.chart.data.datasets[0].label, "2026");
});

test("Jahresvergleich: Werte-Anzeige bleibt bei Jahreswechsel an (dasselbe Chart-Objekt)", async () => {
  const backend = backendWithYearCompare();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "yearcompare");

  const isInteractive = (chart) => Array.isArray(chart.options.events) && chart.options.events.length > 0;
  assert.equal(isInteractive(app.state.yearCompare.chart), true);

  const chartBefore = app.state.yearCompare.chart;
  const btn1 = app.document.querySelector('#yearcompare-years-buttons button[data-years="1"]');
  btn1.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.yearCompare.years === 1);
  await waitFor(() => app.state.yearCompare.chart.data.datasets.length === 1);
  assert.equal(isInteractive(app.state.yearCompare.chart), true);

  // Dasselbe Chart.js-Objekt wird bei einer reinen Datenaktualisierung
  // weiterverwendet.
  assert.equal(app.state.yearCompare.chart, chartBefore);
});

test("Jahresvergleich: Umschalten auf die Unteransicht macht die Sektion sichtbar (Daten schon vorgeladen)", async () => {
  const backend = backendWithYearCompare();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);

  // Wie bei "power"/"dailytotals" loest auch hier setTrendSubView() beim
  // Umschalten erneut refreshYearCompareChart() aus (kein Spezialfall) -
  // hier wird nur geprueft, dass die Sektion sichtbar wird und mit den
  // unveraenderten Standardparametern (month/3) erneut fehlerfrei laedt.
  app.clickSubview("trend", "yearcompare");
  const section = app.document.getElementById("trend-view-yearcompare");
  assert.equal(section.classList.contains("hidden"), false);
  await waitFor(() => app.loadingCount() === 0);
  assert.equal(app.state.yearCompare.chart.data.datasets.length, 3);
});

test("Jahresvergleich: veraltete Antwort einer inzwischen ueberholten Anfrage wird verworfen", async () => {
  const inFlight = [];
  const backend = async (url) => {
    if (url.pathname === "/api/readings/yearly-comparison") {
      const years = url.searchParams.get("years");
      return new Promise((resolve) => {
        inFlight.push({ years, resolve });
      });
    }
    return makeBackend()(url);
  };
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => inFlight.length === 1);

  // Erste (spaeter veraltete) Anfrage mit dem Default years=3.
  const first = inFlight[0];
  const btn1 = app.document.querySelector('#yearcompare-years-buttons button[data-years="1"]');
  btn1.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => inFlight.length === 2);
  const second = inFlight[1];

  // Zweite Anfrage (years=1) beantwortet zuerst und aktueller Stand.
  second.resolve({ granularity: "month", labels: [], years: [{ year: 2026, values: [] }] });
  await waitFor(() => !!app.state.yearCompare.chart);
  // Verspaetete erste Antwort (years=3) darf den inzwischen aktuelleren
  // Stand nicht mehr ueberschreiben (siehe Stale-Response-Guard in
  // refreshYearCompareChart()).
  first.resolve({
    granularity: "month",
    labels: [],
    years: [{ year: 2024, values: [] }, { year: 2025, values: [] }, { year: 2026, values: [] }],
  });
  await new Promise((r) => setTimeout(r, 10));

  assert.equal(app.state.yearCompare.years, 1);
  assert.equal(app.state.yearCompare.chart.data.datasets.length, 1);
});

test("Jahresvergleich: Darstellungs-Buttons nur bei mehreren Wechselrichtern sichtbar, Standard 'Summe'", async () => {
  const app = await bootApp({ fetchHandler: backendWithYearCompare() });
  await waitFor(() => app.loadingCount() === 0);
  const container = app.document.getElementById("yearcompare-mode-buttons");
  assert.equal(container.classList.contains("hidden"), false);
  assert.equal(app.state.yearCompare.mode, "total");
  assert.equal(container.querySelector("button.active").dataset.mode, "total");
});

test("Jahresvergleich 'Je Wechselrichter': Saeulen, je Jahr ein Stapel, Jahresfarbe als helle/dunkle Variante", async () => {
  const backend = backendWithYearCompare();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => app.loadingCount() === 0);
  app.clickSubview("trend", "yearcompare");

  const btn = app.document.querySelector('#yearcompare-mode-buttons button[data-mode="device"]');
  btn.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.yearCompare.chart?.type === "bar");
  assert.ok(backend.seenParams.some((p) => p.per_device === "true"));

  const chart = app.state.yearCompare.chart;
  const { datasets } = chart.data;
  assert.equal(datasets.length, 6); // 3 Jahre x 2 Wechselrichter
  // Array.from: die Arrays stammen aus dem jsdom-Fenster (andere Realm).
  assert.deepEqual(
    Array.from(datasets.map((d) => d.label)),
    ["2024 · WR1", "2024 · WR2", "2025 · WR1", "2025 · WR2", "2026 · WR1", "2026 · WR2"]
  );
  // Ein Stapel je Jahr.
  assert.equal(datasets[0].stack, "2024");
  assert.equal(datasets[1].stack, "2024");
  assert.equal(datasets[4].stack, "2026");
  // Die Wechselrichter eines Jahres unterscheiden sich in der Farbe ...
  assert.notEqual(datasets[4].backgroundColor, datasets[5].backgroundColor);
  // ... und die Jahre untereinander ebenfalls.
  assert.notEqual(datasets[4].backgroundColor, datasets[2].backgroundColor);
  assert.equal(chart.options.scales.x.stacked, true);
  assert.deepEqual(Array.from(datasets[4].data.slice(0, 2)), [140 * 0.75, null]);
  // Hover bei Saeulen immer an.
  assert.ok(chart.options.events.includes("mousemove"));

  // Zurueck auf 'Summe': wieder ein Liniendiagramm mit einem Dataset je Jahr.
  const total = app.document.querySelector('#yearcompare-mode-buttons button[data-mode="total"]');
  total.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.yearCompare.chart?.type === "line");
  await waitFor(() => app.state.yearCompare.chart.data.datasets.length === 3);
});

test("deviceShade: erstes Geraet heller, letztes dunkler als die Jahresfarbe, ein Geraet unveraendert", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const shade = app.window.deviceShade;
  const lum = (hex) => [1, 3, 5].reduce((sum, o) => sum + parseInt(hex.slice(o, o + 2), 16), 0);
  const base = "#60a5fa";
  assert.equal(shade(base, 0, 1), base);
  assert.ok(lum(shade(base, 0, 2)) > lum(base));
  assert.ok(lum(shade(base, 1, 2)) < lum(base));
  assert.match(shade(base, 1, 3), /^#[0-9a-f]{6}$/);
});
