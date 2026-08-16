// Test: der Tagesvergleich zeigt beim Start standardmaessig nur einen Tag
// (nicht mehr 7 wie zuvor) - passend zum Leistungsverlauf ("24 Std") und
// zum Wechselrichter-Vergleich ("1 Tag"), siehe CALCULATIONS.md
// "Tagesvergleich: Tage uebereinanderlegen". Weitere Tage bleiben ueber die
// Buttons weiterhin waehlbar.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

test("Tagesvergleich: Standardzeitraum ist 1 Tag", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.loadingCount() === 0);

  assert.equal(app.state.dayCompare.days, 1);

  const activeBtn = app.document.querySelector("#daycompare-day-buttons button.active");
  assert.equal(activeBtn.dataset.days, "1");
});

test("Tagesvergleich: andere Zeitraeume bleiben ueber die Buttons waehlbar", async () => {
  const app = await bootApp({ fetchHandler: makeBackend() });
  await waitFor(() => app.loadingCount() === 0);

  const btn7 = app.document.querySelector('#daycompare-day-buttons button[data-days="7"]');
  btn7.dispatchEvent(new app.window.MouseEvent("click", { bubbles: true }));

  assert.equal(app.state.dayCompare.days, 7);
  assert.ok(btn7.classList.contains("active"));
  assert.equal(
    app.document.querySelector('#daycompare-day-buttons button[data-days="1"]').classList.contains("active"),
    false
  );
});
