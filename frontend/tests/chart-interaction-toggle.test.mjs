// Werte-Anzeige (Tooltip/Hover) der Diagramme: seit dem fruehreren globalen
// An/Aus-Knopf (der verwirrenderweise an fuenf Stellen im Dashboard auftauchte,
// aber ALLE Diagramme auf einmal umgeschaltet hat) jetzt automatisch je
// Diagramm bestimmt (siehe chartEventsFor() in app.js): am Desktop an, wenn
// genau EIN Tag dargestellt wird, sonst aus; auf einem Touch-Geraet immer aus
// (damit die Seite frei scrollt).
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

test("Leistungsverlauf: Wechsel auf 7 Tage schaltet die Werte-Anzeige automatisch aus", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.chart);

  const btn7 = app.document.querySelector('#range-buttons button[data-hours="168"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.hours === 168);
  await waitFor(() => isInteractive(app.state.chart) === false);
  assert.equal(isInteractive(app.state.chart), false);
});

test("Tagesvergleich: 1 Tag (Default) an, mehrere Tage aus - auch ohne Diagramm-Neuaufbau", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.dayCompare.chart);
  assert.equal(isInteractive(app.state.dayCompare.chart), true);

  const chartBefore = app.state.dayCompare.chart;
  const btn7 = app.document.querySelector('#daycompare-day-buttons button[data-days="7"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.dayCompare.days === 7);
  await waitFor(() => isInteractive(app.state.dayCompare.chart) === false);
  // Dasselbe Chart.js-Objekt wird bei einer reinen Datenaktualisierung
  // weiterverwendet (kein destroy()/new Chart) - genau der Fall, der
  // options.events explizit nachziehen muss, siehe Kommentar in
  // refreshDayCompareChart().
  assert.equal(app.state.dayCompare.chart, chartBefore);
});

test("Wechselrichter-Vergleich: 1 Tag (Default) an, mehrere Tage aus - auch ohne Diagramm-Neuaufbau", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("consumption"));
  await waitFor(() => !!app.state.hourlyCompare.chart);
  assert.equal(isInteractive(app.state.hourlyCompare.chart), true);

  const chartBefore = app.state.hourlyCompare.chart;
  const btn7 = app.document.querySelector('#hourly-day-buttons button[data-days="7"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.hourlyCompare.days === 7);
  await waitFor(() => isInteractive(app.state.hourlyCompare.chart) === false);
  assert.equal(app.state.hourlyCompare.chart, chartBefore);
});

test("Tagesverbrauch und Autarkiegrad zeigen strukturell nie einen Einzeltag - Werte-Anzeige bleibt immer aus", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => app.state.tabsLoaded.has("autarky"));
  await waitFor(() => !!app.state.dailyTotals.chart);
  await waitFor(() => !!app.state.autarky.chart);

  assert.equal(isInteractive(app.state.dailyTotals.chart), false);
  assert.equal(isInteractive(app.state.autarky.chart), false);
});

test("Stuendliche Prognose-Diagramme (heute/morgen/gestern, je genau ein Tag) sind an, die mehrtaegigen Prognose-Diagramme aus", async () => {
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
  assert.equal(isInteractive(app.state.forecastChart), false);
  assert.equal(isInteractive(app.state.forecastAccuracyChart), false);
});

test("Auf einem Touch-Geraet bleiben die Werte auch in der Tagesansicht immer aus", async () => {
  const app = await bootApp({ fetchHandler: makeBackend(), touch: true });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.chart);
  assert.equal(isInteractive(app.state.chart), false);
});
