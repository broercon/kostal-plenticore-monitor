# API-Referenz

[Zurück zum README](../README.md)

Das Backend stellt zusätzlich die automatisch erzeugte FastAPI-Oberfläche
unter `/docs` und das OpenAPI-Schema unter `/openapi.json` bereit.

Alle `/api/...`-Endpunkte außer `POST /api/auth/login` benötigen das
Session-Cookie `kpm_session`. Für Skripte kann `curl` das Cookie speichern
und wiederverwenden:

```bash
curl -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"DEIN_PASSWORT"}' \
  http://localhost:8000/api/auth/login

curl -b cookies.txt http://localhost:8000/api/devices
```

## Anmeldung

- `POST /api/auth/login` – nimmt `username` und `password` entgegen,
  setzt das Session-Cookie und liefert den aktuellen Nutzer.
- `POST /api/auth/logout` – löscht die aktuelle serverseitige Sitzung und
  das Cookie.
- `GET /api/auth/me` – liefert `id`, `username`, `role` und
  `must_change_password`.
- `POST /api/auth/change-password` – erwartet `current_password` und ein
  `new_password` mit 12 bis 256 Zeichen. Bei Erfolg werden alle Sitzungen
  des Nutzers ungültig; anschließend ist eine neue Anmeldung erforderlich.

## Geräte und Messwerte

- `GET /api/devices` – konfigurierte Geräte mit ID, Name und Host.
- `GET /api/readings/latest` – letzter bekannter Messwert je Gerät. Bei
  mehreren Geräten enthält die Antwort zusätzlich `device_id: "_all_"`.
- `GET /api/readings/today-summary` – heutiger PV-Ertrag, Hausverbrauch
  und Einspeisung je Gerät sowie optional die Gesamtsumme.
- `GET /api/readings/feed-in-summary` – hausweite Einspeisung für heute,
  gestern, vorgestern, diese/letzte Woche, diesen/letzten Monat und
  dieses/letztes Jahr.
- `GET /api/readings/pv-yield-summary` – reine, integrierte PV-Erzeugung
  für dieselben neun Zeiträume.

### Zeitreihen

- `GET /api/readings/history`
  - `device_id`: optional; leer bedeutet alle Geräte
  - `hours`: Standard `24`, Bereich `0.1` bis `2160`
  - `bucket_minutes`: Standard `5`, Bereich `1` bis `1440`
  - Bei `hours <= 24` beginnt die Zeitreihe an der lokalen Mitternacht.
- `GET /api/readings/day-profile`
  - `device_id`: optional
  - `days`: Standard `7`, Bereich `1` bis `30`
  - `bucket_minutes`: Standard `15`, Bereich `5` bis `60`
  - Liefert Kalendertage auf einer gemeinsamen Achse von 00:00 bis 24:00 Uhr.
- `GET /api/readings/daily-totals`
  - `device_id`: optional
  - `metric`: `home`, `pv`, `grid_draw` oder `feed_in`
  - `days`: Standard `30`, Bereich `1` bis `400`
- `GET /api/readings/daily-home-breakdown`
  - `days`: Standard `30`, Bereich `1` bis `400`
  - Liefert `pv_kwh`, `battery_kwh` und `grid_kwh` pro Tag.
- `GET /api/readings/hourly-per-device`
  - `metric`: `feed_in`, `pv`, `home` oder `grid_draw`
  - `days`: Standard `1`, Bereich `1` bis `30`
  - Liefert stündliche Werte getrennt nach Wechselrichter.

Hausverbrauch, Netzbezug und Einspeisung sind bei mehreren Wechselrichtern
hausweite Größen. Ein gesetztes `device_id` beschränkt dort nur sinnvoll
zuordenbare Werte wie PV- und Batterieleistung. Die Hintergründe stehen unter
[Dashboard und Berechnungen](CALCULATIONS.md#mehrere-wechselrichter-hausverbrauchnetz-korrekt-berechnen).

## Administration

Alle folgenden Endpunkte benötigen die Rolle `admin`:

- `GET /api/admin/users` – Benutzer auflisten.
- `POST /api/admin/users/{user_id}/reset-password` – Passwort eines
  Nutzers zurücksetzen. Ohne `new_password` wird ein zufälliges Passwort
  erzeugt. Ein manuell gesetztes Passwort benötigt 12 bis 256 Zeichen.
  Bestehende Sitzungen des Nutzers werden beendet.
- `GET /api/admin/forecast/config` – Aktivierung und Standortkoordinaten
  abrufen. `source` zeigt, ob die Startwerte aus
  `inverters.json` oder die gespeicherten SQLite-Werte verwendet werden.
- `PUT /api/admin/forecast/config` – Aktivierung und Standortkoordinaten in
  SQLite speichern.
- `POST /api/admin/import-history` – Historienabgleich im Hintergrund
  starten; funktioniert auch bei `AUTO_IMPORT_HISTORY=false`.
- `GET /api/admin/import-history/status` – Laufstatus und Ergebnis je Gerät.
- `GET /api/admin/daily-report/status` – Zustand und letztes Ergebnis des
  täglichen Berichts.
- `GET /api/admin/daily-report/config` – gespeicherte Konfiguration; der
  API-Key wird nicht zurückgegeben, nur `mail_service_api_key_set`.
- `PUT /api/admin/daily-report/config` – Berichtskonfiguration speichern.
  Ein leer gelassener API-Key behält den vorhandenen Wert bei.
- `POST /api/admin/daily-report/trigger` – Bericht sofort versenden,
  unabhängig vom Aktiv-Schalter.

## PV-Prognose

- `GET /api/forecast` – sieben Tage erwartete PV-Leistung und Energie aus
  den historischen Messwerten jedes Wechselrichters und Open-Meteo-
  Strahlungsdaten. Liefert Stundenwerte, Tagesenergie, Prognosebereich,
  Produktionszeitraum und getrennte Tageswerte je Wechselrichter.
