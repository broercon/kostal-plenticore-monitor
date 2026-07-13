// Tests fuer den Schutz gegen veraltete Antworten beim Tab-Wechsel
// (Fix-Branch fix/tab-wechsel-race).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor, sleep } from "./harness.mjs";

test("Tab-Wechsel laedt die Daten des gewaehlten Wechselrichters", async () => {
  const app = await bootApp({
    fetchHandler: makeBackend({
      historyPv: (dev) => (dev === "wr1" ? 600 : dev === "wr2" ? 400 : 1000),
    }),
  });

  app.clickTab("WR1");
  await waitFor(() => app.chartMetricLast("PV-Leistung") === 600);

  app.clickTab("WR2");
  await waitFor(() => app.chartMetricLast("PV-Leistung") === 400);
});

test("schneller Tab-Wechsel: verspaetete Antwort des vorherigen WR ueberschreibt die Anzeige nicht", async () => {
  const app = await bootApp({
    fetchHandler: makeBackend({
      // WR1 antwortet bewusst langsam, WR2 schnell -> die Antworten treffen
      // in vertauschter Reihenfolge ein.
      historyDelayMs: (dev) => (dev === "wr1" ? 250 : 20),
      historyPv: (dev) => (dev === "wr1" ? 600 : dev === "wr2" ? 400 : 1000),
    }),
  });

  // WR1 (langsam) direkt gefolgt von WR2 (schnell): ohne Schutz wuerde die
  // spaeter eintreffende WR1-Antwort das Diagramm ueberschreiben, obwohl
  // WR2 aktiv ist.
  app.clickTab("WR1");
  app.clickTab("WR2");

  await waitFor(() => app.state.selectedDeviceId === "wr2");
  await sleep(300); // bis auch die langsame WR1-Antwort eingetroffen ist

  assert.equal(app.state.selectedDeviceId, "wr2");
  assert.equal(
    app.chartMetricLast("PV-Leistung"),
    400,
    "Diagramm zeigt WR2-Daten (400), nicht die verspaeteten WR1-Daten (600)"
  );
});
