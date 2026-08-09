# Kostal Plenticore Monitor

Webanwendung zum lokalen Erfassen und Auswerten von Leistungs- und
Energiedaten eines oder mehrerer Kostal-Plenticore-Wechselrichter.

Die Anwendung besteht aus einem FastAPI-Backend, einem statischen Dashboard
mit Chart.js und einer SQLite-Datenbank. Alles läuft gemeinsam in einem
Docker-Container.

## Funktionen

- Live-Anzeige für PV-Leistung, Hausverbrauch, Netzbezug und Einspeisung
- Batterie-Leistung und Ladezustand, sofern vorhanden
- Tages-, Wochen-, Monats- und Jahresauswertungen
- Vergleich mehrerer Wechselrichter und Kalendertage
- Automatischer Import vorhandener Wechselrichter-Logdaten
- Benutzerverwaltung mit Admin- und Betreiberrolle
- Optionaler täglicher E-Mail-Bericht
- Persistente Speicherung in SQLite

## Voraussetzungen

- Docker mit Docker Compose
- Netzwerkzugriff vom Docker-Host auf den Wechselrichter
- IP-Adresse und Gerätepasswort jedes Wechselrichters
- Internetzugriff im Browser auf `cdnjs.cloudflare.com`, da Chart.js derzeit
  von dort geladen wird

## Schnellstart

1. Repository klonen und in das Projektverzeichnis wechseln.
2. Wechselrichter-Konfiguration anlegen:

```bash
cp config/inverters.example.json config/inverters.json
```

3. Mindestens Host und Gerätepasswort eintragen:

```json
[
  {
    "id": "wr1",
    "name": "Wechselrichter",
    "host": "192.168.1.50",
    "password": "DEIN_GERAETEPASSWORT"
  }
]
```

4. Anwendung starten:

```bash
docker compose up -d --build
```

Das Dashboard ist anschließend unter
[http://localhost:8000](http://localhost:8000) beziehungsweise über die
IP-Adresse des Docker-Hosts auf Port 8000 erreichbar.

Die beim ersten Start erzeugten Zugangsdaten stehen einmalig im Log:

```bash
docker compose logs -f kostal-monitor
```

Nach der ersten Anmeldung sollte das Initialpasswort über den automatisch
geöffneten Dialog geändert werden. Neue Passwörter benötigen mindestens
12 Zeichen.

## Wichtige Konfiguration

Optionale Umgebungsvariablen können über eine lokale `.env`-Datei gesetzt
werden:

```bash
cp .env.example .env
```

Die wichtigsten Werte sind:

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `POLL_INTERVAL_SECONDS` | `15` | Abstand zwischen zwei Messungen |
| `TIMEZONE` | `Europe/Berlin` | Zeitzone für Tagesgrenzen und Berichte |
| `AUTO_IMPORT_HISTORY` | `true` | Historienabgleich beim Start |
| `AUTO_IMPORT_DAYS` | `35` | Zeitraum des automatischen Imports |
| `GRID_POWER_INVERTED` | `false` | Vertauscht Netzbezug und Einspeisung |

Bei mehreren Wechselrichtern muss genau das Gerät mit dem echten
Netzzähler/KSEM über `has_grid_meter: true` markiert werden. Die ausführliche
Erklärung steht unter
[Dashboard und Berechnungen](docs/CALCULATIONS.md#mehrere-wechselrichter-hausverbrauchnetz-korrekt-berechnen).

## Aktualisieren und stoppen

```bash
docker compose up -d --build
docker compose down
```

Die Messhistorie bleibt im Verzeichnis `data/` erhalten.

## Backup

Die Daten liegen in `data/kostal.db`. Für ein konsistentes Backup sollte die
Anwendung kurz gestoppt und anschließend das Verzeichnis `data/` gesichert
werden. Die Datenbank enthält neben Messwerten auch Benutzer-, Sitzungs- und
Mail-Konfigurationsdaten und muss daher vertraulich behandelt werden.

## Dokumentation

- [Installation, Benutzer und Mail-Report](docs/INSTALLATION.md)
- [Datenerfassung und Historienimport](docs/DATA_IMPORT.md)
- [Dashboard, mehrere Wechselrichter und Berechnungen](docs/CALCULATIONS.md)
- [Betrieb, Logs und Backup](docs/OPERATIONS.md)
- [API-Referenz](docs/API.md)
- [Entwicklung und Tests](docs/DEVELOPMENT.md)

## Sicherheit

Das Dashboard ist für den Betrieb im eigenen Netz ausgelegt. Wird es von
außerhalb erreichbar gemacht, sollte zwingend HTTPS über einen Reverse Proxy
verwendet werden. Konfigurationsdateien, `.env` und die Datenbank dürfen
nicht veröffentlicht werden.

## Lizenz

Für dieses Repository ist derzeit keine separate Lizenzdatei hinterlegt.
