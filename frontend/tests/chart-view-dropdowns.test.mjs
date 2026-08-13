// Tests fuer die Ansichts-Flyouts in "Verlauf" und "Verbrauch &
// Wechselrichter": statt mehrerer gleichzeitig sichtbarer Diagramme steht
// jeweils nur eines auf dem Bildschirm, gesteuert per Hover-Flyout-Menue am
// jeweiligen Reiter oben (siehe setupViewTabs() in app.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function isHidden(document, id) {
  return document.getElementById(id).classList.contains("hidden");
}

test("Verlauf-Tab zeigt per Default den Leistungsverlauf, nicht den Tagesvergleich", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("trend");
  await waitFor(() => app.state.tabsLoaded.has("trend"));

  assert.equal(isHidden(app.document, "trend-view-power"), false);
  assert.equal(isHidden(app.document, "trend-view-daycompare"), true);
});

test("Verlauf-Tab: Flyout wechselt zwischen Leistungsverlauf und Tagesvergleich, nie beide gleichzeitig", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("trend");
  await waitFor(() => app.state.tabsLoaded.has("trend"));

  app.clickSubview("trend", "pv");
  await waitFor(() => isHidden(app.document, "trend-view-power") === true);

  assert.equal(isHidden(app.document, "trend-view-power"), true);
  assert.equal(isHidden(app.document, "trend-view-daycompare"), false);
  assert.equal(app.state.dayCompare.metric, "pv");

  app.clickSubview("trend", "solar_battery");
  await waitFor(() => app.state.dayCompare.metric === "solar_battery");
  assert.equal(isHidden(app.document, "trend-view-power"), true);
  assert.equal(isHidden(app.document, "trend-view-daycompare"), false);

  app.clickSubview("trend", "power");
  await waitFor(() => isHidden(app.document, "trend-view-power") === false);
  assert.equal(isHidden(app.document, "trend-view-daycompare"), true);
});

test("Verbrauch-Tab zeigt per Default den Tagesverbrauch, nicht den Wechselrichter-Vergleich", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("consumption");
  await waitFor(() => app.state.tabsLoaded.has("consumption"));

  assert.equal(isHidden(app.document, "consumption-view-dailytotals"), false);
  assert.equal(isHidden(app.document, "hourly-section"), true);
});

test("Verbrauch-Tab: Flyout zeigt Wechselrichter-Vergleich nur in 'Alle (Summe)'", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("consumption");
  await waitFor(() => app.state.tabsLoaded.has("consumption"));

  app.clickSubview("consumption", "hourly");
  await waitFor(() => isHidden(app.document, "hourly-section") === false);

  assert.equal(isHidden(app.document, "consumption-view-dailytotals"), true);
  assert.equal(isHidden(app.document, "hourly-section"), false);
  assert.equal(isHidden(app.document, "hourly-chart-content"), false);
  assert.equal(isHidden(app.document, "hourly-chart-unavailable"), true);

  // Einzelnen Wechselrichter waehlen: der Vergleich ergibt keinen Sinn mehr -
  // die Sektion bleibt (Flyout zeigt weiter "Wechselrichter-Vergleich"),
  // aber statt des Diagramms erscheint der Hinweistext.
  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(() => isHidden(app.document, "hourly-chart-unavailable") === false);
  assert.equal(isHidden(app.document, "hourly-chart-content"), true);
  assert.equal(isHidden(app.document, "hourly-section"), false);
});
