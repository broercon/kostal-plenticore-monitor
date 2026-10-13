// Test: die PV-Leistung-Kurve zeigt die vom Backend berechnete reine PV
// (pv_pure_power_w), die Batterie-Kurve unveraendert die Batterieleistung.
// Relevant fuer Anlagen mit Batterie am PV3-String (pv_power_w enthaelt dann
// die Batterie, pv_pure_power_w nicht) - bei Geraeten ohne PV3-Wert ist
// pv_pure_power_w dagegen gleich pv_power_w, auch wenn die Batterie entlaedt.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

async function bootWithHistoryPoint(point) {
  const base = makeBackend();
  const handler = async (url) =>
    url.pathname === "/api/readings/history"
      ? [{ timestamp: "2026-07-14T02:00:00", home_power_w: 0, feed_in_power_w: 0, grid_draw_power_w: 0, ...point }]
      : base(url);
  const app = await bootApp({ fetchHandler: handler });
  // Der Leistungsverlauf-Chart gehoert zum "trend"-Tab und laedt erst bei
  // dessen erstem Oeffnen, siehe setupViewTabs()/TAB_LOADERS in app.js.
  app.clickViewTab("trend");
  await waitFor(() => app.chartMetricLast("Batterie") !== null);
  return app;
}

test("Chart: PV-Leistung = reine PV (Batterie an PV3), Batterie separat", async () => {
  const app = await bootWithHistoryPoint({
    pv_power_w: 4000, pv_pure_power_w: 0, battery_power_w: 4000,
  });
  assert.equal(app.chartMetricLast("PV-Leistung"), 0);   // Batterie ist in pv_power_w, nicht in der reinen PV
  assert.equal(app.chartMetricLast("Batterie"), 4000);   // Batterie unveraendert
});

test("Chart: ohne PV3 entlaedt die Batterie, die PV bleibt erhalten", async () => {
  const app = await bootWithHistoryPoint({
    pv_power_w: 600, pv_pure_power_w: 600, battery_power_w: 1600,
  });
  assert.equal(app.chartMetricLast("PV-Leistung"), 600);
  assert.equal(app.chartMetricLast("Batterie"), 1600);
});
