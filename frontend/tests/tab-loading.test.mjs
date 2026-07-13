// Tests fuer den Ladeindikator beim Tab-Wechsel (Feature-Branch
// feature/tab-wechsel-ladeindikator).
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

test("Ladeindikator erscheint beim Tab-Wechsel und verschwindet nach dem Laden", async () => {
  const app = await bootApp({ fetchHandler: backend() });

  // Nach dem initialen Laden ist kein Indikator mehr aktiv.
  await waitFor(() => app.loadingCount() === 0);
  assert.equal(app.loadingCount(), 0, "vor dem Klick kein Ladeindikator");

  app.clickTab("WR1");
  // Der Indikator wird synchron in refreshAll() vor dem ersten await gesetzt.
  assert.ok(app.loadingCount() > 0, "direkt nach dem Klick ist der Indikator sichtbar");

  await waitFor(() => app.loadingCount() === 0);
  assert.equal(app.loadingCount(), 0, "nach dem Laden ist der Indikator wieder weg");
});

test("bei schnellem Tab-Wechsel bleibt der Indikator, bis die zuletzt gewaehlte Ansicht geladen ist", async () => {
  const app = await bootApp({ fetchHandler: backend() });
  await waitFor(() => app.loadingCount() === 0);

  // Schnell WR1 -> WR2: WR1 ist langsamer und trifft spaeter ein.
  app.clickTab("WR1");
  app.clickTab("WR2");
  assert.ok(app.loadingCount() > 0, "direkt nach dem Schnellklick ist der Indikator sichtbar");

  // Sobald WR2 (der letzte Klick) geladen ist, verschwindet der Indikator.
  await waitFor(() => app.loadingCount() === 0);
  assert.equal(app.state.selectedDeviceId, "wr2");

  // Die spaeter eintreffende WR1-Antwort darf den Indikator NICHT erneut
  // einblenden.
  await sleep(DELAYS.wr1);
  assert.equal(app.loadingCount(), 0, "verspaetete WR1-Antwort laesst den Indikator nicht aufflackern");
});
