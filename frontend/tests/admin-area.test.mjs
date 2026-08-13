import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

test("Admin-Button ist fuer Nicht-Admins ausgeblendet", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.document.querySelectorAll("#device-tabs button").length > 0);
  assert.ok(app.document.getElementById("admin-area-btn").classList.contains("hidden"));
});

test("Prognosekonfiguration enthaelt nur Aktivierung und Koordinaten", async () => {
  const base = makeBackend();
  let savedPayload = null;
  const app = await bootApp({
    fetchHandler: async (url, options = {}) => {
      if (url.pathname === "/api/auth/me") {
        return { id: 1, username: "admin", role: "admin", must_change_password: false };
      }
      if (url.pathname === "/api/admin/forecast/config") {
        if (options.method === "PUT") savedPayload = JSON.parse(options.body);
        return {
          enabled: true,
          latitude: savedPayload?.latitude ?? 51.1,
          longitude: savedPayload?.longitude ?? 7.2,
          source: "database",
        };
      }
      if (url.pathname === "/api/admin/users") return [];
      if (url.pathname === "/api/admin/daily-report/config") {
        return {
          enabled: false,
          report_time: "19:00",
          recipients: [],
          mail_service_url: "",
          mail_service_api_key_set: false,
          mail_service_from_name: "",
        };
      }
      if (url.pathname === "/api/admin/daily-report/status") {
        return { enabled: false, scheduled_time: "19:00", recipients: [] };
      }
      if (url.pathname === "/api/admin/import-history/status") {
        return { running: false, last_finished_at: null, results: [] };
      }
      return base(url, options);
    },
  });

  app.document.getElementById("admin-area-btn").click();
  await waitFor(() => app.document.getElementById("fc-latitude").value === "51.1");
  assert.equal(app.document.querySelectorAll("#forecast-config-form input").length, 3);
  assert.equal(app.document.querySelector("#forecast-device-fields"), null);

  app.document.getElementById("fc-latitude").value = "50.5";
  app.document.getElementById("forecast-config-form").dispatchEvent(
    new app.window.Event("submit", { bubbles: true, cancelable: true })
  );
  await waitFor(() => savedPayload !== null);
  assert.deepEqual(savedPayload, { enabled: true, latitude: 50.5, longitude: 7.2 });
});

test("Dashboard zeigt Energie, Zeitraum und Wechselrichter der Prognose", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  // Der Prognose-Tab laedt (wie alle Tabs ausser "Uebersicht") erst beim
  // ersten Oeffnen, siehe setupViewTabs()/TAB_LOADERS in app.js.
  app.clickViewTab("forecast");
  await waitFor(() => app.document.querySelectorAll(".forecast-day").length === 1);
  const text = app.document.getElementById("forecast-days").textContent;
  assert.match(text, /12\.4 kWh/);
  assert.match(text, /WR1: 8\.0 kWh/);
  assert.match(text, /WR2: 4\.4 kWh/);
  assert.match(app.document.getElementById("forecast-status").textContent, /gelernt/);
  assert.ok(app.state.forecastChart);
});

test("Dashboard vergleicht gespeicherte Prognose mit echten Werten", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  app.clickViewTab("forecast");
  // ".forecast-accuracy-day" kommt jetzt auch fuer die separate "Heute
  // (bisher)"-Karte vor (#forecast-accuracy-today, siehe refreshForecast
  // Accuracy()) - hier gezielt nur die abgeschlossenen Tage im eigentlichen
  // Kacheln-Container zaehlen.
  await waitFor(
    () => app.document.querySelectorAll("#forecast-accuracy-days .forecast-accuracy-day").length === 1
  );
  const text = app.document.getElementById("forecast-accuracy-days").textContent;
  assert.match(text, /Erwartet 11\.5/);
  assert.match(text, /tatsächlich 12\.0 kWh/);
  assert.match(text, /Abweichung \+0\.5 kWh/);
  assert.match(text, /WR1: 7\.5 → 8\.0 kWh/);
  assert.ok(app.state.forecastAccuracyChart);
});
