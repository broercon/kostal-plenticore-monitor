// Test: die PV-Leistung-Kurve zeigt die reine PV (pv_power_w - battery_power_w),
// die Batterie-Kurve unveraendert die Batterieleistung. Relevant fuer Anlagen
// mit Batterie am PV3-String (pv_power_w enthaelt dann die Batterie).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

test("Chart: PV-Leistung = reine PV, Batterie separat", async () => {
  const base = makeBackend();
  const handler = async (url) =>
    url.pathname === "/api/readings/history"
      ? [{
          timestamp: "2026-07-14T02:00:00",
          pv_power_w: 4000, battery_power_w: 4000,
          home_power_w: 0, feed_in_power_w: 0, grid_draw_power_w: 0,
        }]
      : base(url);

  const app = await bootApp({ fetchHandler: handler });
  // Der Leistungsverlauf-Chart gehoert zum "trend"-Tab und laedt erst bei
  // dessen erstem Oeffnen, siehe setupViewTabs()/TAB_LOADERS in app.js.
  app.clickViewTab("trend");
  await waitFor(() => app.chartMetricLast("Batterie") !== null);
  assert.equal(app.chartMetricLast("PV-Leistung"), 0);   // 4000 - 4000 (Batterie raus)
  assert.equal(app.chartMetricLast("Batterie"), 4000);   // Batterie unveraendert
});
