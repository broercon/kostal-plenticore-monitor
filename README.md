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

### 2. Optional: Abfrageintervall anpassen

```bash
cp .env.example .env
```

`POLL_INTERVAL_SECONDS` steuert, wie oft (in Sekunden) abgefragt wird.
Standard: 15 Sekunden.

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
