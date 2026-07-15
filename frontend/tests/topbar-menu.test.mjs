// Auf schmalen Bildschirmen wandern die Topbar-Buttons in ein aufklappbares
// Menue (verhindert horizontalen Overflow). Der Toggle oeffnet/schliesst es.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

test("Topbar-Menue laesst sich oeffnen und schliessen", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.document.getElementById("menu-toggle"));
  const toggle = app.document.getElementById("menu-toggle");
  const menu = app.document.getElementById("topbar-actions");

  const click = () => toggle.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));

  click();
  assert.ok(menu.classList.contains("open"), "erster Klick oeffnet das Menue");
  assert.equal(toggle.getAttribute("aria-expanded"), "true");

  click();
  assert.ok(!menu.classList.contains("open"), "zweiter Klick schliesst das Menue");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
});
