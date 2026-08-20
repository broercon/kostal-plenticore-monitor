// Tests fuer das Ansichts-Flyout im Prognose-Tab: Tagesuebersicht (Wochen-
// verlauf-Diagramm + Tageswerte in einer Ansicht, Diagramm zuerst),
// stuendliche Prognose heute, Gestern und Prognosekontrolle sind getrennte
// Ansichten - immer nur eine gleichzeitig sichtbar, gesteuert ueber das
// Hover-Flyout-Menue am "Prognose"-Reiter (siehe setupViewTabs() in app.js).
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

  assert.equal(isHidden(app.document, "forecast-view-days"), false);
  assert.equal(isHidden(app.document, "forecast-view-hours-today"), true);
  assert.equal(isHidden(app.document, "forecast-view-yesterday"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);
});

test("Prognose-Tab: Flyout schaltet zwischen allen vier Ansichten um, nie mehrere gleichzeitig", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  app.clickSubview("forecast", "hours-today");
  assert.equal(isHidden(app.document, "forecast-view-days"), true);
  assert.equal(isHidden(app.document, "forecast-view-hours-today"), false);
  assert.equal(isHidden(app.document, "forecast-view-yesterday"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);

  app.clickSubview("forecast", "yesterday");
  assert.equal(isHidden(app.document, "forecast-view-hours-today"), true);
  assert.equal(isHidden(app.document, "forecast-view-yesterday"), false);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);

  app.clickSubview("forecast", "accuracy");
  assert.equal(isHidden(app.document, "forecast-view-yesterday"), true);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), false);

  app.clickSubview("forecast", "days");
  assert.equal(isHidden(app.document, "forecast-view-days"), false);
  assert.equal(isHidden(app.document, "forecast-accuracy-section"), true);
});

test("Prognose-Tab: Tagesuebersicht zeigt zuerst das Wochenverlauf-Diagramm, danach die Tageswerte", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  const container = app.document.getElementById("forecast-view-days");
  const chartWrapper = app.document.getElementById("forecast-chart-wrapper");
  const days = app.document.getElementById("forecast-days");
  const children = [...container.children];

  assert.ok(children.includes(chartWrapper));
  assert.ok(children.includes(days));
  assert.ok(
    children.indexOf(chartWrapper) < children.indexOf(days),
    "Diagramm muss vor den Tageswerten stehen"
  );
});

test("Prognose-Tab: Flyout laesst sich auf einem Touch-Geraet nach Auswahl einer Unteransicht erneut oeffnen", async () => {
  // Regression fuer einen Bug auf dem Handy: suppressMenuUntilLeave()
  // (app.js) blendet das Flyout nach Auswahl einer Unteransicht per
  // .menu-suppress aus und verlaesst sich darauf, dass ein "mouseleave"
  // dieses Flag wieder entfernt - auf einem Touch-Geraet feuert dieses
  // Event nie, .menu-suppress blieb also (per !important staerker als
  // .menu-open) dauerhaft bestehen und das Flyout liess sich danach nie
  // wieder oeffnen, egal wie oft man auf den Reiter tippte.
  const app = await bootApp({ fetchHandler: makeBackend(), touch: true });
  const wrapper = app.document.querySelector(
    '.view-tab-with-menu[data-tab-group="forecast"]'
  );

  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));
  assert.ok(wrapper.classList.contains("menu-open"), "Tap auf den Reiter oeffnet das Flyout");

  app.clickSubview("forecast", "yesterday");
  assert.ok(!wrapper.classList.contains("menu-open"));
  assert.ok(
    wrapper.classList.contains("menu-suppress"),
    "Auswahl einer Unteransicht setzt menu-suppress"
  );

  app.clickViewTab("forecast");
  assert.ok(
    wrapper.classList.contains("menu-open"),
    "Erneuter Tap auf den Reiter muss das Flyout wieder oeffnen"
  );
  assert.ok(
    !wrapper.classList.contains("menu-suppress"),
    "menu-suppress darf ein erneutes Oeffnen per Tap nicht mehr blockieren"
  );
});


test("Kopfzeile (Titel + Status) im Prognose-Tab bleibt unabhaengig von der gewaehlten Ansicht sichtbar", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  await waitFor(() => app.state.tabsLoaded.has("forecast"));

  app.clickSubview("forecast", "accuracy");

  assert.equal(isHidden(app.document, "forecast-section"), false);
  assert.ok(app.document.getElementById("forecast-status").textContent.length > 0);
});
