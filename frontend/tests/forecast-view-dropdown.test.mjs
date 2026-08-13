// Tests fuer das Ansichts-Flyout im Prognose-Tab: Tagesuebersicht,
// stuendliche Prognose heute, Gestern, Wochenverlauf und Prognosekontrolle
// sind getrennte Ansichten - immer nur eine gleichzeitig sichtbar,
// gesteuert ueber das Hover-Flyout-Menue am "Prognose"-Reiter (siehe
// setupViewTabs() in app.js).
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
  assert.equal(isHidden(app.document, "forecast-view-yesterday"), true);
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);
});

test("Prognose-Tab: Flyout schaltet zwischen allen fuenf Ansichten um, nie mehrere gleichzeitig", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  app.clickSubview("forecast", "hours-today");
  assert.equal(isHidden(app.document, "forecast-days"), true);
  assert.equal(isHidden(app.document, "forecast-view-hours-today"), false);
  assert.equal(isHidden(app.document, "forecast-view-yesterday"), true);
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);

  app.clickSubview("forecast", "yesterday");
  assert.equal(isHidden(app.document, "forecast-view-hours-today"), true);
  assert.equal(isHidden(app.document, "forecast-view-yesterday"), false);
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);

  app.clickSubview("forecast", "week-chart");
  assert.equal(isHidden(app.document, "forecast-view-yesterday"), true);
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), false);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);

  app.clickSubview("forecast", "accuracy");
  assert.equal(isHidden(app.document, "forecast-view-week-chart"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), false);

  app.clickSubview("forecast", "days");
  assert.equal(isHidden(app.document, "forecast-days"), false);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);
});

test("Kopfzeile (Titel + Status) im Prognose-Tab bleibt unabhaengig von der gewaehlten Ansicht sichtbar", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  app.clickSubview("forecast", "accuracy");

  assert.equal(isHidden(app.document, "forecast-section"), false);
  assert.ok(app.document.getElementById("forecast-status").textContent.length > 0);
});
