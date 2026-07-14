// Tests fuer die Sichtbarkeit der Einspeisungs-Leiste: nur im Gesamt-Tab
// ("Alle (Summe)"), nicht fuer einen einzelnen Wechselrichter.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

const isHidden = (document) =>
  document.getElementById("feedin-summary").classList.contains("hidden");

test("Einspeisungs-Leiste ist im Gesamt-Tab sichtbar", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.state.selectedDeviceId === "");
  assert.equal(isHidden(app.document), false, "im Gesamt-Tab sichtbar");
});

test("Einspeisungs-Leiste wird fuer einen einzelnen WR ausgeblendet", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });

  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  assert.equal(isHidden(app.document), true, "im Einzel-WR-Tab ausgeblendet");

  // Zurueck auf Gesamt: wieder sichtbar.
  app.clickTab("Alle (Summe)");
  await waitFor(() => app.state.selectedDeviceId === "");
  assert.equal(isHidden(app.document), false, "zurueck im Gesamt-Tab sichtbar");
});
