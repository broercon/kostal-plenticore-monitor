// Gemeinsamer Test-Aufbau: laedt index.html + app.js in eine jsdom-Umgebung
// und mockt Backend (fetch), Chart.js und <canvas>. Bewusst framework-frei,
// damit die Tests ohne zusaetzliche Build-/Test-Infrastruktur mit dem
// eingebauten Node-Test-Runner (node:test) laufen.
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const FRONTEND_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Wartet, bis eine Bedingung wahr ist (Polling) - fuer asynchrones
// Nachladen, ohne feste, potenziell flakey Wartezeiten.
export async function waitFor(predicate, { timeout = 3000, interval = 10 } = {}) {
  const start = Date.now();
  for (;;) {
    if (predicate()) return;
    if (Date.now() - start > timeout) throw new Error("waitFor: Timeout ueberschritten");
    await sleep(interval);
  }
}

// Standard-Backend mit zwei Wechselrichtern (WR1/WR2). Ueber die Optionen
// laesst sich das Verhalten des /history-Endpunkts je Geraet steuern
// (Verzoegerung + gemeldeter PV-Wert), um Race-/Ladeverhalten zu testen.
export function makeBackend({ historyDelayMs = () => 0, historyPv = () => null } = {}) {
  return async (url) => {
    const path = url.pathname;
    const device = url.searchParams.get("device_id") || "";
    switch (path) {
      case "/api/auth/me":
        return { id: 1, username: "test", role: "user", must_change_password: false };
      case "/api/devices":
        return [
          { id: "wr1", name: "WR1", host: "h1" },
          { id: "wr2", name: "WR2", host: "h2" },
        ];
      case "/api/readings/latest":
        return [];
      case "/api/readings/today-summary":
        return [];
      case "/api/readings/history": {
        await sleep(historyDelayMs(device));
        const pv = historyPv(device);
        if (pv === null) return [];
        return [
          {
            timestamp: "2026-07-13T12:00:00",
            pv_power_w: pv,
            home_power_w: 0,
            feed_in_power_w: 0,
            grid_draw_power_w: 0,
          },
        ];
      }
      case "/api/readings/day-profile":
        return { days: [] };
      case "/api/readings/daily-home-breakdown":
        return { days: [] };
      case "/api/readings/hourly-per-device":
        // Ist-Werte fuer die stuendliche Prognose-Ansicht (Vergleich
        // Prognose vs. echt, siehe app.js refreshForecast()) - die beiden
        // Buckets entsprechen lokal genau den beiden "heutigen" Stunden aus
        // dem /api/forecast-Mock unten (lokale Anlagenzeit, siehe deren
        // local_hour-Felder).
        return {
          devices: [
            { device_id: "wr1", device_name: "WR1" },
            { device_id: "wr2", device_name: "WR2" },
          ],
          buckets: [
            { bucket: "2026-07-13T01:00:00", values: { wr1: 0, wr2: 0 } },
            { bucket: "2026-07-13T14:00:00", values: { wr1: 2.5, wr2: 1.2 } },
          ],
        };
      case "/api/readings/feed-in-summary":
        return { periods: [] };
      case "/api/readings/pv-yield-summary":
        return { periods: [] };
      case "/api/readings/autarky-monthly":
        return { months: [] };
      case "/api/readings/yearly-comparison":
        return { granularity: "month", labels: [], years: [] };
      case "/api/readings/battery-soc-history":
        return { devices: [], points: [] };
      case "/api/forecast":
        return {
          available: true,
          message: "Prognose aus historischen PV- und Wetterdaten.",
          generated_at: "2026-07-13T06:00:00Z",
          training_start: "2026-05-01T00:00:00Z",
          training_end: "2026-07-12T23:00:00Z",
          training_samples: 1200,
          weather_source: "Open-Meteo",
          models: [
            {
              device_id: "wr1",
              device_name: "WR1",
              method: "learned",
              validation_samples: 120,
              validation_error_percent: 8.5,
            },
          ],
          days: [
            {
              date: "2026-07-13",
              expected_kwh: 12.4,
              low_kwh: 10.1,
              high_kwh: 14.8,
              production_start: "2026-07-13T05:00:00Z",
              production_end: "2026-07-13T19:00:00Z",
              peak_at: "2026-07-13T12:00:00Z",
              peak_kw: 4.2,
              devices: [
                {
                  device_id: "wr1",
                  device_name: "WR1",
                  expected_kwh: 8.0,
                  low_kwh: 6.5,
                  high_kwh: 9.4,
                  production_start: "2026-07-13T05:00:00Z",
                  production_end: "2026-07-13T19:00:00Z",
                  peak_at: "2026-07-13T12:00:00Z",
                  peak_kw: 2.7,
                },
                {
                  device_id: "wr2",
                  device_name: "WR2",
                  expected_kwh: 4.4,
                  low_kwh: 3.6,
                  high_kwh: 5.4,
                  production_start: "2026-07-13T05:00:00Z",
                  production_end: "2026-07-13T19:00:00Z",
                  peak_at: "2026-07-13T12:00:00Z",
                  peak_kw: 1.5,
                },
              ],
            },
          ],
          hours: [
            {
              // In UTC noch der Vortag; local_date stammt aus der
              // Anlagen-Zeitzone und muss fuer die Heute-Auswahl gelten.
              timestamp: "2026-07-12T23:00:00Z",
              local_date: "2026-07-13",
              local_hour: "2026-07-13T01:00:00",
              expected_kw: 3.0,
              low_kw: 2.4,
              high_kw: 3.6,
              devices: [
                { device_id: "wr1", device_name: "WR1", expected_kw: 2.0, low_kw: 1.6, high_kw: 2.4 },
                { device_id: "wr2", device_name: "WR2", expected_kw: 1.0, low_kw: 0.8, high_kw: 1.2 },
              ],
            },
            {
              timestamp: "2026-07-13T12:00:00Z",
              local_date: "2026-07-13",
              local_hour: "2026-07-13T14:00:00",
              expected_kw: 4.2,
              low_kw: 3.4,
              high_kw: 5.0,
              devices: [
                { device_id: "wr1", device_name: "WR1", expected_kw: 2.7, low_kw: 2.1, high_kw: 3.2 },
                { device_id: "wr2", device_name: "WR2", expected_kw: 1.5, low_kw: 1.3, high_kw: 1.8 },
              ],
            },
            {
              timestamp: "2026-07-14T12:00:00Z",
              local_date: "2026-07-14",
              local_hour: "2026-07-14T14:00:00",
              expected_kw: 5.0,
              low_kw: 4.0,
              high_kw: 6.0,
              devices: [
                { device_id: "wr1", device_name: "WR1", expected_kw: 3.0, low_kw: 2.4, high_kw: 3.6 },
                { device_id: "wr2", device_name: "WR2", expected_kw: 2.0, low_kw: 1.6, high_kw: 2.4 },
              ],
            },
          ],
        };
      case "/api/forecast/yesterday":
        return {
          available: true,
          message: "Stündlicher Vergleich der gespeicherten Prognosen mit den echten Messwerten für gestern.",
          date: "2026-07-12",
          hours: [
            {
              timestamp: "2026-07-12T05:00:00Z",
              local_hour: "2026-07-12T07:00:00",
              expected_kw: 2.0,
              low_kw: 1.6,
              high_kw: 2.4,
              actual_kw: 2.3,
              devices: [
                {
                  device_id: "wr1",
                  device_name: "WR1",
                  expected_kw: 1.2,
                  low_kw: 1.0,
                  high_kw: 1.4,
                  actual_kw: 1.4,
                },
                {
                  device_id: "wr2",
                  device_name: "WR2",
                  expected_kw: 0.8,
                  low_kw: 0.6,
                  high_kw: 1.0,
                  actual_kw: 0.9,
                },
              ],
            },
            {
              timestamp: "2026-07-12T11:00:00Z",
              local_hour: "2026-07-12T13:00:00",
              expected_kw: 4.5,
              low_kw: 3.8,
              high_kw: 5.2,
              actual_kw: 4.0,
              devices: [
                {
                  device_id: "wr1",
                  device_name: "WR1",
                  expected_kw: 3.0,
                  low_kw: 2.5,
                  high_kw: 3.5,
                  actual_kw: 2.6,
                },
                {
                  device_id: "wr2",
                  device_name: "WR2",
                  expected_kw: 1.5,
                  low_kw: 1.3,
                  high_kw: 1.7,
                  actual_kw: 1.4,
                },
              ],
            },
          ],
        };
      case "/api/forecast/accuracy":
        return {
          available: true,
          message: "Vergleich der gespeicherten Prognosen mit echten Messwerten.",
          overall_accuracy_percent: 92.4,
          today_so_far: {
            date: "2026-07-13",
            expected_kwh: 3.0,
            actual_kwh: 4.5,
            difference_kwh: 1.5,
            difference_percent: 50.0,
            accuracy_percent: 66.7,
            matched_hours: 3,
            devices: [
              {
                device_id: "wr1",
                device_name: "WR1",
                expected_kwh: 2.0,
                actual_kwh: 3.0,
                difference_kwh: 1.0,
                difference_percent: 50.0,
                accuracy_percent: 66.7,
                matched_hours: 3,
              },
              {
                device_id: "wr2",
                device_name: "WR2",
                expected_kwh: 1.0,
                actual_kwh: 1.5,
                difference_kwh: 0.5,
                difference_percent: 50.0,
                accuracy_percent: 66.7,
                matched_hours: 3,
              },
            ],
          },
          days: [
            {
              date: "2026-07-12",
              expected_kwh: 11.5,
              actual_kwh: 12.0,
              difference_kwh: 0.5,
              difference_percent: 4.3,
              accuracy_percent: 95.8,
              matched_hours: 24,
              devices: [
                {
                  device_id: "wr1",
                  device_name: "WR1",
                  expected_kwh: 7.5,
                  actual_kwh: 8.0,
                  difference_kwh: 0.5,
                  difference_percent: 6.7,
                  accuracy_percent: 93.8,
                  matched_hours: 24,
                },
                {
                  device_id: "wr2",
                  device_name: "WR2",
                  expected_kwh: 4.0,
                  actual_kwh: 4.0,
                  difference_kwh: 0.0,
                  difference_percent: 0.0,
                  accuracy_percent: 100.0,
                  matched_hours: 24,
                },
              ],
            },
          ],
        };
      default:
        return {};
    }
  };
}

export async function bootApp({ fetchHandler, touch = false }) {
  const html = readFileSync(join(FRONTEND_DIR, "index.html"), "utf8");
  const appjs = readFileSync(join(FRONTEND_DIR, "app.js"), "utf8");

  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  const { document } = window;

  // jsdom implementiert window.matchMedia von sich aus nicht. Fuer Tests, die
  // das Touch-/Handy-Verhalten pruefen wollen (siehe isTouchDevice() in
  // app.js, Abfrage von "(hover: none), (pointer: coarse)"), simuliert
  // { touch: true } ein Geraet ohne Hover/mit grobem Zeiger - alle anderen
  // Tests laufen weiterhin ohne matchMedia (== Desktop-Zweig), wie bisher.
  if (touch) {
    window.matchMedia = () => ({ matches: true });
  }

  // Chart.js, Canvas und location stubben.
  window.Chart = class {
    constructor(_ctx, config) {
      this.type = config.type;
      this.data = config.data;
      this.options = config.options;
      this.resizeCount = 0;
    }
    update() {}
    destroy() {}
    // Zaehlt Aufrufe mit, damit Tests pruefen koennen, dass ein im
    // Hintergrund (waehrend das Tab-Panel noch "display:none" war)
    // erzeugtes Diagramm beim tatsaechlichen Sichtbarwerden per resize()
    // nachgezogen wird (siehe resizeTabCharts() in app.js).
    resize() {
      this.resizeCount += 1;
    }
  };
  window.HTMLCanvasElement.prototype.getContext = () => ({});
  try {
    Object.defineProperty(window, "location", {
      value: { href: "" },
      writable: true,
      configurable: true,
    });
  } catch {
    /* in manchen jsdom-Versionen bereits konfigurierbar - ignorieren */
  }

  // Periodische Hintergrund-Aktualisierungen im Test abschalten: sonst haelt
  // setInterval den Prozess offen und die Timer stoeren die Assertions.
  window.setInterval = () => 0;

  window.fetch = async (input, options = {}) => {
    const url = new URL(input, "http://localhost");
    const body = await fetchHandler(url, options);
    return { ok: true, status: 200, json: async () => body };
  };

  // app.js exportiert nichts - ueber window.__state kommen wir im Test an
  // den internen Zustand (selectedDeviceId, chart, ...).
  dom.window.eval(appjs + "\nwindow.__state = state;");

  // Warten, bis init() die Geraete-Tabs aufgebaut hat.
  await waitFor(() => document.querySelectorAll("#device-tabs button").length > 0);

  const state = window.__state;

  function clickTab(label) {
    const btn = [...document.querySelectorAll("#device-tabs button")].find(
      (b) => b.textContent === label
    );
    if (!btn) throw new Error(`Tab nicht gefunden: ${label}`);
    btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  }

  // Klickt einen der Ansichts-Tabs (Uebersicht/Verlauf/Verbrauch & WR/
  // Prognose, siehe setupViewTabs() in app.js) - anhand seiner data-tab-ID,
  // nicht des sichtbaren Labels (robuster gegen Textaenderungen).
  function clickViewTab(tabId) {
    const btn = document.querySelector(`#view-tabs button[data-tab="${tabId}"]`);
    if (!btn) throw new Error(`Ansichts-Tab nicht gefunden: ${tabId}`);
    btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  }

  // Klickt einen Eintrag im Hover-Flyout-Menue eines Ansichts-Tabs (ersetzt
  // die frueheren <select>-Dropdowns, siehe setupViewTabs()/
  // SUBVIEW_SETTERS in app.js) - anhand der Tab-Gruppe (data-tab-group,
  // z.B. "trend") und des gewuenschten Unteransicht-Werts (data-subview,
  // z.B. "pv"). Im echten Browser wuerde das Menue erst per Hover
  // sichtbar - im Test wird direkt auf den (im DOM immer vorhandenen)
  // Button geklickt, das reicht fuer den click-Handler in app.js.
  function clickSubview(groupId, subview) {
    const btn = document.querySelector(
      `.view-tab-with-menu[data-tab-group="${groupId}"] button[data-subview="${subview}"]`
    );
    if (!btn) throw new Error(`Unteransicht nicht gefunden: ${groupId}/${subview}`);
    btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  }

  const loadingCount = () => document.querySelectorAll(".is-loading").length;
  const isLoading = (selector) => {
    const node = document.querySelector(selector);
    return !!node && node.classList.contains("is-loading");
  };

  // Letzter Y-Wert einer Kurve im Hauptdiagramm (Punkte sind im Tagesmodus
  // {x, y}-Objekte, sonst reine Zahlen).
  function chartMetricLast(label) {
    const dataset = state.chart?.data?.datasets?.find((d) => d.label === label);
    if (!dataset || dataset.data.length === 0) return null;
    const last = dataset.data[dataset.data.length - 1];
    return last && typeof last === "object" ? last.y : last;
  }

  return {
    dom,
    window,
    document,
    state,
    clickTab,
    clickViewTab,
    clickSubview,
    loadingCount,
    isLoading,
    chartMetricLast,
  };
}
