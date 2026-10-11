// Werte-Anzeige (Tooltip/Hover) der Diagramme: am Desktop bei ALLEN
// Diagrammen immer an (siehe chartEvents() in app.js), unabhaengig von
// Tages-/Jahresanzahl; auf einem Touch-Geraet immer aus (damit die Seite
// frei scrollt).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function isInteractive(chart) {
  return !!chart && Array.isArray(chart.options.events) && chart.options.events.length > 0;
}

test("Der frueher globale 'Werte anzeigen'-Knopf existiert nicht mehr", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  assert.equal(app.document.querySelector(".chart-interaction-toggle"), null);
});

test("Leistungsverlauf (Tagesansicht, Desktop): Werte automatisch an", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.chart);
  assert.equal(isInteractive(app.state.chart), true);
});

test("Leistungsverlauf: Wechsel auf 7 Tage laesst die Werte-Anzeige an", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.chart);

  const btn7 = app.document.querySelector('#range-buttons button[data-hours="168"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.hours === 168);
  await waitFor(() => app.state.hours === 168);
  assert.equal(isInteractive(app.state.chart), true);
});

test("Tagesvergleich: Werte-Anzeige bleibt bei mehreren Tagen an - auch ohne Diagramm-Neuaufbau", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.dayCompare.chart);
  assert.equal(isInteractive(app.state.dayCompare.chart), true);

  const chartBefore = app.state.dayCompare.chart;
  const btn7 = app.document.querySelector('#daycompare-day-buttons button[data-days="7"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.dayCompare.days === 7);
  assert.equal(isInteractive(app.state.dayCompare.chart), true);
  // Dasselbe Chart.js-Objekt wird bei einer reinen Datenaktualisierung
  // weiterverwendet (kein destroy()/new Chart) - genau der Fall, der
  // options.events explizit nachziehen muss, siehe Kommentar in
  // refreshDayCompareChart().
  assert.equal(app.state.dayCompare.chart, chartBefore);
});

test("Wechselrichter-Vergleich: Werte-Anzeige bleibt bei mehreren Tagen an - auch ohne Diagramm-Neuaufbau", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("evaluation"));
  await waitFor(() => !!app.state.hourlyCompare.chart);
  assert.equal(isInteractive(app.state.hourlyCompare.chart), true);

  const chartBefore = app.state.hourlyCompare.chart;
  const btn7 = app.document.querySelector('#hourly-day-buttons button[data-days="7"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.hourlyCompare.days === 7);
  assert.equal(isInteractive(app.state.hourlyCompare.chart), true);
  assert.equal(app.state.hourlyCompare.chart, chartBefore);
});

test("Tagesverbrauch: Werte-Anzeige an", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.dailyTotals.chart);

  assert.equal(isInteractive(app.state.dailyTotals.chart), true);
});

test("Autarkiegrad (Standard 3 Jahre): Werte-Anzeige an", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.autarky.chart);

  assert.equal(isInteractive(app.state.autarky.chart), true);
});

test("Alle Prognose-Diagramme (heute/morgen/gestern, Tagesuebersicht, Prognosekontrolle) zeigen die Werte immer an", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("forecast"));
  await waitFor(() => !!app.state.forecastHoursTodayChart);
  await waitFor(() => !!app.state.forecastYesterdayChart);
  await waitFor(() => !!app.state.forecastChart);
  await waitFor(() => !!app.state.forecastAccuracyChart);

  assert.equal(isInteractive(app.state.forecastHoursTodayChart), true);
  assert.equal(isInteractive(app.state.forecastYesterdayChart), true);
  if (app.state.forecastTomorrowChart) {
    assert.equal(isInteractive(app.state.forecastTomorrowChart), true);
  }
  assert.equal(isInteractive(app.state.forecastChart), true);
  assert.equal(isInteractive(app.state.forecastAccuracyChart), true);
});

test("Auf einem Touch-Geraet bleiben die Werte auch in der Tagesansicht immer aus", async () => {
  const app = await bootApp({ fetchHandler: makeBackend(), touch: true });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.chart);
  assert.equal(isInteractive(app.state.chart), false);
});

test("Neue Auswertungs-Diagramme und Jahresvergleich/Speicherstand zeigen die Werte am Desktop an", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("evaluation"));
  await waitFor(() => !!app.state.pvDaily.chart && !!app.state.yearCompare.chart);
  assert.equal(isInteractive(app.state.pvDaily.chart), true);
  assert.equal(isInteractive(app.state.yearCompare.chart), true);
});
