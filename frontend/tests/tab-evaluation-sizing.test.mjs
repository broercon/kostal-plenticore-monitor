// Tests fuer "Auswertungen > Speicher-Erweiterung" (siehe app.js
// refreshBatterySizing, Backend /api/readings/battery-sizing).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bootApp, makeBackend, waitFor } from "./harness.mjs";

function bucket(overrides = {}) {
  return {
    days: 30,
    complete_days: 30,
    base_grid_kwh: 40,
    grid_days_over_threshold: 3,
    avoided_kwh: [6, 9.5],
    feed_in_reduction_kwh: [6.5, 10.2],
    usage: [
      { unused: 10, partial: 15, full: 4, multi: 1 },
      { unused: 12, partial: 16, full: 2, multi: 0 },
    ],
    ...overrides,
  };
}

function sizingPayload(overrides = {}) {
  return {
    variants: [
      { key: "base", modules: 4, extra_modules: 0, nominal_kwh: 11.04 },
      { key: "plus1", modules: 5, extra_modules: 1, nominal_kwh: 13.8 },
      { key: "plus2", modules: 6, extra_modules: 2, nominal_kwh: 16.56 },
    ],
    calibration: {
      capacity_kwh: 11.37,
      state_of_health_percent: 103,
      min_soc_percent: 10,
      max_charge_w: 6600,
      max_discharge_w: 5000,
      eta_charge: 0.967,
      eta_discharge: 0.967,
      standby_w: 10,
      sources: { capacity: "measured", min_soc: "measured", eta_charge: "measured", max_charge: "measured", standby: "default" },
      regression_windows: 784,
    },
    coverage: {
      first_day: "2026-09-01",
      last_day: "2026-10-24",
      days: 54,
      complete_days: 53,
      missing_months: ["2025-11", "2025-12"],
      full_year: false,
    },
    curtailment: { limit_w: null, limit_source: null, hours_at_limit: 0, days_at_limit: 0, max_feed_in_w: 22000, suspected: false },
    validation: {
      soc_points: 30000,
      soc_mean_error: 0.1,
      soc_rmse: 1.3,
      grid_days: 50,
      grid_measured_kwh: 13.3,
      grid_simulated_kwh: 10,
      grid_deviation_percent: -24.5,
      grid_deviation_kwh: -3.3,
      grid_deviation_kwh_per_day: -0.07,
    },
    financial: null,
    total: bucket({ days: 54, complete_days: 53 }),
    last_365_days: bucket({ days: 54, complete_days: 53 }),
    months: [
      { month: "2026-09", ...bucket({ avoided_kwh: [0, 0] }) },
      { month: "2026-10", ...bucket({ days: 24, complete_days: 23 }) },
    ],
    daily: [],
    extra_usable_kwh: [2.4, 4.8],
    grid_charge_kwh: 0.5,
    grid_charge_days: 1,
    grid_day_threshold_kwh: 0.5,
    assumptions: ["Annahme A", "Annahme B"],
    ...overrides,
  };
}

function backendWith(payload) {
  const base = makeBackend();
  return async (url, options) => {
    if (url.pathname === "/api/readings/battery-sizing") return payload;
    return base(url, options);
  };
}

async function openSizing(payload = sizingPayload()) {
  const app = await bootApp({ fetchHandler: backendWith(payload) });
  app.clickViewTab("evaluation");
  app.clickSubview("evaluation", "sizing");
  return app;
}

test("Speicher-Erweiterung: eigene Unteransicht im Auswertungen-Tab", async () => {
  const app = await openSizing();
  await waitFor(() => !!app.state.batterySizing.chart);
  assert.equal(app.state.subView.evaluation, "sizing");
  assert.ok(!app.document.getElementById("evaluation-view-sizing").classList.contains("hidden"));
  assert.ok(app.document.getElementById("evaluation-view-pvdaily").classList.contains("hidden"));
});

test("Speicher-Erweiterung: zwei Saeulen je Monat, 12 Monate, Monate ohne Daten leer", async () => {
  const app = await openSizing();
  await waitFor(() => !!app.state.batterySizing.chart);
  const chart = app.state.batterySizing.chart;
  assert.equal(chart.type, "bar");
  assert.equal(chart.data.labels.length, 12);
  assert.equal(chart.data.labels[0], "Nov 2025");
  assert.equal(chart.data.labels.at(-1), "Okt 2026");
  assert.deepEqual(chart.data.datasets.map((d) => d.label), ["+1 Modul (13.80 kWh)", "+2 Module (16.56 kWh)"]);
  assert.equal(chart.data.datasets[0].data[0], null);
  assert.equal(chart.data.datasets[0].data.at(-1), 6);
  assert.equal(chart.data.datasets[1].data.at(-1), 9.5);
  assert.notEqual(chart.data.datasets[0].backgroundColor, chart.data.datasets[1].backgroundColor);
  // Tooltip-Fusszeile kennt die Tage je Monat.
  const footer = chart.options.plugins.tooltip.callbacks.footer;
  assert.equal(footer([{ dataIndex: 11 }]), "24 Tage mit Daten");
  assert.equal(footer([{ dataIndex: 0 }]), "keine Messdaten");
});

test("Speicher-Erweiterung: Monatstabelle mit +1/+2 und Nutzung, leere Monate markiert", async () => {
  const app = await openSizing();
  await waitFor(() => app.document.querySelectorAll("#sizing-month-body tr").length === 12);
  const rows = [...app.document.querySelectorAll("#sizing-month-body tr")];
  assert.ok(rows[0].classList.contains("sizing-row-empty"));
  assert.match(rows[0].textContent, /keine Daten/);
  const october = rows.at(-1).textContent;
  assert.match(october, /Okt 2026/);
  assert.match(october, /24 \(23 vollständig\)/);
  assert.match(october, /6\.0 kWh.*9\.5 kWh/);
  assert.equal(rows.at(-1).children.length, 7);
  assert.equal(rows.at(-1).children[2].textContent, "3");
  assert.equal(rows[0].lastChild.colSpan, 6);
  assert.match(
    app.document.getElementById("sizing-grid-days").textContent,
    /an 3 von 54 Tagen wurden gemessen mehr als 0\.5 kWh aus dem Netz bezogen/
  );
  assert.doesNotMatch(app.document.getElementById("sizing-month-table").textContent, /Zusatznutzen/);
  assert.match(october, /10 ungenutzt · 15 teilweise · 4 vollständig · 1 mehrfach/);
  // Stufen ohne Tage entfallen: Variante +2 hat keinen "mehrfach"-Tag.
  assert.match(rows.at(-1).children[6].textContent, /^12 ungenutzt · 16 teilweise · 2 vollständig$/);

  const usage = [...app.document.querySelectorAll("#sizing-usage-body tr")].map((r) => r.textContent);
  assert.equal(usage.length, 2);
  assert.match(usage[0], /\+1 Modul.*10.*15.*4.*1/);
  assert.match(app.document.getElementById("sizing-extra-capacity").textContent, /2\.40 kWh bei \+1, 4\.80 kWh bei \+2/);
});

test("Speicher-Erweiterung: Kennzahlen, Validierung, Annahmen und offene Finanzdaten", async () => {
  const app = await openSizing();
  await waitFor(() => app.document.querySelectorAll(".sizing-kpi").length === 3);
  const kpis = app.document.getElementById("sizing-kpis").textContent;
  assert.match(kpis, /6\.0 kWh/);
  assert.match(kpis, /9\.5 kWh/);
  assert.doesNotMatch(kpis, /Zusatznutzen/);
  assert.doesNotMatch(app.document.getElementById("evaluation-view-sizing").textContent, /simul/i);
  assert.match(kpis, /bisher, 54 Tage/);
  assert.match(kpis, /15 % des nachgerechneten Netzbezugs/);

  assert.match(app.document.getElementById("sizing-coverage").textContent, /54 Tage \(01\.09\.2026 bis 24\.10\.2026\).*53 vollständig.*Noch kein volles Jahr/);
  // Modellpruefung und Kenngroessen-Kasten gibt es nicht mehr.
  assert.equal(app.document.getElementById("sizing-validation"), null);
  assert.equal(app.document.getElementById("sizing-calibration"), null);
  assert.doesNotMatch(app.document.getElementById("evaluation-view-sizing").textContent, /Modellprüfung|RMSE/);
  assert.deepEqual(
    [...app.document.querySelectorAll("#sizing-assumptions li")].map((li) => li.textContent),
    ["Annahme A", "Annahme B"]
  );
  assert.match(app.document.getElementById("sizing-financial").textContent, /Offen – Strompreis, Einspeisevergütung und Erweiterungskosten/);
});

test("Speicher-Erweiterung: Wirtschaftlichkeit, sobald Preise eingetragen sind", async () => {
  const app = await openSizing(
    sizingPayload({
      financial: {
        price_ct_per_kwh: 32,
        feed_in_ct_per_kwh: 8,
        full_year: false,
        variants: [
          { cost_eur: 1500, savings_eur: 1.4, payback_years: null },
          { cost_eur: null, savings_eur: 2.2, payback_years: null },
        ],
      },
    })
  );
  await waitFor(() => app.document.querySelectorAll("#sizing-financial li").length === 2);
  const text = app.document.getElementById("sizing-financial").textContent;
  assert.match(text, /1500 € Kosten – Amortisation erst mit einem vollen Jahr Daten/);
  assert.match(text, /im bisherigen Zeitraum/);
});

test("Speicher-Erweiterung: ohne Daten keine Fehler", async () => {
  const app = await openSizing({});
  await waitFor(() => app.document.getElementById("sizing-coverage").textContent === "Keine Daten verfügbar.");
  assert.equal(app.state.batterySizing.chart, null);
});

test("Speicher-Erweiterung: Admin-Formular rechnet Wirkungsgrade in Prozent um", async () => {
  const base = backendWith(sizingPayload());
  let saved = null;
  const config = {
    nominal_capacity_kwh: 11.04,
    module_capacity_kwh: 2.76,
    usable_capacity_kwh: null,
    min_soc_percent: null,
    max_charge_w: null,
    max_discharge_w: null,
    inverter_battery_limit_w: null,
    eta_charge: 0.95,
    eta_discharge: null,
    standby_w: null,
    feed_in_limit_w: null,
    cost_plus_one_eur: null,
    cost_plus_two_eur: null,
    price_ct_per_kwh: null,
    feed_in_ct_per_kwh: null,
    source: "default",
  };
  const app = await bootApp({
    fetchHandler: async (url, options = {}) => {
      if (url.pathname === "/api/auth/me") return { id: 1, username: "admin", role: "admin", must_change_password: false };
      if (url.pathname === "/api/admin/battery-sizing/config") {
        if (options.method === "PUT") saved = JSON.parse(options.body);
        return { ...config, ...(saved ?? {}) };
      }
      if (url.pathname === "/api/admin/users") return [];
      if (url.pathname === "/api/admin/import-history/status") return { running: false, last_finished_at: null, results: [] };
      return base(url, options);
    },
  });

  app.document.getElementById("admin-area-btn").click();
  await waitFor(() => app.document.getElementById("bsz-nominal").value === "11.04");
  assert.equal(app.document.getElementById("bsz-eta-charge").value, "95");
  assert.equal(app.document.getElementById("bsz-usable").value, "");

  app.document.getElementById("bsz-eta-discharge").value = "96.5";
  app.document.getElementById("bsz-price").value = "32";
  app.document.getElementById("battery-sizing-form").dispatchEvent(
    new app.window.Event("submit", { bubbles: true, cancelable: true })
  );
  await waitFor(() => saved !== null);
  assert.equal(saved.eta_charge, 0.95);
  assert.equal(saved.eta_discharge, 0.965);
  assert.equal(saved.price_ct_per_kwh, 32);
  assert.equal(saved.usable_capacity_kwh, null);
  assert.equal(saved.module_capacity_kwh, 2.76);
});

test("Speicher-Erweiterung: Netzbezug-Kachel nennt den Zeitraum, nicht 'heute'", async () => {
  const app = await openSizing();
  await waitFor(() => app.document.querySelectorAll(".sizing-kpi").length === 3);
  const tile = app.document.querySelectorAll(".sizing-kpi")[2].textContent;
  assert.match(tile, /Netzbezug ohne Erweiterung/);
  assert.match(tile, /jetzigen Speicher \(11\.04 kWh\)/);
  assert.match(tile, /bisher, 54 Tage/);
  assert.doesNotMatch(tile, /heute/);
});
