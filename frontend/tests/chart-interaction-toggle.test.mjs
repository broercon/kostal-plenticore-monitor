// Umschalter fuer die Diagramm-Interaktion (Tooltip + Zoom/Pan). In jsdom
// gibt es kein Touch-Geraet -> Standard ist "an"; ein Klick schaltet um.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

test("Diagramm-Interaktion ist umschaltbar (Default am Desktop: an)", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  // init() setzt den Default (kein Touch -> an) am Ende via setChartsInteractive.
  await waitFor(() => app.document.body.classList.contains("charts-interactive"));

  assert.equal(app.state.chartsInteractive, true);

  const btn = app.document.querySelector(".chart-interaction-toggle");
  assert.match(btn.textContent, /an$/);

  btn.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));
  assert.equal(app.state.chartsInteractive, false);
  assert.ok(!app.document.body.classList.contains("charts-interactive"));
  assert.match(btn.textContent, /aus$/);
});
