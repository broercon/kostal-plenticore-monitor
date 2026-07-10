# Kostal Plenticore Monitor

Eigene Anwendung (Backend + Frontend) zum Tracken von Einspeiseleistung und
Hausverbrauch für einen oder zwei Kostal-Plenticore-Wechselrichter. Die
Messwerte werden selbst in einer SQLite-Datenbank gespeichert und in einer
Weboberfläche mit Live-Kacheln und Diagrammen angezeigt.

## Architektur

- **Backend**: Python + FastAPI. Ein Hintergrund-Task fragt die konfigurierten
  Wechselrichter über die REST-API (via [pykoplenti](https://github.com/stegm/pykoplenti))
  in einem festen Intervall ab und schreibt jeden Messwert in SQLite.
- **Frontend**: statisches HTML/JS-Dashboard (Chart.js), wird direkt vom
  Backend mit ausgeliefert – kein separater Webserver nötig.
- **Datenbank**: SQLite-Datei, per Docker-Volume persistiert.
- Alles läuft in einem einzigen Container über Docker Compose.

## Was wird erfasst?

Pro Wechselrichter und Abfrage-Zyklus:

- Hausverbrauch (`Home_P`)
- Netzleistung (`Grid_P`), aufgeteilt in Einspeiseleistung und Netzbezug
- PV-Leistung (Summe aller DC-Eingänge, `pv_P`)
- Batterieleistung und Ladezustand (falls vorhanden)
- Tagessummen in kWh: PV-Ertrag, Hausverbrauch, Einspeisung

**Wichtiger Hinweis zur Vorzeichen-Konvention:** `Grid_P` wird so
interpretiert, dass ein negativer Wert Einspeisung ins Netz bedeutet und ein
positiver Wert Bezug aus dem Netz. Das ist die gängige Konvention für den
Plenticore, sollte sich das bei deinem Gerät anders verhalten, kannst du die
Funktion `_split_grid_power` in `backend/app/plenticore_client.py` einfach
umdrehen (schau dir dazu einfach die ersten paar Messwerte im Diagramm an).

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

## Wie werden die Daten gefüllt?

Solange der Container läuft, fragt der Hintergrund-Task alle
`POLL_INTERVAL_SECONDS` Sekunden jeden konfigurierten Wechselrichter ab und
schreibt einen Datensatz in `data/kostal.db`. Es gibt **keine rückwirkende
Migration** alter Messwerte: Der Plenticore selbst stellt über seine
REST-API nur Momentanwerte plus kumulierte Tages-/Monats-/Jahres-/
Gesamtwerte bereit, aber keine minutengenaue Historie zum Nachladen. Die
Zeitreihen-Diagramme bauen sich also erst mit der Zeit auf, seit dem
Zeitpunkt, ab dem der Container läuft – der Server muss dafür dauerhaft
aktiv sein (z.B. auf einem Raspberry Pi, der durchläuft), nicht nur beim
Betrachten des Dashboards.

Die drei "heute"-Kacheln (PV-Ertrag, Verbrauch, Einspeisung) nutzen primär
die vom Wechselrichter selbst mitgeführten Tageswerte. Manche Geräte/Logins
liefern diese aber nicht vollständig (z.B. wenn der normale Nutzer-Login
keinen Zugriff auf das Statistik-Modul hat, oder der virtuelle
Einspeise-Tageswert eine Batterie voraussetzt). In diesem Fall rechnet die
Anwendung automatisch aus den seit lokaler Mitternacht gespeicherten
Leistungswerten hoch (Integration) – das braucht aber ebenfalls etwas
Vorlauf seit Mitternacht, bis sinnvolle Werte erscheinen; bei einem frisch
gestarteten Container mitten am Tag zeigt die selbst berechnete Kachel dann
zunächst nur den Teil des Tages, der bereits erfasst wurde.

## Diagramm: mehrere Kurven ein-/ausblenden

Das Diagramm zeigt bereits Hausverbrauch, Einspeisung, Netzbezug und
PV-Leistung übereinander. Auf einen Eintrag in der Legende klicken blendet
die jeweilige Kurve ein oder aus (Standardverhalten von Chart.js). Über die
Buttons oberhalb des Diagramms lässt sich der Zeitraum wechseln (24 Std,
7 Tage, 30 Tage) – für die 7-Tage-Ansicht braucht es entsprechend ein paar
Tage Laufzeit, bis sie vollständig gefüllt ist.

## Daten sichern

Die komplette Historie liegt in `./data/kostal.db` (SQLite-Datei). Für ein
Backup reicht es, diese Datei zu kopieren (idealerweise bei gestopptem
Container, damit keine Schreiboperation mittendrin ist).

## API (für eigene Auswertungen)

- `GET /api/devices` – konfigurierte Wechselrichter
- `GET /api/readings/latest` – letzter bekannter Messwert je Wechselrichter
- `GET /api/readings/today-summary` – Tagessummen (PV-Ertrag, Verbrauch, Einspeisung)
- `GET /api/readings/history?device_id=&hours=24&bucket_minutes=5` – Zeitreihe
  für Diagramme. `device_id` weglassen, um beide Wechselrichter summiert zu
  bekommen.

## Grenzen / mögliche Erweiterungen

- Aktuell wird nur eine feste Auswahl an Prozessdaten erfasst (Verbrauch,
  Netz, PV, Batterie). Weitere Werte (z.B. je String) lassen sich in
  `PROCESS_DATA_CANDIDATES` in `backend/app/plenticore_client.py` ergänzen.
- Die SQLite-Datei wächst mit der Zeit (bei 15s-Intervall und 2 Geräten ca.
  11.000 Zeilen/Tag). Für viele Jahre Historie wäre irgendwann ein Umzug auf
  PostgreSQL oder eine Zeitreihen-DB sinnvoll – die Datenzugriffsschicht ist
  bewusst einfach gehalten, damit das leicht austauschbar bleibt.
- Kein Login/Auth auf dem Dashboard selbst – für den Heimgebrauch im eigenen
  Netz gedacht. Falls von außen erreichbar, unbedingt hinter einen Reverse
  Proxy mit Authentifizierung stellen.
