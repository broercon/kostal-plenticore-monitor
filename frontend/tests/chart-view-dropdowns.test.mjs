// Tests fuer die Ansichts-Dropdowns in "Verlauf" und "Verbrauch &
// Wechselrichter": statt mehrerer gleichzeitig sichtbarer Diagramme steht
// jeweils nur eines auf dem Bildschirm, gesteuert per <select>.
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

test("Verlauf-Tab: Dropdown wechselt zwischen Leistungsverlauf und Tagesvergleich, nie beide gleichzeitig", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("trend");
  await waitFor(() => app.state.tabsLoaded.has("trend"));

  const select = app.document.getElementById("trend-view-select");
  select.value = "pv";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  await waitFor(() => isHidden(app.document, "trend-view-power") === true);

  assert.equal(isHidden(app.document, "trend-view-power"), true);
  assert.equal(isHidden(app.document, "trend-view-daycompare"), false);
  assert.equal(app.state.dayCompare.metric, "pv");

  select.value = "solar_battery";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  await waitFor(() => app.state.dayCompare.metric === "solar_battery");
  assert.equal(isHidden(app.document, "trend-view-power"), true);
  assert.equal(isHidden(app.document, "trend-view-daycompare"), false);

  select.value = "power";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));
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

test("Verbrauch-Tab: Dropdown zeigt Wechselrichter-Vergleich nur in 'Alle (Summe)'", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("consumption");
  await waitFor(() => app.state.tabsLoaded.has("consumption"));

  const select = app.document.getElementById("consumption-view-select");
  select.value = "hourly";
  select.dispatchEvent(new app.window.Event("change", { bubbles: true }));
  await waitFor(() => isHidden(app.document, "hourly-section") === false);

  assert.equal(isHidden(app.document, "consumption-view-dailytotals"), true);
  assert.equal(isHidden(app.document, "hourly-section"), false);
  assert.equal(isHidden(app.document, "hourly-chart-content"), false);
  assert.equal(isHidden(app.document, "hourly-chart-unavailable"), true);

  // Einzelnen Wechselrichter waehlen: der Vergleich ergibt keinen Sinn mehr -
  // die Sektion bleibt (Dropdown zeigt weiter "Wechselrichter-Vergleich"),
  // aber statt des Diagramms erscheint der Hinweistext.
  app.clickTab("WR1");
  await waitFor(() => app.state.selectedDeviceId === "wr1");
  await waitFor(() => isHidden(app.document, "hourly-chart-unavailable") === false);
  assert.equal(isHidden(app.document, "hourly-chart-content"), true);
  assert.equal(isHidden(app.document, "hourly-section"), false);
});
