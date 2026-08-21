// Tests fuer die neue "Morgen"-Ansicht im Prognose-Tab (analog zu "Heute"
// und "Gestern", siehe app.js refreshForecast()): stuendliche Prognose fuer
// den Folgetag als Balkendiagramm mit Spannbereich, aber ohne
// "Tatsaechlich"-Balken (der Tag hat ja noch nicht stattgefunden) - dazu
// ein Hinweistext, ab wann diese Prognose feststeht (siehe Backend:
// config.FORECAST_FREEZE_TIME / EnergyForecastOut.freeze_time).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function isHidden(document, id) {
  return document.getElementById(id).classList.contains("hidden");
}

// Erweitert den Standard-Mock um einen zweiten Prognosetag (morgen,
// 2026-07-14) mit passenden Stundenwerten - der Standard-Mock in
// harness.mjs liefert bewusst nur EINEN Tag (siehe dortiger Kommentar bzw.
// admin-area.test.mjs, das genau einen ".forecast-day" erwartet), daher
// hier eine eigene, lokale Erweiterung statt harness.mjs anzufassen.
function backendWithTomorrow() {
  const base = makeBackend();
  return async (url) => {
    if (url.pathname === "/api/forecast") {
      const data = await base(url);
      return {
        ...data,
        freeze_time: "22:00",
        days: [
          ...data.days,
          {
            date: "2026-07-14",
            expected_kwh: 15.5,
            low_kwh: 12.0,
            high_kwh: 18.0,
            production_start: "2026-07-14T05:00:00Z",
            production_end: "2026-07-14T19:00:00Z",
            peak_at: "2026-07-14T12:00:00Z",
            peak_kw: 5.0,
            devices: [],
          },
        ],
      };
    }
    return base(url);
  };
}

test("Prognose-Tab zeigt die 'Morgen'-Ansicht nicht per Default, aber ueber das Flyout", async () => {
  const app = await bootApp({ fetchHandler: backendWithTomorrow() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  assert.equal(isHidden(app.document, "forecast-view-tomorrow"), true);

  app.clickSubview("forecast", "tomorrow");
  assert.equal(isHidden(app.document, "forecast-view-tomorrow"), false);
  assert.equal(isHidden(app.document, "forecast-view-days"), true);
});

test("Prognose morgen: Statuszeile nennt Erwartungswert, Bereich und Hinweis auf die Einfriergrenze", async () => {
  const app = await bootApp({ fetchHandler: backendWithTomorrow() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));
  await waitFor(() => !!app.state.forecastTomorrowChart);

  const text = app.document.getElementById("forecast-tomorrow-status").textContent;
  assert.match(text, /15\.5 kWh/);
  assert.match(text, /12\.0.{1,2}18\.0 kWh/);
  // Je nach tatsaechlicher Uhrzeit beim Testlauf ist die Prognose entweder
  // noch nicht oder schon eingefroren - hier wird nur geprueft, dass EINER
  // der beiden Hinweistexte erscheint (siehe isForecastFrozen()), nicht
  // welcher genau (das prueft der separate Test unten deterministisch).
  assert.match(
    text,
    /(wird bis 22:00 Uhr heute Abend laufend aktualisiert und steht erst danach endgültig fest\.)|(steht bereits seit 22:00 Uhr fest\.)/
  );
});

test("Prognose morgen: Diagramm zeigt im Gesamt-Tab die Aufschluesselung je Wechselrichter, keinen 'Tatsächlich'-Balken", async () => {
  const app = await bootApp({ fetchHandler: backendWithTomorrow() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));
  await waitFor(() => !!app.state.forecastTomorrowChart);

  const chart = app.state.forecastTomorrowChart;
  assert.equal(chart.type, "bar");
  // Im Gesamt-Tab ("Alle") zeigt das Diagramm seit der Pro-Geraet-
  // Aufschluesselung (analog zum Wechselrichter-Vergleich) je Wechselrichter
  // einen eigenen Prognose-Balken statt eines einzelnen kombinierten -
  // "Tatsächlich" gibt es fuer morgen weiterhin nicht (Tag liegt in der
  // Zukunft, siehe buildForecastDeviceDatasets(hours, null)).
  assert.equal(chart.data.datasets.length, 2);
  assert.ok(chart.data.datasets.every((d) => d.label.startsWith("Prognose ")));
  assert.ok(chart.data.datasets.every((d) => d.stack === "expected"));
  assert.equal(chart.data.datasets.some((d) => d.label.startsWith("Tatsächlich")), false);
});

test("Prognose morgen: ohne Folgetag-Daten erscheint ein Hinweis statt eines Diagramms", async () => {
  // Der Standard-Mock (ohne backendWithTomorrow-Erweiterung) liefert nur den
  // heutigen Tag - fuer "morgen" also noch keine Prognose.
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  const text = app.document.getElementById("forecast-tomorrow-status").textContent;
  assert.match(text, /Für morgen liegt noch keine stündliche Prognose vor/);
  assert.equal(app.state.forecastTomorrowChart, null);
});

test("isForecastFrozen: false vor, true ab der konfigurierten Einfriergrenze", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const isForecastFrozen = app.window.isForecastFrozen;

  assert.equal(
    isForecastFrozen("22:00", new Date(2026, 6, 13, 21, 59)),
    false,
    "eine Minute vor der Grenze noch nicht eingefroren"
  );
  assert.equal(
    isForecastFrozen("22:00", new Date(2026, 6, 13, 22, 0)),
    true,
    "exakt zur Grenze bereits eingefroren"
  );
  assert.equal(
    isForecastFrozen("22:00", new Date(2026, 6, 13, 23, 0)),
    true,
    "danach weiterhin eingefroren"
  );
});
