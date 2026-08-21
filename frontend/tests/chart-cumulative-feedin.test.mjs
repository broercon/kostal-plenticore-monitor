// Im Tagesmodus des Leistungsverlaufs (power-chart) waechst die
// Einspeisungskurve kumulativ seit Mitternacht (kWh), statt wie die
// uebrigen Kurven die Momentanleistung (W) zu zeigen - siehe
// cumulativeKwhSeries() in app.js, spiegelbildlich zu
// aggregation.integrate_kwh() im Backend (inkl. 30-Minuten-Luecken-Regel).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function historyHandlerWith(points) {
  const backend = makeBackend();
  return async (url, options) => {
    if (url.pathname === "/api/readings/history") return points;
    return backend(url, options);
  };
}

function findDataset(chart, label) {
  return chart.data.datasets.find((d) => d.label === label);
}

test("Einspeisung im Tagesdiagramm summiert sich kumulativ (kWh), andere Kurven bleiben Momentanleistung (W)", async () => {
  const points = [
    { timestamp: "2026-07-13T10:00:00Z", home_power_w: 500, feed_in_power_w: 1000, pv_power_w: 1500, battery_power_w: 0, grid_draw_power_w: 0 },
    { timestamp: "2026-07-13T10:15:00Z", home_power_w: 600, feed_in_power_w: 2000, pv_power_w: 2600, battery_power_w: 0, grid_draw_power_w: 0 },
    { timestamp: "2026-07-13T10:30:00Z", home_power_w: 700, feed_in_power_w: 3000, pv_power_w: 3700, battery_power_w: 0, grid_draw_power_w: 0 },
  ];
  const app = await bootApp({ fetchHandler: historyHandlerWith(points) });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.chart);

  const feedin = findDataset(app.state.chart, "Einspeisung (kumuliert seit 0 Uhr)");
  assert.ok(feedin, "kumulierte Einspeisungskurve nicht gefunden");
  assert.equal(feedin.yAxisID, "y1");
  assert.equal(feedin.unit, "kwh");
  const feedinValues = feedin.data.map((p) => p.y);
  // Trapezregel, 15-Minuten-Schritte: (1000+2000)/2*0.25h=375Wh,
  // (2000+3000)/2*0.25h=625Wh -> kumuliert 0 / 0.375 / 1.0 kWh.
  assert.equal(feedinValues[0], 0);
  assert.equal(feedinValues[1], 0.375);
  assert.equal(feedinValues[2], 1.0);

  const home = findDataset(app.state.chart, "Hausverbrauch");
  assert.equal(home.yAxisID, "y");
  assert.equal(home.unit, "watt");
  assert.deepEqual(home.data.map((p) => p.y), [500, 600, 700]);
});

test("Kumulierte Einspeisung ueberbrueckt keine Datenluecke ueber 30 Minuten (wie aggregation.integrate_kwh)", async () => {
  const points = [
    { timestamp: "2026-07-13T10:00:00Z", home_power_w: 0, feed_in_power_w: 1000, pv_power_w: 0, battery_power_w: 0, grid_draw_power_w: 0 },
    // 2 Stunden Luecke -> dieses Intervall wird NICHT interpoliert.
    { timestamp: "2026-07-13T12:00:00Z", home_power_w: 0, feed_in_power_w: 1000, pv_power_w: 0, battery_power_w: 0, grid_draw_power_w: 0 },
    // Danach wieder normaler 15-Minuten-Abstand -> ab hier zaehlt es wieder.
    { timestamp: "2026-07-13T12:15:00Z", home_power_w: 0, feed_in_power_w: 1000, pv_power_w: 0, battery_power_w: 0, grid_draw_power_w: 0 },
  ];
  const app = await bootApp({ fetchHandler: historyHandlerWith(points) });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.chart);

  const feedin = findDataset(app.state.chart, "Einspeisung (kumuliert seit 0 Uhr)");
  const feedinValues = feedin.data.map((p) => p.y);
  assert.equal(feedinValues[0], 0);
  assert.equal(feedinValues[1], 0, "2h-Luecke darf nicht interpoliert werden");
  assert.equal(feedinValues[2], 0.25, "(1000+1000)/2 * 0.25h = 250 Wh = 0.25 kWh");
});

test("Wochenansicht (7 Tage) zeigt Einspeisung weiterhin als normale Momentanleistung, nicht kumuliert", async () => {
  const points = [
    { timestamp: "2026-07-06T12:00:00Z", home_power_w: 0, feed_in_power_w: 1000, pv_power_w: 0, battery_power_w: 0, grid_draw_power_w: 0 },
    { timestamp: "2026-07-13T12:00:00Z", home_power_w: 0, feed_in_power_w: 3000, pv_power_w: 0, battery_power_w: 0, grid_draw_power_w: 0 },
  ];
  const app = await bootApp({ fetchHandler: historyHandlerWith(points) });
  await waitFor(() => app.state.tabsLoaded.has("trend"));
  await waitFor(() => !!app.state.chart);

  const btn7 = app.document.querySelector('#range-buttons button[data-hours="168"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  await waitFor(() => app.state.hours === 168 && app.state.chartMode === "range");

  const feedin = findDataset(app.state.chart, "Einspeisung");
  assert.ok(feedin, "Einspeisungskurve im Wochenmodus nicht gefunden (falscher Label-Text?)");
  assert.equal(feedin.unit, "watt");
  assert.deepEqual(feedin.data, [1000, 3000]);
});
