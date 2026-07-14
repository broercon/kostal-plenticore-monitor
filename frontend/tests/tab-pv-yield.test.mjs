// Tests fuer die Sichtbarkeit der PV-Ertrag-Leiste: nur im Gesamt-Tab
// ("Alle (Summe)"), nicht fuer einen einzelnen Wechselrichter.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

const isHidden = (document) =>
  document.getElementById("pv-yield-summary").classList.contains("hidden");

test("PV-Ertrag-Leiste ist im Gesamt-Tab sichtbar", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.selectedDeviceId === "");
  assert.equal(isHidden(app.document), false, "im Gesamt-Tab sichtbar");
});

test("PV-Ertrag-Leiste wird fuer einen einzelnen WR ausgeblendet", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  assert.equal(isHidden(app.document), true, "im Einzel-WR-Tab ausgeblendet");

  // Zurueck auf Gesamt: wieder sichtbar.
  app.clickTab("Alle (Summe)");
  await waitFor(() => app.state.selectedDeviceId === "");
  assert.equal(isHidden(app.document), false, "zurueck im Gesamt-Tab sichtbar");
});
