// Tests fuer das Prognosekontrolle-Diagramm (Abweichung Prognose vs. Ist je
// Tag, siehe app.js refreshForecastAccuracy): Liniendiagramm statt Balken,
// aktuellster Tag links statt rechts, Punktfarbe je nach Abweichungsrichtung.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function dayEntry(date, { expected, actual }) {
  const difference_kwh = Number((actual - expected).toFixed(2));
  const difference_percent = Number(((difference_kwh / expected) * 100).toFixed(1));
  return {
    date,
    expected_kwh: expected,
    actual_kwh: actual,
    difference_kwh,
    difference_percent,
    accuracy_percent: 90.0,
    matched_hours: 24,
    devices: [],
  };
}

function backendWithAccuracyDays(days) {
  const base = makeBackend();
  return async (url) => {
    if (url.pathname === "/api/forecast/accuracy") {
      return {
        available: true,
        message: "Vergleich der gespeicherten Prognosen mit echten Messwerten.",
        overall_accuracy_percent: 90.0,
        today_so_far: null,
        // Absteigend nach Datum, wie vom Backend geliefert (siehe
        // forecast_evaluation.get_forecast_accuracy) - neuester Tag zuerst.
        days,
      };
    }
    return base(url);
  };
}

test("Prognosekontrolle-Diagramm ist ein Liniendiagramm mit dem aktuellsten Tag ganz links", async () => {
  const days = [
    dayEntry("2026-07-14", { expected: 10, actual: 12 }), // neuester Tag, +20%
    dayEntry("2026-07-13", { expected: 10, actual: 9 }), // -10%
    dayEntry("2026-07-12", { expected: 10, actual: 10 }), // 0%
  ];
  const app = await bootApp({ fetchHandler: backendWithAccuracyDays(days) });
  await waitFor(() => app.state.tabsLoaded.has("forecast"));
  await waitFor(() => !!app.state.forecastAccuracyChart);

  const chart = app.state.forecastAccuracyChart;
  assert.equal(chart.type, "line", "Prognosekontrolle ist ein Liniendiagramm, kein Balkendiagramm");

  // Erster Eintrag (ganz links) muss der aktuellste Tag (14.07.) sein.
  assert.equal(chart.data.labels[0], "14.07.");
  assert.equal(chart.data.labels[chart.data.labels.length - 1], "12.07.");

  // Es wird die Abweichung (%) geplottet, nicht Erwartet/Tatsaechlich.
  assert.equal(chart.data.datasets.length, 1);
  assert.deepEqual([...chart.data.datasets[0].data], [20, -10, 0]);
});
