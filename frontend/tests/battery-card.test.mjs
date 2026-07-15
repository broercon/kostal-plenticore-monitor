// Tests fuer die Sichtbarkeit der Batterie-Kachel: sie bleibt sichtbar,
// solange Batterie-Leistung ODER SoC vorliegt (nachts fehlt zeitweise der
// SoC), und wird nur ausgeblendet, wenn gar keine Batteriedaten da sind.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function backendWithLatest(latest) {
  const base = makeBackend();
  return async (url) => (url.pathname === "/api/readings/latest" ? latest : base(url));
}

const R = (o) => ({
  device_id: "wr1", device_name: "WR1", timestamp: "2026-07-14T02:00:00",
  pv_power_w: 0, home_power_w: 0, feed_in_power_w: 0, grid_draw_power_w: 0,
  battery_power_w: null, battery_soc_percent: null, ...o,
});

test("Batterie-Kachel bleibt nachts sichtbar (Leistung ohne SoC)", async () => {
  const app = await bootApp({
    fetchHandler: backendWithLatest([R({ battery_power_w: -500, battery_soc_percent: null })]),
  });
  const cell = () => app.document.getElementById("card-battery");
  await waitFor(() => cell().textContent !== "–");
  assert.notEqual(
    app.document.getElementById("card-battery-wrapper").style.display, "none");
  assert.match(cell().textContent, /\(–\)/); // SoC unbekannt -> "-"
});

test("Batterie-Kachel wird ohne jegliche Batteriedaten ausgeblendet", async () => {
  const app = await bootApp({
    fetchHandler: backendWithLatest([R({ battery_power_w: null, battery_soc_percent: null })]),
  });
  const wrapper = () => app.document.getElementById("card-battery-wrapper");
  await waitFor(() => wrapper().style.display === "none");
  assert.equal(wrapper().style.display, "none");
});
