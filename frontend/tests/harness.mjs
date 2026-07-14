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
        return { devices: [], buckets: [] };
      case "/api/readings/feed-in-summary":
        return { periods: [] };
      case "/api/readings/pv-yield-summary":
        return { periods: [] };
      default:
        return {};
    }
  };
}

export async function bootApp({ fetchHandler }) {
  const html = readFileSync(join(FRONTEND_DIR, "index.html"), "utf8");
  const appjs = readFileSync(join(FRONTEND_DIR, "app.js"), "utf8");

  const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  const { document } = window;

  // Chart.js, Canvas und location stubben.
  window.Chart = class {
    constructor(_ctx, config) {
      this.data = config.data;
    }
    update() {}
    destroy() {}
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

  window.fetch = async (input) => {
    const url = new URL(input, "http://localhost");
    const body = await fetchHandler(url);
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

  const loadingCount = () => document.querySelectorAll(".is-loading").length;

  // Letzter Y-Wert einer Kurve im Hauptdiagramm (Punkte sind im Tagesmodus
  // {x, y}-Objekte, sonst reine Zahlen).
  function chartMetricLast(label) {
    const dataset = state.chart?.data?.datasets?.find((d) => d.label === label);
    if (!dataset || dataset.data.length === 0) return null;
    const last = dataset.data[dataset.data.length - 1];
    return last && typeof last === "object" ? last.y : last;
  }

  return { dom, window, document, state, clickTab, loadingCount, chartMetricLast };
}
