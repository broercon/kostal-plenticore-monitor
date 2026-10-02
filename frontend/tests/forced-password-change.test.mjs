import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor, sleep } from "./harness.mjs";

test("Bei must_change_password: Passwort-Dialog statt Dashboard, nicht schliessbar", async () => {
  const requested = [];
  const base = makeBackend();
  const app = await bootApp({
    waitForDashboard: false,
    fetchHandler: async (url, options = {}) => {
      requested.push(url.pathname);
      if (url.pathname === "/api/auth/me") {
        return { id: 1, username: "admin", role: "admin", must_change_password: true };
      }
      return base(url, options);
    },
  });

  const overlay = app.document.getElementById("change-password-overlay");
  await waitFor(() => !overlay.classList.contains("hidden"));
  await sleep(100);

  // Das Dashboard laedt keine Daten (der Server wuerde ohnehin 403 liefern).
  assert.deepEqual(requested, ["/api/auth/me"]);

  // Der Dialog laesst sich nicht wegklicken; stattdessen steht dort "Abmelden".
  const cancel = app.document.getElementById("cp-cancel");
  assert.equal(cancel.textContent, "Abmelden");
});
