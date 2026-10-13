// Tests fuer "Auswertungen > Erzeugung je Tag": gestapeltes Saeulendiagramm
// der PV-Erzeugung je Tag und Wechselrichter (siehe app.js
// refreshPvDailyChart, Backend /api/readings/daily-pv-per-device).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function backendSeen() {
  const base = makeBackend();
  const seenDays = [];
  const handler = async (url) => {
    if (url.pathname === "/api/readings/daily-pv-per-device") {
      seenDays.push(url.searchParams.get("days"));
    }
    return base(url);
  };
  handler.seenDays = seenDays;
  return handler;
}

test("Erzeugung je Tag: Standard 1 Tag, ein gestapeltes Dataset je Wechselrichter", async () => {
  const backend = backendSeen();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => !!app.state.pvDaily.chart);

  assert.equal(app.state.pvDaily.days, 1);
  assert.equal(backend.seenDays[0], "1");
  const active = app.document.querySelector("#pvdaily-day-buttons button.active");
  assert.equal(active.dataset.days, "1");

  const chart = app.state.pvDaily.chart;
  assert.equal(chart.type, "bar");
  assert.deepEqual(chart.data.datasets.map((d) => d.label), ["WR1", "WR2"]);
  assert.deepEqual(chart.data.datasets[0].data, [10.0, 12.5]);
  assert.deepEqual(chart.data.datasets[1].data, [4.0, null]);
  // Unterschiedliche Farben je Wechselrichter, gleicher Stapel.
  assert.notEqual(chart.data.datasets[0].backgroundColor, chart.data.datasets[1].backgroundColor);
  assert.equal(chart.data.datasets[0].stack, chart.data.datasets[1].stack);
  assert.equal(chart.options.scales.x.stacked, true);
  assert.equal(chart.options.scales.y.stacked, true);
});

test("Erzeugung je Tag: Zeitraum-Buttons 7/14/Monat/2 Monate fragen 7/14/30/60 Tage ab", async () => {
  const backend = backendSeen();
  const app = await bootApp({ fetchHandler: backend });
  await waitFor(() => !!app.state.pvDaily.chart);

  for (const [btnDays, expected] of [["7", 7], ["14", 14], ["30", 30], ["60", 60]]) {
    const btn = app.document.querySelector(`#pvdaily-day-buttons button[data-days="${btnDays}"]`);
    btn.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
    await waitFor(() => app.state.pvDaily.days === expected);
    await waitFor(() => backend.seenDays.includes(String(expected)));
    assert.ok(btn.classList.contains("active"));
  }
  assert.equal(
    app.document.querySelector('#pvdaily-day-buttons button[data-days="30"]').textContent.trim(),
    "Monat"
  );
  assert.equal(
    app.document.querySelector('#pvdaily-day-buttons button[data-days="60"]').textContent.trim(),
    "2 Monate"
  );
});

test("Erzeugung je Tag: Hover bleibt auch bei mehreren Tagen an (Werte sind der Kern der Saeulen)", async () => {
  const app = await bootApp({ fetchHandler: backendSeen() });
  await waitFor(() => !!app.state.pvDaily.chart);
  const events = app.state.pvDaily.chart.options.events;
  assert.ok(events.includes("mousemove"));
});

test("Erzeugung je Tag: ein einzeln gewaehlter Wechselrichter zeigt nur seine Reihe (Farbe bleibt)", async () => {
  const app = await bootApp({ fetchHandler: backendSeen() });
  await waitFor(() => !!app.state.pvDaily.chart);
  const colorWr2 = app.state.pvDaily.chart.data.datasets[1].backgroundColor;

  app.clickTab("WR2");
  await waitFor(() => app.state.selectedDeviceId === "wr2");
  await waitFor(() => app.state.pvDaily.chart.data.datasets.length === 1);
  const [only] = app.state.pvDaily.chart.data.datasets;
  assert.equal(only.label, "WR2");
  assert.equal(only.backgroundColor, colorWr2);
});
