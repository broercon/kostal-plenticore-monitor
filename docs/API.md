# API-Referenz

[Zurück zum README](../README.md)

## API (für eigene Auswertungen)

Alle Endpunkte außer `/api/auth/login` erfordern eine gültige Anmeldung
(Session-Cookie) – ohne diese liefern sie `401`. Für eigene Skripte zuerst
gegen `/api/auth/login` einloggen und das Cookie mitsenden (z.B. `curl -c
cookies.txt -b cookies.txt ...`).

- `POST /api/auth/login` – `{"username": ..., "password": ...}`, setzt bei
  Erfolg das Session-Cookie.
- `POST /api/auth/logout` – beendet die aktuelle Sitzung.
- `GET /api/auth/me` – aktueller Nutzer (`id`, `username`, `role`,
  `must_change_password`).
- `POST /api/auth/change-password` – eigenes Passwort ändern
  (`current_password`, `new_password`).
- `GET /api/admin/users` – (nur Rolle admin) alle Nutzer auflisten.
- `POST /api/admin/users/{id}/reset-password` – (nur Rolle admin) Passwort
  eines Nutzers zurücksetzen; ohne `new_password` im Body wird eines
  zufällig erzeugt und in der Antwort zurückgegeben.
- `GET /api/devices` – konfigurierte Wechselrichter
- `GET /api/readings/latest` – letzter bekannter Messwert je Wechselrichter;
  bei mehreren konfigurierten Geräten zusätzlich ein Eintrag mit
  `device_id: "_all_"` mit der korrekt berechneten Gesamtsumme (siehe
  "Mehrere Wechselrichter: Hausverbrauch/Netz korrekt berechnen")
- `GET /api/readings/today-summary` – Tagessummen (PV-Ertrag, Verbrauch,
  Einspeisung); ebenfalls mit `"_all_"`-Eintrag bei mehreren Geräten
- `GET /api/readings/history?device_id=&hours=24&bucket_minutes=5` – Zeitreihe
  für Diagramme. `device_id` weglassen, um alle Wechselrichter summiert zu
  bekommen. Bei mehreren konfigurierten Geräten liefert `home_power_w`/
  `feed_in_power_w`/`grid_draw_power_w` IMMER die hausweite Energiebilanz,
  auch mit gesetztem `device_id` (siehe "Mehrere Wechselrichter" oben) –
  nur `pv_power_w`/`battery_power_w` sind dann geräteeigen.
- `GET /api/readings/day-profile?device_id=&days=7&bucket_minutes=15` –
  Zeitreihen je Kalendertag (00:00–24:00 Uhr lokal) für den
  Tagesvergleich, inkl. Solar-/Batterie-Aufteilung des Hausverbrauchs.
  Netzbezug und Solar-/Batterie-Aufteilung sind ebenfalls hausweit (siehe
  oben), nur die PV-Kurve bleibt bei gesetztem `device_id` geräteeigen.
- `GET /api/readings/daily-totals?device_id=&metric=home&days=30` –
  tägliche kWh-Summen für das Tagesverbrauchs-Säulendiagramm.
  `metric`: `home`, `pv`, `grid_draw` oder `feed_in`. Bei `home`/`grid_draw`/
  `feed_in` wird `device_id` bei mehreren Geräten ignoriert (hausweite
  Summe); nur bei `pv` bleibt es wirksam.
- `GET /api/readings/daily-home-breakdown?days=30` – wie `daily-totals`
  (`metric=home`), aber zusätzlich aufgeschlüsselt nach Deckungsanteil:
  `pv_kwh`, `battery_kwh`, `grid_kwh` je Tag (Summe ergibt den gesamten
  Hausverbrauch) – Grundlage für die gestapelte Einfärbung im
  Tagesverbrauchs-Diagramm. Kein `device_id`-Parameter, da hausweit.
- `GET /api/readings/hourly-per-device?metric=feed_in&days=1` – stündliche
  kWh-Summen JE Wechselrichter (nicht summiert) für den
  Wechselrichter-Vergleich.
- `POST /api/admin/import-history` – stößt den Logdaten-Abgleich sofort an
  (auch bei `AUTO_IMPORT_HISTORY=false`); liefert `{"started": bool,
  "message": str}`.
- `GET /api/admin/import-history/status` – Status/Ergebnis des letzten
  Abgleichs (`running`, `last_started_at`, `last_finished_at`, `results`
  je Wechselrichter).
- `GET /api/admin/daily-report/status` – (nur Rolle admin) Status des täglichen
  Mail-Reports (`enabled`, `scheduled_time`, `recipients`,
  `last_sent_at`, `last_status`, `last_message`).
- `GET /api/admin/daily-report/config` – (nur Rolle admin) aktuelle
  Konfiguration; der Mail-Service-API-Key selbst wird nie zurückgegeben,
  nur `mail_service_api_key_set` (bool).
- `PUT /api/admin/daily-report/config` – (nur Rolle admin) Konfiguration
  speichern (`enabled`, `report_time`, `recipients`, `mail_service_url`,
  `mail_service_api_key`, `mail_service_from_name`); wirkt ohne
  Container-Neustart.
- `POST /api/admin/daily-report/trigger` – (nur Rolle admin) verschickt den
  täglichen Zusammenfassungs-Report sofort, z.B. zum Testen der
  Mail-Konfiguration, unabhängig vom "Aktiv"-Schalter.
