// Der Admin-Button (und damit die Admin-Seite) ist fuer Nicht-Admins in der
// UI ausgeblendet. Die eigentliche Absicherung liegt serverseitig
// (require_admin, siehe backend/tests) - das hier prueft nur die UI-Gating.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

test("Admin-Button ist fuer Nicht-Admins ausgeblendet", async () => {
  // makeBackend liefert /api/auth/me mit role "user" (kein Admin).
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.document.querySelectorAll("#device-tabs button").length > 0);
  const btn = app.document.getElementById("admin-area-btn");
  assert.ok(btn.classList.contains("hidden"), "Admin-Button muss versteckt sein");
});

test("PV-Felder werden im Admin-Bereich je Wechselrichter getrennt dargestellt", async () => {
  const base = makeBackend();
  let savedPayload = null;
  const app = await bootApp({
    fetchHandler: async (url, options = {}) => {
      if (url.pathname === "/api/auth/me") {
        return { id: 1, username: "admin", role: "admin", must_change_password: false };
      }
      if (url.pathname === "/api/admin/forecast/config") {
        if (options.method === "PUT") {
          savedPayload = JSON.parse(options.body);
          return {
            ...savedPayload,
            source: "database",
            arrays: savedPayload.arrays.map((array, index) => ({
              ...array,
              id: index + 1,
              device_name: array.device_id === "wr1" ? "WR1" : "WR2",
              effective_peak_power_kwp:
                array.peak_power_kwp ?? array.module_count * array.module_power_wp / 1000,
            })),
          };
        }
        return {
          enabled: true,
          location_name: "Beispielstandort",
          latitude: 51.1,
          longitude: 7.2,
          forecast_days: 7,
          system_loss_percent: 14,
          source: "database",
          arrays: [
            {
              id: 1,
              device_id: "wr1",
              device_name: "WR1",
              name: "Sued",
              module_count: 20,
              module_power_wp: 430,
              peak_power_kwp: null,
              effective_peak_power_kwp: 8.6,
              tilt_degrees: 35,
              azimuth_degrees: 0,
              inverter_limit_kw: 8,
              enabled: true,
            },
            {
              id: 2,
              device_id: "wr2",
              device_name: "WR2",
              name: "West",
              module_count: null,
              module_power_wp: null,
              peak_power_kwp: 5.2,
              effective_peak_power_kwp: 5.2,
              tilt_degrees: 20,
              azimuth_degrees: 90,
              inverter_limit_kw: null,
              enabled: true,
            },
          ],
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
      return base(url);
    },
  });

  app.document.getElementById("admin-area-btn").click();
  await waitFor(() => app.document.querySelectorAll(".forecast-array").length === 2);

  const rows = [...app.document.querySelectorAll(".forecast-array")];
  assert.deepEqual(rows.map((row) => row.dataset.deviceId), ["wr1", "wr2"]);
  assert.equal(
    rows[0].querySelector('[data-field="name"]').value,
    "Sued"
  );
  assert.match(rows[0].querySelector(".forecast-effective").textContent, /8\.600 kWp/);

  const secondDevice = [...app.document.querySelectorAll(".forecast-device")]
    .find((node) => node.querySelector("h4").textContent.includes("WR2"));
  secondDevice.querySelector("button").click();
  assert.equal(secondDevice.querySelectorAll(".forecast-array").length, 2);

  const added = secondDevice.querySelectorAll(".forecast-array")[1];
  added.querySelector('[data-field="peak_power_kwp"]').value = "3.4";
  app.document.getElementById("forecast-config-form").dispatchEvent(
    new app.window.Event("submit", { bubbles: true, cancelable: true })
  );
  await waitFor(() => savedPayload !== null);
  assert.deepEqual(savedPayload.arrays.map((array) => array.device_id), ["wr1", "wr2", "wr2"]);
  assert.equal(savedPayload.arrays[2].peak_power_kwp, 3.4);
});
