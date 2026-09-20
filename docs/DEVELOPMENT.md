# Entwicklung

[Zurück zum README](../README.md)

## Architektur

- **Backend**: Python + FastAPI. Ein Hintergrund-Task fragt die konfigurierten
  Wechselrichter über die REST-API (via [pykoplenti](https://github.com/stegm/pykoplenti))
  in einem festen Intervall ab und schreibt jeden Messwert in die Datenbank.
- **Frontend**: statisches HTML/JS-Dashboard (Chart.js), wird direkt vom
  Backend mit ausgeliefert – kein separater Webserver nötig.
- **Datenbank**: PostgreSQL, über `DATABASE_URL` konfiguriert (siehe
  [Installation](INSTALLATION.md#datenbank-einrichten)). Das Docker-Volume
  `./data` enthält nur noch die Logdateien.
- Alles läuft in einem einzigen Container über Docker Compose.

## Datenbank-Migrationen

`init_db()` (`backend/app/database.py`) legt über `Base.metadata.create_all()`
fehlende Tabellen an, ändert aber **keine** bestehenden Tabellen ab. Kommt mit
einem Update ein neues Feld zu einem bestehenden Modell hinzu (z.B.
`readings.ac_power_w`), braucht es dafür eine kleine, manuell geschriebene
Migrationsfunktion direkt in `database.py`, die das Feld per `ALTER TABLE`
ergänzt. Ein Werkzeug wie Alembic lohnt sich für dieses Einzelplatz-Projekt
(noch) nicht.

**Migrationen werden nicht für immer mitgeschleppt.** Jede Migrationsfunktion
trägt in ihrem Docstring das Einführungsdatum. Etwa 6 Monate nach diesem
Datum kann man davon ausgehen, dass die eine (oder wenigen) betriebenen
Instanz(en) dieser App längst darüber gelaufen sind – die Migration kann
dann ersatzlos entfernt werden, statt unbegrenzt Code für ein Altschema zu
pflegen, das niemand mehr hat. Bei einem Update, das eine Migrationsfunktion
entfernt, immer auch den zugehörigen Test in `backend/tests/` mit entfernen.

Beim Umstieg von SQLite auf PostgreSQL (September 2026) sind alle damals
vorhandenen Migrationsfunktionen entfallen: die PostgreSQL-Datenbank wurde
frisch über `create_all()` mit dem vollständigen Schema angelegt, eine
Bestandsdatenbank mit fehlenden Spalten kann es dort also nicht geben.
`init_db()` legt seitdem nur noch fehlende Tabellen an.

## Worauf beim Ändern von Abfragen zu achten ist

Die Anwendung lief bis September 2026 auf SQLite. Beim Umstieg kamen drei
Fehler ans Licht, die SQLite jahrelang verziehen hatte – sie beschreiben
gut, worauf PostgreSQL besteht:

1. **`VARCHAR`-Längen werden erzwungen.** Eine im Modell zu knapp
   deklarierte Spalte fällt unter SQLite nie auf. Genau so passiert bei
   `daily_energy_cache.field`: deklariert als `String(32)`, beschrieben mit
   37 Zeichen.
2. **Fremdschlüssel werden erzwungen** (SQLite bräuchte dafür
   `PRAGMA foreign_keys=ON`). Beim Löschen auf die Reihenfolge achten:
   erst die verweisende Tabelle, dann die verwiesene.
3. **Zeitstempel kommen zonenbehaftet zurück.** Ein unbedingtes
   `replace(tzinfo=timezone.utc)` auf einen Wert aus der Datenbank
   verschiebt den Zeitpunkt still um den Zonenversatz – richtig ist
   `astimezone()` für den bereits zonenbehafteten Fall (siehe
   `weather_cache._utc()`). Die Verbindung wird zwar auf UTC festgelegt
   (`database.py`), aber darauf sollte sich kein Aufrufer verlassen müssen.

Zwei SQL-Eigenheiten, die im Code bewusst so stehen:

- **`greatest()` statt `max()`** für den größeren zweier Werte *je Zeile* –
  `max()` ist in PostgreSQL ausschließlich eine Aggregatfunktion und
  existiert mit zwei Argumenten gar nicht
  (`energy_forecast._pure_pv_sql_expression()`).
- **`date_trunc(... AT TIME ZONE 'UTC')`** für die Stunden-Einteilung, statt
  sich auf die Zeitzone der Sitzung zu verlassen
  (`energy_forecast._hour_bucket_expression()`).

Alle fünf Punkte sind in `backend/tests/test_postgres_contract.py`
abgesichert.

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
TEST_DATABASE_URL=postgresql://kostal_app:kostal_app@localhost:5432/kostal_app_test \
  python -m pytest tests/ -v
```

Die Tests brauchen eine laufende PostgreSQL-Instanz. **Unbedingt eine
eigene Test-Datenbank angeben, niemals die produktive** – vor jedem
einzelnen Testfall wird das gesamte Schema geleert. Ohne
`TEST_DATABASE_URL` wird genau die oben gezeigte Adresse versucht (siehe
`conftest.py`). Schnell aufgesetzt mit:

```bash
docker run --rm -d --name kpm-testdb -p 5432:5432 \
  -e POSTGRES_USER=kostal_app -e POSTGRES_PASSWORD=kostal_app \
  -e POSTGRES_DB=kostal_app_test postgres:17
```

Die Tests starten bewusst keinen echten Poller/Import gegen einen
Wechselrichter. Abgedeckt sind u.a.: Standard-Nutzer werden nur
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
- Die Tabelle `readings` wächst mit der Zeit (bei 15s-Intervall und 2
  Geräten ca. 11.000 Zeilen/Tag). Für viele Jahre Historie wären irgendwann
  eine Verdichtung älterer Messwerte oder eine Zeitreihen-Erweiterung
  sinnvoll – die Datenzugriffsschicht ist bewusst einfach gehalten.
- Die Benutzerverwaltung ist bewusst einfach gehalten (kein 2FA, kein
  Passwort-Reset per E-Mail, feste Rollen admin/betreiber). Details stehen
  unter [Benutzerverwaltung und Login](INSTALLATION.md#benutzerverwaltung--login).
