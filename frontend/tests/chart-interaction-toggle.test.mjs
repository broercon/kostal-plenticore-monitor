// Umschalter fuer die Werte-Anzeige (Tooltip/Hover) der Diagramme. In jsdom
// gibt es kein Touch-Geraet -> Standard ist "an"; ein Klick schaltet um.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

test("Werte-Anzeige der Diagramme ist umschaltbar (Default am Desktop: an)", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  const btn = () => app.document.querySelector(".chart-interaction-toggle");
  // init() setzt den Default (kein Touch -> an) am Ende via setChartsInteractive.
  await waitFor(() => btn() && /an$/.test(btn().textContent));
  assert.equal(app.state.chartsInteractive, true);

  btn().dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  assert.equal(app.state.chartsInteractive, false);
  assert.match(btn().textContent, /aus$/);
});
