# Installation und Konfiguration

[Zurück zum README](../README.md)

## Einrichtung

### 1. Wechselrichter konfigurieren

```bash
cp config/inverters.example.json config/inverters.json
```

Dann `config/inverters.json` mit IP-Adresse(n) und Passwort/Gerätepasswort
deiner Wechselrichter befüllen. Für zwei Wechselrichter einfach beide
Einträge in der Liste lassen, für einen nur einen Eintrag:

```json
[
  { "id": "wr1", "name": "Wechselrichter Dach Süd", "host": "192.168.1.50", "password": "..." }
]
```

Das Passwort ist dasselbe, mit dem du dich auch an der Web-Oberfläche des
Wechselrichters (`http://<ip-des-wechselrichters>`) anmeldest.

**Bei zwei oder mehr Wechselrichtern am selben Hausanschluss** (z.B. ein
Wechselrichter mit Batterie + Netzzähler/KSEM als "Master", ein zweiter ohne
eigenen Zähler, der per AC die Batterie des ersten mitlädt) unbedingt den
[Hinweise für mehrere Wechselrichter](CALCULATIONS.md#mehrere-wechselrichter-hausverbrauchnetz-korrekt-berechnen) lesen und `has_grid_meter` passend setzen – sonst werden
Hausverbrauch und Netzbezug in der "Alle (Summe)"-Ansicht falsch berechnet.

### 2. Optional: Abfrageintervall/Zeitzone anpassen

```bash
cp .env.example .env
```

`POLL_INTERVAL_SECONDS` steuert, wie oft (in Sekunden) abgefragt wird.
Standard: 15 Sekunden. `TIMEZONE` legt fest, wann der Tag für die
"heute"-Kacheln beginnt (Standard: `Europe/Berlin`).

### 3. Starten

```bash
docker compose up -d --build
```

Danach ist das Dashboard unter `http://<ip-des-servers>:8000` erreichbar,
z.B. `http://localhost:8000` wenn es lokal läuft, oder `http://<raspberry-ip>:8000`
wenn es auf einem Raspberry Pi/NAS/Home-Server läuft.

### Logs ansehen

```bash
docker compose logs -f
```

Verbindungsprobleme zu einem Wechselrichter werden dort als Warnung geloggt,
ohne dass die Anwendung abstürzt – der nächste Abfrage-Zyklus versucht es
automatisch erneut.

## Benutzerverwaltung / Login

Das Dashboard ist jetzt durchgängig durch einen Login geschützt – ohne
gültige Anmeldung liefert die API überall `401` und das Frontend leitet
automatisch zur Login-Seite um. Es gibt zwei Rollen:

- **admin** – volle Rechte, zusätzlich Zugriff auf die Benutzerverwaltung
  (Nutzer auflisten, Passwörter anderer Nutzer zurücksetzen).
- **betreiber** – normaler Zugriff auf Dashboard/Diagramme, kann nur das
  eigene Passwort ändern.

### Erste Anmeldung

Beim allerersten Start (wenn die `users`-Tabelle noch leer ist) legt die App
automatisch drei Nutzer an: `admin` (Rolle admin), `betreiber1` und `betreiber2`
(Rolle betreiber) – jeweils mit einem zufällig erzeugten Initial-Passwort.
Diese Passwörter werden **einmalig** in den Logs ausgegeben:

```bash
docker compose logs -f kostal-monitor
```

Dort erscheint ein Block wie:

```
======================================================================
ERSTE ANMELDEDATEN (nur jetzt im Log sichtbar - bitte notieren
und nach dem ersten Login ueber "Passwort aendern" ersetzen):
  Benutzername: admin       Passwort: xxxxxxxxxxxxxx
  Benutzername: betreiber1    Passwort: xxxxxxxxxxxxxx
  Benutzername: betreiber2    Passwort: xxxxxxxxxxxxxx
======================================================================
```

Bitte notieren und danach über den Button "Passwort ändern" (oben rechts im
Dashboard) durch ein eigenes Passwort ersetzen – bei diesen drei
Initial-Konten öffnet sich der Passwort-Ändern-Dialog beim ersten Login
automatisch.

### Passwort ändern / vergessen

Jeder angemeldete Nutzer kann über "Passwort ändern" (Topbar) sein eigenes
Passwort setzen – dafür muss das aktuelle Passwort bekannt sein; das neue
Passwort muss mindestens 12 Zeichen lang sein. Nach dem Wechsel werden alle
bestehenden Sitzungen beendet und eine erneute Anmeldung ist erforderlich. Hat jemand
sein Passwort vergessen, kann ein Admin es über die Benutzerverwaltung
("Benutzerverwaltung"-Button, nur für Rolle admin sichtbar) zurücksetzen:
dort wird ein neues, zufälliges Passwort angezeigt (nur einmal – merken
oder direkt an die Person weitergeben), das beim nächsten Login sofort
geändert werden muss.

### Technische Details

- Passwörter werden nicht im Klartext gespeichert, sondern als
  PBKDF2-HMAC-SHA256-Hash (200.000 Iterationen) mit individuellem Salt je
  Nutzer.
- Sitzungen laufen über ein httponly-Cookie (kein Zugriff per JavaScript,
  schützt gegen einfaches Auslesen durch eingeschleusten Code) und sind
  serverseitig gespeichert – ein Logout invalidiert die aktuelle Sitzung;
  ein Passwort-Wechsel oder Admin-Reset invalidiert alle Sitzungen des
  betroffenen Nutzers sofort. Ein Container-Neustart meldet bereits angemeldete
  Nutzer nicht ab (Sitzungen sind 30 Tage gültig).
- Diese Nutzerverwaltung ist bewusst einfach gehalten (kein 2FA, kein
  Passwort-Reset per E-Mail) – für den Heimgebrauch im eigenen Netz
  gedacht. Falls das Dashboard von außerhalb des eigenen Netzes erreichbar
  sein soll, zusätzlich HTTPS (z.B. über einen Reverse Proxy) davorschalten,
  damit Zugangsdaten nicht unverschlüsselt übertragen werden.

### Update von einer Version ohne Benutzerverwaltung

Bereits vorhandene Messwerte (`data/kostal.db`) bleiben beim Update
vollständig erhalten: die App legt beim Start nur die neu hinzugekommenen
Tabellen (`users`, `sessions`) an, die bestehende `readings`-Tabelle wird
dabei nicht angefasst. Ein einfaches `docker compose up -d --build` reicht
aus, um die neuen Tabellen zu ergänzen und die drei Standardnutzer
anzulegen – die komplette bisherige Historie bleibt wie gewohnt abrufbar.

## Täglicher Mail-Report

Einmal täglich, zu einer festen (konfigurierbaren) Uhrzeit, verschickt die
App eine gestaltete HTML-Zusammenfassungsmail mit denselben Werten wie im
Dashboard:

- Welche Wechselrichter aktiv/erreichbar waren und wie viel PV-Ertrag sie
  – einzeln und in Summe – an diesem Tag bereits erzielt haben.
- Einspeisung über mehrere Zeiträume (heute, gestern, vorgestern, diese/
  letzte Woche, dieser/letzter Monat) – wie die "Einspeisung"-Tabelle im
  Dashboard.
- Heutiger Hausverbrauch aufgeschlüsselt nach PV-/Batterie-/Netz-Anteil –
  wie das "Tagesverbrauch"-Diagramm.
- Aktueller Batterie-Ladestand je Gerät mit Batterie.

Der Versand läuft über den separaten zentralen Mail-Service
[broercon/Mailserver](https://github.com/broercon/Mailserver) (`POST
/send`), nicht über einen eigenen SMTP-Versand in dieser App.

### Einrichtung über die Admin-Oberfläche (empfohlen)

Als Nutzer mit Rolle **admin** oben rechts auf **"Mail-Report"** klicken.
Dort lassen sich vollständig über die Weboberfläche einstellen – ohne
Server-/Umgebungsvariablen-Zugriff:

- Aktiv/inaktiv
- Uhrzeit
- Empfänger-Adresse(n) (kommagetrennt, beliebig viele)
- Mail-Service-URL (`POST /send`-Endpunkt des Mailserver-Repos)
- Mail-Service API-Key (muss einem der dort unter `API_KEYS` vergebenen
  Keys entsprechen; ein bereits gespeicherter Key bleibt beim Speichern
  erhalten, wenn das Feld leer gelassen wird – er wird aus Sicherheitsgründen
  nie wieder im Klartext angezeigt)
- Absender-Anzeigename (optional)

Über **"Testmail jetzt senden"** lässt sich die Konfiguration sofort prüfen,
unabhängig vom Uhrzeit-Zeitpunkt und auch wenn "Aktiv" noch nicht gesetzt
ist. Änderungen wirken sofort, ohne den Container neu zu starten.

Da Mailserver und dieser Monitor üblicherweise zwei getrennte
`docker-compose`-Projekte sind, muss die Mail-Service-URL von diesem
Container aus erreichbar sein. Zwei Möglichkeiten:
- Einfach: die LAN-IP oder den Hostnamen des Servers verwenden, auf dem
  der Mail-Service läuft, plus dessen veröffentlichten Port (Default
  `8080`) – z.B. `http://192.168.178.50:8080/send`.
- Alternativ: beide Compose-Projekte einem gemeinsamen externen
  Docker-Netzwerk beitreten lassen und dann den Container-Namen
  (`http://mail-api:8080/send`) verwenden.

### Alternative: Einrichtung über Umgebungsvariablen

Wer die Admin-Oberfläche nicht nutzen möchte, kann stattdessen `.env`
befüllen (siehe `.env.example`: `DAILY_REPORT_ENABLED`, `DAILY_REPORT_TIME`,
`DAILY_REPORT_RECIPIENTS`, `MAIL_SERVICE_URL`, `MAIL_SERVICE_API_KEY`,
`MAIL_SERVICE_FROM_NAME`) und den Container neu starten. Diese Werte dienen
nur als Erstbefüllung: Sobald einmal über die Admin-Oberfläche gespeichert
wurde, hat die dortige (in der Datenbank abgelegte) Konfiguration Vorrang
vor den Umgebungsvariablen.

### Was zählt als "aktiv"?

Ein Wechselrichter gilt als aktiv, wenn der Poller innerhalb der letzten
drei Poll-Intervalle (mindestens aber 120s) tatsächlich einen Messwert von
ihm erhalten hat (siehe `app/daily_summary.py:device_online_map`). Ein
Gerät, das seit Containerstart noch nie erfolgreich erreicht wurde, gilt
als nicht aktiv.
