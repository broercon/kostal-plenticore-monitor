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
