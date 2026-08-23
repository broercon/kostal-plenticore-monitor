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

test("Prognosekontrolle: Diagramm steht im Markup vor den Werten (Tages-/Heute-Karten)", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  const section = app.document.getElementById("forecast-accuracy-section");
  const children = [...section.children].map((el) => el.id);
  const chartIndex = children.indexOf("forecast-accuracy-chart-wrapper");
  const daysIndex = children.indexOf("forecast-accuracy-days");
  assert.ok(chartIndex !== -1 && daysIndex !== -1);
  assert.ok(chartIndex < daysIndex, "Diagramm muss vor den Werten stehen");
});

test("Prognosekontrolle: Punktfarbe zeigt Abweichungsrichtung (gruen/rot)", async () => {
  const days = [
    dayEntry("2026-07-14", { expected: 10, actual: 12 }), // +20% -> gruen
    dayEntry("2026-07-13", { expected: 10, actual: 9 }), // -10% -> rot
  ];
  const app = await bootApp({ fetchHandler: backendWithAccuracyDays(days) });
  await waitFor(() => app.state.tabsLoaded.has("forecast"));
  await waitFor(() => !!app.state.forecastAccuracyChart);

  const dataset = app.state.forecastAccuracyChart.data.datasets[0];
  const colorFor = (value) => dataset.pointBackgroundColor({ raw: value });
  assert.equal(colorFor(20), "#4ade80", "positive Abweichung ist gruen");
  assert.equal(colorFor(-10), "#f87171", "negative Abweichung ist rot");
  assert.equal(colorFor(null), "#94a3b8", "fehlender Wert ist neutral grau");
});

test("Prognosekontrolle: kleine Abweichung innerhalb der Anzeige-Toleranz zeigt 'im Rahmen'", async () => {
  const days = [
    // 1 kWh Abweichung bei 10 kWh erwartet liegt an der Toleranzgrenze
    // (max(0.5, 10% von 10 = 1.0) = 1.0 kWh) - siehe app.js
    // isDeviationWithinDisplayTolerance(). Reine Anzeigeentscheidung: die
    // zugrunde liegenden Werte (difference_kwh/accuracy_percent) bleiben
    // unveraendert vom Backend.
    dayEntry("2026-07-14", { expected: 10, actual: 9 }),
  ];
  const app = await bootApp({ fetchHandler: backendWithAccuracyDays(days) });
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  const card = app.document.querySelector("#forecast-accuracy-days .forecast-accuracy-day");
  const badge = card.querySelector(".forecast-accuracy-tolerance-badge");
  assert.ok(badge, "Badge 'im Rahmen' sollte innerhalb der Toleranz angezeigt werden");
  assert.equal(badge.textContent, "im Rahmen");
  // Die Rohwerte selbst bleiben unangetastet.
  assert.match(card.querySelector(".muted").textContent, /Abweichung -1\.0 kWh/);
});

test("Prognosekontrolle: Abweichung ausserhalb der Toleranz zeigt keine 'im Rahmen'-Markierung", async () => {
  const days = [
    // 2 kWh Abweichung bei 10 kWh erwartet liegt klar ueber der Toleranz
    // (1.0 kWh, siehe Test oben).
    dayEntry("2026-07-14", { expected: 10, actual: 12 }),
  ];
  const app = await bootApp({ fetchHandler: backendWithAccuracyDays(days) });
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  const card = app.document.querySelector("#forecast-accuracy-days .forecast-accuracy-day");
  assert.equal(card.querySelector(".forecast-accuracy-tolerance-badge"), null);
});

test("Prognosekontrolle: 'Heute (bisher)' zeigt dieselbe Toleranz-Markierung", async () => {
  const base = makeBackend();
  const app = await bootApp({
    fetchHandler: async (url) => {
      if (url.pathname === "/api/forecast/accuracy") {
        const data = await base(url);
        return {
          ...data,
          today_so_far: {
            date: "2026-07-13",
            expected_kwh: 3.0,
            actual_kwh: 3.2,
            difference_kwh: 0.2,
            difference_percent: 6.7,
            accuracy_percent: 90.0,
            matched_hours: 3,
            devices: [],
          },
        };
      }
      return base(url);
    },
  });
  await waitFor(() => !app.document.getElementById("forecast-accuracy-today").classList.contains("hidden"));

  const today = app.document.getElementById("forecast-accuracy-today");
  const badge = today.querySelector(".forecast-accuracy-tolerance-badge");
  assert.ok(badge, "0.2 kWh Abweichung bei 3.0 kWh erwartet liegt unter der Toleranz (0.5 kWh Sockel)");
});
