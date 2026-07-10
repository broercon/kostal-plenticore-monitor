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

**Wichtiger Hinweis zur Vorzeichen-Konvention:** `Grid_P` wird standardmäßig
so interpretiert, dass ein negativer Wert Einspeisung ins Netz bedeutet und
ein positiver Wert Bezug aus dem Netz. Das kann je nach Installation/
Zählerausrichtung aber andersherum sein. Im Dashboard gibt es dafür eine
Kachel "Netzleistung (roh, Grid_P)" mit dem unveränderten Wert – zum
Abgleich in der Web-Oberfläche des Wechselrichters selbst nachsehen, ob
gerade Einspeisung oder Netzbezug angezeigt wird. Falls unsere Zuordnung
vertauscht ist, in `.env` `GRID_POWER_INVERTED=true` setzen und neu
starten (`docker compose up -d`).

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
schreibt einen Datensatz in `data/kostal.db`. Der Server muss dafür
dauerhaft aktiv sein (z.B. auf einem Raspberry Pi, der durchläuft), nicht
nur beim Betrachten des Dashboards – die normale REST-API des Plenticore
liefert nur Momentanwerte plus kumulierte Tages-/Monats-/Jahres-/
Gesamtwerte, keine minutengenaue Historie.

Es gibt aber einen zweiten Weg für ältere Daten: Der Wechselrichter führt
intern einen Datenlogger, dessen Aufzeichnungen sich über
`backend/app/import_logdata.py` nachträglich importieren lassen – siehe
Abschnitt "Alte Daten nachträglich importieren" unten.

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

## Alte Daten nachträglich importieren

Der Plenticore führt intern einen Datenlogger (die gleiche Quelle, aus der
auch die "Logdaten"-Ansicht in der Web-Oberfläche des Wechselrichters
gespeist wird). Damit lassen sich Messwerte von vor der Inbetriebnahme
dieser App nachträglich importieren – mit Einschränkungen:

- Das genaue Spaltenformat ist von Kostal nicht offiziell dokumentiert und
  basiert hier auf Berichten aus der Community, nicht auf offizieller
  Doku. Es kann je nach Gerät/Firmware abweichen.
- Einspeiseleistung lässt sich aus dem Log-Format nicht zuverlässig
  auftrennen und bleibt bei importierten Altdaten leer. Hausverbrauch,
  PV-Leistung, Netzbezug und Batterie-Ladestand werden aber befüllt.
- Wie weit der interne Logger zurückreicht, hängt vom Gerät ab.

### Automatischer Abgleich bei jedem Start

Die App macht das jetzt automatisch: bei jedem Start wird im Hintergrund
(ohne das Dashboard zu blockieren) für jeden konfigurierten Wechselrichter
der interne Logger der letzten `AUTO_IMPORT_DAYS` Tage (Standard: 7)
abgeglichen. Das ist dank der Dedup-Logik gefahrlos bei jedem Neustart –
so werden z.B. Lücken durch Ausfallzeiten automatisch nachträglich
gefüllt, sobald der Server wieder läuft. In den Logs
(`docker compose logs -f`) siehst du nach jedem Start eine Zeile wie
"Automatischer Logdaten-Abgleich für ...: X neue Zeilen, Y bereits
vorhanden".

Mit `AUTO_IMPORT_HISTORY=false` (in `.env`) lässt sich das abschalten,
mit `AUTO_IMPORT_DAYS` der Zeitraum anpassen (Standard: 35 Tage, damit der
"30 Tage"-Button im Dashboard mit etwas Puffer abgedeckt ist). Mit
`AUTO_IMPORT_DAYS=unbegrenzt` (oder `0`/`all`) wird stattdessen so weit
wie möglich zurück abgeglichen – der Wechselrichter liefert dabei ohnehin
nur so viel Historie zurück, wie sein interner Logger tatsächlich noch
gespeichert hat, ein zu weit zurückreichendes Anfragedatum ist also
unproblematisch. Das läuft im Hintergrund und blockiert das Dashboard
nicht, kann bei sehr langer Gerätehistorie aber ein paar Minuten dauern,
bis der komplette Datensatz einmal heruntergeladen und importiert ist.
Wichtig:
Diese Einstellung wirkt erst ab dem nächsten Neustart (`docker compose up
-d --build`) – erst dann holt die App den zusätzlichen Zeitraum vom
internen Logger nach. **Solange die App noch keine 30 Tage lang lief UND
kein passender Import gemacht wurde, zeigen die 14-/30-Tage-Ansichten nur
so viele Tage, wie tatsächlich in der Datenbank vorhanden sind** – das
liegt dann nicht an einem Anzeigefehler, sondern schlicht daran, dass die
Werte für die fehlenden Tage noch nicht übertragen/importiert wurden.

### Abgleich manuell anstoßen

Im Dashboard gibt es oben den Button "Logdaten-Abgleich jetzt starten" –
damit lässt sich der Abgleich sofort auslösen, ohne extra den Container
neu zu starten (z.B. um nach einer Änderung von `AUTO_IMPORT_DAYS` direkt
zu sehen, ob der Import durchläuft). Daneben steht der aktuelle Status
("läuft …" bzw. Ergebnis des letzten Laufs je Wechselrichter: Anzahl neuer/
nachträglich befüllter/unveränderter Zeilen, oder eine Fehlermeldung).
Läuft bereits ein Abgleich, wird ein zweiter Klick ignoriert (kein
paralleler Import). Das funktioniert auch, wenn `AUTO_IMPORT_HISTORY=false`
gesetzt ist – diese Einstellung betrifft nur den automatischen Lauf beim
Start, nicht den manuellen Button.
Zusätzlich gilt: Netzbezug/Einspeisung (und damit auch die
Solar/Batterie-Aufteilung im Tagesvergleich) lassen sich nur für Zeiträume
befüllen, in denen die App live gepollt hat – der interne Logger liefert
dafür keine Werte (KSEM-Limitation, siehe oben). Für PV-Erzeugung und
Hausverbrauch dagegen holt der automatische Abgleich auch länger
zurückliegende Tage nach, sobald `AUTO_IMPORT_DAYS` erhöht ist und der
interne Logger des Wechselrichters so weit zurückreicht.

### Manueller Import für einen größeren/bestimmten Zeitraum

Für einen initialen Import weiter zurückliegender Daten (mehr als
`AUTO_IMPORT_DAYS` Tage) weiterhin manuell aufrufen:

```bash
# 1. Erst nur eine Vorschau ansehen (nichts wird gespeichert):
docker compose exec kostal-monitor python -m app.import_logdata \
  --host 192.168.1.50 --password DEIN_PASSWORT \
  --device-id wr1 --begin 2026-06-01 --end 2026-07-10

# 2. Wenn die Vorschau plausibel aussieht (z.B. Größenordnung passt zum
#    Live-Dashboard), wirklich importieren:
docker compose exec kostal-monitor python -m app.import_logdata \
  --host 192.168.1.50 --password DEIN_PASSWORT \
  --device-id wr1 --begin 2026-06-01 --end 2026-07-10 --commit
```

`--device-id` muss zu einer ID aus `config/inverters.json` passen. Ein
erneuter Lauf überspringt bereits importierte Zeitstempel automatisch
(kein doppelter Import). Falls die PV-Spalten falsch erkannt werden
(Vorschau prüfen!), lassen sie sich mit `--pv-columns "DC0/P,DC1/P"`
manuell vorgeben.

## Diagramm: mehrere Kurven ein-/ausblenden

Das Diagramm zeigt bereits Hausverbrauch, Einspeisung, Netzbezug und
PV-Leistung übereinander. Auf einen Eintrag in der Legende klicken blendet
die jeweilige Kurve ein oder aus (Standardverhalten von Chart.js). Über die
Buttons oberhalb des Diagramms lässt sich der Zeitraum wechseln (24 Std,
7 Tage, 30 Tage) – für die 7-Tage-Ansicht braucht es entsprechend ein paar
Tage Laufzeit, bis sie vollständig gefüllt ist. Die "24 Std"-Ansicht zeigt
immer die feste Achse 00:00–24:00 Uhr des aktuellen Tages (lokale
Mitternacht als Start, 24 Uhr als Ende) statt eines rollierenden
24-Stunden-Fensters – der noch nicht vergangene Teil des Tages bleibt dabei
einfach leer.

## Tagesvergleich: Tage übereinanderlegen

Unterhalb des normalen Diagramms gibt es einen zweiten Bereich
("Tagesvergleich"), der einzelne Kalendertage direkt vergleichbar macht:
die X-Achse zeigt immer fest 00:00–24:00 Uhr, und jeder ausgewählte Tag
erscheint als eigene Kurve darüber gelegt, jeweils in einer eigenen, festen
Farbe (der aktuellste Tag etwas dicker gezeichnet). Zeitraum wählbar von
1 Tag bis 30 Tage.

Drei Kennzahlen stehen zur Auswahl:

- **PV-Erzeugung** – reine PV-Leistungskurve je Tag.
- **Verbrauch aus Solar & Batterie** – Hausverbrauch aufgeteilt in den
  Anteil, der direkt aus PV gedeckt wurde (durchgezogene Linie), und den
  Anteil aus der Batterie (gestrichelte Linie, gleiche Farbe wie der
  zugehörige Tag). Die Aufteilung wird rein aus der Leistungsbilanz
  berechnet (PV + Netzbezug + Batterie = Hausverbrauch + Einspeisung) und
  braucht daher Netzbezugs-/Einspeisungswerte – bei importierten Altdaten
  ohne Netzmessung (KSEM-Limitation, siehe oben) bleibt sie leer, es
  funktioniert nur mit live erfassten Daten. Aus Lesbarkeitsgründen ist der
  Zeitraum hierfür auf 7 Tage begrenzt.
- **Verbrauch aus dem Netz** – reine Netzbezugskurve je Tag.

Auch hier lässt sich per Klick auf die Legende ein einzelner Tag ein-/
ausblenden, z.B. um gezielt nur zwei Tage gegenüberzustellen.

## Tagesverbrauch: Säulendiagramm

Ganz unten zeigt ein Säulendiagramm den Hausverbrauch je Tag als Summe
(kWh) – wählbar über 14/30/90/365 Tage zurück. Anders als die
"heute"-Kachel wird hier immer direkt aus den gespeicherten Messwerten
integriert (Trapezregel), nicht aus vom Wechselrichter gemeldeten
Tageswerten. Das funktioniert daher auch für vergangene Tage, die nur über
den Logdaten-Import (nicht live) erfasst wurden, da Hausverbrauch (anders
als Netzbezug/Einspeisung) auch in importierten Altdaten vorhanden ist.
Für Tage ganz ohne Messwerte bleibt die Säule leer.

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
- `GET /api/readings/day-profile?device_id=&days=7&bucket_minutes=15` –
  Zeitreihen je Kalendertag (00:00–24:00 Uhr lokal) für den
  Tagesvergleich, inkl. Solar-/Batterie-Aufteilung des Hausverbrauchs.
- `GET /api/readings/daily-totals?device_id=&metric=home&days=30` –
  tägliche kWh-Summen für das Tagesverbrauchs-Säulendiagramm.
  `metric`: `home`, `pv`, `grid_draw` oder `feed_in`.
- `POST /api/admin/import-history` – stößt den Logdaten-Abgleich sofort an
  (auch bei `AUTO_IMPORT_HISTORY=false`); liefert `{"started": bool,
  "message": str}`.
- `GET /api/admin/import-history/status` – Status/Ergebnis des letzten
  Abgleichs (`running`, `last_started_at`, `last_finished_at`, `results`
  je Wechselrichter).

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
