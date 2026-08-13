// Tests fuer das Ansichts-Dropdown im Prognose-Tab: Tagesuebersicht,
// stuendliche Prognose heute, Wochenverlauf und Prognosekontrolle sind
// getrennte Ansichten - immer nur eine gleichzeitig sichtbar.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function isHidden(document, id) {
  return document.getElementById(id).classList.contains("hidden");
}

test("Prognose-Tab zeigt per Default die Tagesuebersicht", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  assert.equal(isHidden(app.document, "forecast-days"), false);
  assert.equal(isHidden(app.document, "forecast-view-hours-today"), true);
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);
});

test("Prognose-Tab: Dropdown schaltet zwischen allen vier Ansichten um, nie mehrere gleichzeitig", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  const select = app.document.getElementById("forecast-view-select");

  select.value = "hours-today";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  assert.equal(isHidden(app.document, "forecast-days"), true);
  assert.equal(isHidden(app.document, "forecast-view-hours-today"), false);
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);

  select.value = "week-chart";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  assert.equal(isHidden(app.document, "forecast-view-hours-today"), true);
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), false);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);

  select.value = "accuracy";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), false);

  select.value = "days";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  assert.equal(isHidden(app.document, "forecast-days"), false);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);
});

test("Kopfzeile (Titel + Status) im Prognose-Tab bleibt unabhaengig von der gewaehlten Ansicht sichtbar", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  const select = app.document.getElementById("forecast-view-select");
  select.value = "accuracy";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));

  assert.equal(isHidden(app.document, "forecast-section"), false);
  assert.ok(app.document.getElementById("forecast-status").textContent.length > 0);
});
