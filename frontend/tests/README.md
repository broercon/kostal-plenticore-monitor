# Frontend-Tests

Kleine, framework-freie Tests fuer das Dashboard-JavaScript (`../app.js`),
ausgefuehrt mit dem eingebauten Test-Runner von Node (`node:test`) und
[jsdom](https://github.com/jsdom/jsdom) als DOM-/Browser-Ersatz.

## Ausfuehren

```bash
cd frontend/tests
npm install
npm test
```

Die Tests laden `../index.html` und `../app.js` in eine jsdom-Umgebung und
mocken das Backend (`fetch`), Chart.js und `<canvas>`. Siehe `harness.mjs`
fuer den gemeinsamen Aufbau.
