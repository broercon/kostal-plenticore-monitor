# Entwicklung

[Zurück zum README](../README.md)

## Architektur

- **Backend**: Python + FastAPI. Ein Hintergrund-Task fragt die konfigurierten
  Wechselrichter über die REST-API (via [pykoplenti](https://github.com/stegm/pykoplenti))
  in einem festen Intervall ab und schreibt jeden Messwert in SQLite.
- **Frontend**: statisches HTML/JS-Dashboard (Chart.js), wird direkt vom
  Backend mit ausgeliefert – kein separater Webserver nötig.
- **Datenbank**: SQLite-Datei, per Docker-Volume persistiert.
- Alles läuft in einem einzigen Container über Docker Compose.

## Tests

Das Projekt hat zwei getrennte, unabhängig lauffähige Test-Suites: die
Backend-Tests (Python/pytest) und die Frontend-Tests (JavaScript/jsdom).

### Backend-Tests

Die Benutzerverwaltung (Login, Rollen, Passwort-Änderung, Session-Handling)
sowie die Update-Sicherheit für Bestandsdaten sind mit automatisierten
Tests abgedeckt (`backend/tests/`, pytest + FastAPI TestClient – echte
HTTP-Requests gegen die App inkl. Cookies, nicht nur isolierte
Funktionsaufrufe). Lokal ausführen:

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

Die Tests laufen gegen eine temporäre, isolierte SQLite-Datenbank (nicht
gegen `data/kostal.db`) und starten bewusst keinen echten Poller/Import
gegen einen Wechselrichter. Abgedeckt sind u.a.: Standard-Nutzer werden nur
einmal angelegt, falsches/unbekanntes Passwort wird abgelehnt, erfolgreicher
Login setzt ein Cookie und schaltet die API frei, Logout invalidiert die
Sitzung, eigenes Passwort ändern (inkl. Ablehnung bei falschem aktuellem
Passwort), Admin-Endpunkte sind für die Rolle betreiber gesperrt (403),
Admin kann Nutzer auflisten und deren Passwort zurücksetzen, sowie: ein
`init_db()`-Lauf auf einer Bestandsdatenbank (nur `readings`-Tabelle,
noch ohne Benutzerverwaltung) ergänzt lediglich die fehlenden Tabellen und
lässt vorhandene Messwerte unverändert, sowie: der Poller bricht bei einem
unerwarteten Fehlertyp eines einzelnen Geräts nicht komplett ab (siehe
[Betrieb und Fehlerdiagnose](OPERATIONS.md#polling-stoppt-nachts--zu-einer-bestimmten-uhrzeit)), und die korrigierte
Energiebilanz-Berechnung bei mehreren Wechselrichtern (siehe
[Berechnungen bei mehreren Wechselrichtern](CALCULATIONS.md#mehrere-wechselrichter-hausverbrauchnetz-korrekt-berechnen)) ist sowohl auf
Ebene der Aggregations-Funktionen als auch End-to-End über die echten API-
Endpunkte getestet, anhand echter, per `debug_live.py` ausgelesener
Rohwerte.

Der [tägliche Mail-Report](INSTALLATION.md#täglicher-mail-report) ist ebenfalls End-to-End getestet:
Berechnung des nächsten Sendezeitpunkts, "aktiv/erreichbar"-Status je
Wechselrichter, Text-Format der Mail, dass ein fehlgeschlagener Mailversand
abgefangen wird statt die App zu beeinträchtigen, sowie die komplett über
die Datenbank editierbare Konfiguration (Persistenz, Umgebungsvariablen nur
als Fallback, der Mail-Service-API-Key wird nie im Klartext an das
Frontend zurückgegeben) inklusive der zugehörigen Admin-Endpunkte.

### Frontend-Tests

Das Dashboard-JavaScript (`frontend/app.js`) ist mit leichtgewichtigen,
framework-freien Tests abgedeckt: dem eingebauten Test-Runner von Node
(`node:test`) und [jsdom](https://github.com/jsdom/jsdom) als DOM-Ersatz.
Die Tests laden `frontend/index.html` und `frontend/app.js` in eine
jsdom-Umgebung und mocken Backend (`fetch`), Chart.js und `<canvas>` – es
wird also kein laufender Server und kein echter Browser benötigt. Lokal
ausführen (Node 18+ erforderlich):

```bash
cd frontend/tests
npm ci
npm test
```

## Continuous Integration

Der Workflow `.github/workflows/ci.yml` führt bei Pull Requests sowie bei
Pushes auf `main` und `codex/**` drei unabhängige Checks aus:

- Backend-Tests mit Python 3.12
- Frontend-Tests mit Node.js 20
- Build des Docker-Images

## Abgedeckte Frontend-Fälle

Abgedeckt ist u.a. das Verhalten beim Wechsel der Wechselrichter-Tabs
(WR1/WR2/„Alle"): dass die Anzeige die Daten des gewählten Geräts lädt,
dass bei schnellem Wechsel eine verspätet eintreffende Antwort eines
vorher gewählten Geräts die Anzeige nicht überschreibt (Race Condition),
und dass währenddessen ein Ladeindikator sichtbar ist. Der gemeinsame
Aufbau (jsdom + Backend-Mock) steckt in `frontend/tests/harness.mjs`.

## Grenzen / mögliche Erweiterungen

- Aktuell wird nur eine feste Auswahl an Prozessdaten erfasst (Verbrauch,
  Netz, PV, Batterie). Weitere Werte (z.B. je String) lassen sich in
  `PROCESS_DATA_CANDIDATES` in `backend/app/plenticore_client.py` ergänzen.
- Die SQLite-Datei wächst mit der Zeit (bei 15s-Intervall und 2 Geräten ca.
  11.000 Zeilen/Tag). Für viele Jahre Historie wäre irgendwann ein Umzug auf
  PostgreSQL oder eine Zeitreihen-DB sinnvoll – die Datenzugriffsschicht ist
  bewusst einfach gehalten, damit das leicht austauschbar bleibt.
- Die Benutzerverwaltung ist bewusst einfach gehalten (kein 2FA, kein
  Passwort-Reset per E-Mail, feste Rollen admin/betreiber). Details stehen
  unter [Benutzerverwaltung und Login](INSTALLATION.md#benutzerverwaltung--login).
