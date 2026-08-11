// Tests fuer den (jetzt pro Panel unabhaengigen) Ladeindikator sowie das
// Lazy-Loading der Ansichts-Tabs (siehe withLoading()/setupViewTabs() in
// app.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor, sleep } from "./harness.mjs";

// /history je Geraet unterschiedlich verzoegern, damit es ein sichtbares
// Ladefenster gibt: WR1 langsam, WR2 schnell.
const DELAYS = { wr1: 250, wr2: 60 };
const backend = () =>
  makeBackend({
    historyDelayMs: (dev) => DELAYS[dev] ?? 20,
    historyPv: (dev) => (dev === "wr1" ? 600 : dev === "wr2" ? 400 : 1000),
  });

test("Ansichts-Tabs ausser 'Uebersicht' laden erst beim ersten Oeffnen", async () => {
  const app = await bootApp({ fetchHandler: backend() });
  await waitFor(() => app.loadingCount() === 0);

  // Direkt nach dem Start ist die Uebersicht fertig, der Verlaufs-Chart
  // ("trend"-Tab) wurde noch nie geoeffnet und hat daher auch nie geladen.
  assert.equal(app.state.tabsLoaded.has("overview"), true);
  assert.equal(app.state.tabsLoaded.has("trend"), false);
  assert.equal(app.state.chart, null, "Leistungsverlauf-Chart wurde noch nicht aufgebaut");

  app.clickViewTab("trend");
  assert.ok(
    app.isLoading("#power-chart-wrapper"),
    "direkt nach dem ersten Oeffnen zeigt der Verlaufs-Tab seinen eigenen Ladeindikator"
  );
  await waitFor(() => !app.isLoading("#power-chart-wrapper"));
  assert.equal(app.state.tabsLoaded.has("trend"), true);
  assert.ok(app.state.chart, "Leistungsverlauf-Chart ist jetzt aufgebaut");
});

test("fertige Panels bleiben sofort sichtbar, auch waehrend ein anderes Panel noch laedt", async () => {
  const app = await bootApp({ fetchHandler: backend() });
  await waitFor(() => app.loadingCount() === 0);

  // "Verlauf" einmal oeffnen, damit ein Wechselrichter-Wechsel ihn ueberhaupt
  // mit neu laedt (siehe refreshLoadedTabs()).
  app.clickViewTab("trend");
  await waitFor(() => app.loadingCount() === 0);

  // WR1 ist im Mock-Backend langsam (250ms) - die Live-Kacheln haben dagegen
  // keine Verzoegerung und sollten daher deutlich frueher fertig sein als
  // der Verlaufs-Chart, nicht erst gemeinsam mit ihm.
  app.clickTab("WR1");
  assert.ok(app.isLoading("#power-chart-wrapper"), "Verlauf startet seinen eigenen Ladeindikator");

  await waitFor(() => !app.isLoading("#live-cards"));
  assert.ok(
    app.isLoading("#power-chart-wrapper"),
    "Verlauf laedt noch weiter, obwohl die Live-Kacheln schon fertig und sichtbar sind"
  );

  await waitFor(() => app.loadingCount() === 0);
});

test("bei schnellem Wechselrichter-Wechsel verschwindet der Indikator erst, wenn die zuletzt ausgeloeste Anfrage fertig ist", async () => {
  const app = await bootApp({ fetchHandler: backend() });
  await waitFor(() => app.loadingCount() === 0);

  app.clickViewTab("trend");
  await waitFor(() => app.loadingCount() === 0);

  // Schnell WR1 -> WR2: WR1 ist langsamer und trifft spaeter ein. Beide
  // Anfragen betreffen denselben Panel-Knoten (#power-chart-wrapper) -
  // die Referenzzaehlung in withLoading()/beginLoading()/endLoading() muss
  // verhindern, dass die zuerst fertige Antwort den Indikator schon
  // ausblendet, waehrend die andere noch laeuft.
  app.clickTab("WR1");
  app.clickTab("WR2");
  assert.ok(
    app.isLoading("#power-chart-wrapper"),
    "direkt nach dem Schnellklick ist der Indikator sichtbar"
  );

  await waitFor(() => !app.isLoading("#power-chart-wrapper"));
  assert.equal(app.state.selectedDeviceId, "wr2");

  // Die spaeter eintreffende WR1-Antwort darf den Indikator NICHT erneut
  // einblenden.
  await sleep(DELAYS.wr1);
  assert.equal(
    app.isLoading("#power-chart-wrapper"),
    false,
    "verspaetete WR1-Antwort laesst den Indikator nicht aufflackern"
  );
});
