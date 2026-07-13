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
Zählerausrichtung aber andersherum sein – zum Abgleich in der
Web-Oberfläche des Wechselrichters selbst nachsehen, ob gerade Einspeisung
oder Netzbezug angezeigt wird. Falls unsere Zuordnung vertauscht ist, in
`.env` `GRID_POWER_INVERTED=true` setzen und neu starten
(`docker compose up -d`).

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
Abschnitt "Mehrere Wechselrichter: Hausverbrauch/Netz korrekt berechnen"
weiter unten lesen und `has_grid_meter` passend setzen – sonst werden
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

Ganz unten zeigt ein gestapeltes Säulendiagramm den Hausverbrauch je Tag als
Summe (kWh) – wählbar über 14/30/90/365 Tage zurück. Jede Säule ist danach
eingefärbt, zu welchem Anteil der Verbrauch aus PV (direkt), Speicher
(Batterieentladung) bzw. Netzbezug gedeckt wurde (gleiche Farbgebung wie
beim Tagesvergleich "Verbrauch aus Solar & Batterie"/"aus dem Netz") – Maus
über eine Säule zeigt die genaue Aufteilung samt Gesamtsumme im Tooltip.
Anders als die "heute"-Kachel wird hier immer direkt aus den gespeicherten
Messwerten integriert (Trapezregel), nicht aus vom Wechselrichter gemeldeten
Tageswerten. Das funktioniert daher auch für vergangene Tage, die nur über
den Logdaten-Import (nicht live) erfasst wurden. Fehlen für einen
Messpunkt nur Netzbezug/Einspeisung (z.B. weil die Zähler-Abfrage kurz
fehlschlägt, oder bei importierten Altdaten ohne Netzmessung, siehe
KSEM-Limitation oben), wird dafür 0 angenommen statt den ganzen Messpunkt
zu verwerfen – bei Altdaten ganz ohne Netzmessung heißt das konkret: die
Aufteilung geht davon aus, dass der gesamte Verbrauch aus PV/Speicher kam
(0 % Netzbezug), auch wenn das an einzelnen Tagen nicht ganz stimmen muss.
Fehlen dagegen Haus- oder PV-Werte selbst, bleibt der Messpunkt
unberücksichtigt; für Tage ganz ohne Messwerte bleibt die Säule leer.
Hausverbrauch ist eine
hausweite Größe (siehe Abschnitt "Mehrere Wechselrichter" oben) – bei
mehreren konfigurierten Wechselrichtern zeigt dieses Diagramm daher immer
die Gesamtsumme, unabhängig vom oben ausgewählten Tab.

## Wechselrichter-Vergleich: PV-Ertrag pro Stunde

Ganz unten zeigt ein gestapeltes Säulendiagramm den PV-Ertrag je Stunde,
farblich getrennt nach Wechselrichter – so lässt sich direkt sehen, welches
Gerät wie viel zum Gesamtertrag in einer bestimmten Stunde beigetragen hat.
Gezeigt wird die gesamte erzeugte Energie (`pv_power_w`), unabhängig davon,
ob sie eingespeist oder direkt im Haus verbraucht wurde – nicht nur die
Einspeisung. Zeitraum wählbar über 1/7/30 Tage; bei mehr Tagen werden die
Balken entsprechend schmaler (mit Tooltip trotzdem einzeln ablesbar).

Dieser Abschnitt ist nur sichtbar, wenn oben "Alle (Summe)" ausgewählt ist
und mehr als ein Wechselrichter konfiguriert ist – bei einem einzelnen
ausgewählten (oder einzigen vorhandenen) Gerät gäbe es nichts zu
vergleichen, entsprechend bleibt der Abschnitt dann ausgeblendet.

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
Passwort setzen – dafür muss das aktuelle Passwort bekannt sein. Hat jemand
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
  serverseitig gespeichert – ein Logout oder Passwort-Wechsel invalidiert
  die Sitzung sofort, ein Container-Neustart meldet bereits angemeldete
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

## Polling stoppt nachts / zu einer bestimmten Uhrzeit

Frühere Versionen hatten einen Bug, bei dem das Polling dauerhaft stehen
blieb, wenn ein Wechselrichter die Verbindung auf ungewöhnliche Weise
trennte (z.B. nachts, wenn das Gerät ohne PV-Ertrag in einen
Stromsparmodus geht oder sich selbst neu startet) – ein nicht vorgesehener
Fehlertyp lief dann ungebremst durch die Abfrage-Logik hindurch und
beendete den Hintergrund-Task für **alle** Wechselrichter, bis der
Container manuell neu gestartet wurde. Das äußerte sich genau so: die App
hörte zuverlässig zu einer bestimmten Uhrzeit auf, Daten abzufragen.

Das ist jetzt behoben: `fetch_reading()` fängt jetzt jeden Fehlertyp ab
(nicht mehr nur eine feste Liste "erwarteter" Netzwerkfehler) und gibt
stattdessen `None` zurück; zusätzlich ist jedes Gerät im Polling-Zyklus
gegeneinander isoliert (ein Fehler bei einem Gerät beendet nicht den ganzen
Zyklus), und ein letztes Sicherheitsnetz auf Zyklus-Ebene verhindert, dass
ein unvorhergesehener Fehler das Polling dauerhaft stoppt – im Fehlerfall
wird stattdessen im nächsten Zyklus (`POLL_INTERVAL_SECONDS`) automatisch
ein neuer Versuch unternommen.

Falls die Wechselrichter selbst weiterhin regelmäßig zu einer bestimmten
Uhrzeit (z.B. gegen 4 Uhr nachts) kurz nicht erreichbar sind, ist das meist
kein App-Problem, sondern ein bekanntes Verhalten mancher
Kostal-Plenticore-Geräte: das Kommunikationsmodul kann nachts ohne
PV-Ertrag in einen Stromsparmodus gehen oder sich selbst neu starten. Das
führt jetzt nur noch zu ein oder zwei übersprungenen Abfrage-Zyklen
(sichtbar als Warnung in den Logs, siehe unten), nicht mehr zum dauerhaften
Stillstand.

Zur Kontrolle in den Logs nachsehen:

```bash
docker compose logs --no-color kostal-monitor | grep -i "nicht erreichbar\|Unerwarteter Fehler"
```

## Mehrere Wechselrichter: Hausverbrauch/Netz korrekt berechnen

Bei zwei (oder mehr) Wechselrichtern am selben Hausanschluss – typisch: ein
Wechselrichter mit Batterie und dem echten Netzzähler (Kostal Smart Energy
Meter, KSEM) als "Master", ein zweiter ohne eigenen Zähler, der per AC die
Batterie des ersten mitlädt – kann der einfache Ansatz "jeden Wert über alle
Geräte summieren" für Hausverbrauch und Netzbezug/Einspeisung **falsche,
teils stark negative Werte** liefern.

### Warum das passiert

Jeder Plenticore-Wechselrichter kennt nur sich selbst. Sein `Home_P`
("Hausverbrauch") ist keine direkte Messung, sondern ein interner
Rechenwert, der stillschweigend davon ausgeht, dass er die einzige
PV-/Batterie-Quelle im Haus ist. Speist ein zweiter, unabhängiger
Wechselrichter zusätzliche Energie ein (z.B. um die Batterie des ersten per
AC mitzuladen), kann der erste Wechselrichter das nicht einordnen – er
verbucht die zusätzliche, nicht erklärbare Energie fälschlich als
"Ladung aus dem Netz" und rechnet sich daraufhin einen negativen,
unsinnigen Hausverbrauch zusammen (sichtbar auch direkt im
Kostal-Portal/der Kostal-App für das einzelne Gerät). Das ist ein bekanntes
Verhalten bei Kostal-"Schwarm"-Installationen, nicht ein Fehler dieser App –
in der Kostal-eigenen Dokumentation für Mehr-Wechselrichter-Setups mit KSEM
wird deshalb ausdrücklich empfohlen, alle Geräte UND das KSEM ins Kostal
Solar Portal einzupflegen, weil nur dort korrekt aggregiert wird.

Der Netzbezug/die Einspeisung (`Grid_P`) ist davon dagegen **nicht**
betroffen, sofern das jeweilige Gerät tatsächlich am echten Netzzähler
hängt (Energiemanagement-Sensorposition "Netzanschlusspunkt", meist über
KSEM) – das lässt sich empirisch bestätigen: der von einem so konfigurierten
Gerät gemeldete `Grid_P`-Wert stimmt mit den kumulierten Zählerständen
(`devices:local:powermeter/Imp_E`/`Exp_E`) und der Anzeige im Kostal-Portal
überein. Ein zweites Gerät ohne eigenen Zähler ("kein Sensor verwendet" im
eigenen Energiemanagement) liefert dagegen keinen sinnvollen `Grid_P`-Wert
und darf nicht mit summiert werden.

### Die Lösung dieser App

In `config/inverters.json` lässt sich pro Gerät festlegen, ob es den echten
Netzzähler hat:

```json
[
  { "id": "wr1", "name": "Dach Süd (Batterie)", "host": "...", "password": "...", "has_grid_meter": true },
  { "id": "wr2", "name": "Dach Nord",            "host": "...", "password": "...", "has_grid_meter": false }
]
```

Genau **ein** Gerät sollte `has_grid_meter: true` haben (Standard, falls
weggelassen: `true` – bei nur einem konfigurierten Gerät entsprechend
unkritisch). Sobald mindestens ein Gerät explizit `false` ist, ändert sich
für die "Alle (Summe)"-Ansicht die Berechnung:

- **PV-Leistung** (Anzeige) wird weiterhin über alle Geräte summiert (jedes
  Gerät kennt zuverlässig nur seine eigenen PV-Strings).
- **Batterieleistung** (Anzeige) wird ebenfalls über alle Geräte summiert
  (nur das Gerät mit Batterie liefert überhaupt einen Wert).
- **Netzbezug/Einspeisung** werden NICHT summiert, sondern nur vom als
  `has_grid_meter: true` markierten Gerät übernommen.
- **Hausverbrauch** wird nicht mehr aus den (potenziell falschen)
  `Home_P`-Werten der Geräte summiert, sondern aus der Energiebilanz neu
  berechnet – bevorzugt über die AC-seitige Nettoleistung jedes Geräts
  (`devices:local:ac/P`, intern `ac_power_w`): `Hausverbrauch = AC-Leistung
  gesamt + Netzbezug − Einspeisung`. Das ist genauer als eine Rechnung mit
  der PV-**DC**-Erzeugung (die vor den geräteeigenen
  Umwandlungsverlusten liegt und diese sonst fälschlich als Hausverbrauch
  erscheinen lässt) und schließt das eigene Batterieladen/-entladen jedes
  Geräts automatisch mit ein. Für Messwerte von **vor** diesem Feature
  (`ac_power_w` noch nicht erfasst) greift als Fallback die ältere,
  etwas ungenauere Variante: `PV gesamt (DC) + Netzbezug − Einspeisung +
  Batterieleistung` (Batterieleistung positiv = Entladen, negativ = Laden).

Ohne diese Konfiguration (Standardfall: ein einzelnes Gerät, oder alle
Geräte unverändert `true`) bleibt das bisherige Verhalten (einfache Summe)
unverändert erhalten – es ändert sich nichts an bestehenden
Ein-Geräte-Installationen.

Diese korrigierte Berechnung gilt für Hausverbrauch, Netzbezug und
Einspeisung **überall im Dashboard** – auch in den Live-Kacheln, dem
Hauptdiagramm, dem Tagesvergleich und dem Tagesverbrauch, und zwar
unabhängig davon, welcher Tab oben (welches Einzelgerät oder "Alle
(Summe)") gerade ausgewählt ist. Grund: Hausverbrauch/Netzbezug/Einspeisung
sind hausweite Größen, die sich keinem einzelnen Wechselrichter sinnvoll
zuordnen lassen – der eigene, potenziell falsche Rohwert eines einzelnen
Geräts wird daher nie mehr angezeigt. Nur die PV-Erzeugung (und, falls
vorhanden, die Batterie) bleiben bei einem ausgewählten Einzelgerät auch
dessen eigene Werte.

Falls die Batterie-Vorzeichen-Konvention bei einem Gerät umgekehrt sein
sollte (positiv = Laden statt Entladen), zusätzlich
`"battery_power_inverted": true` bei diesem Gerät setzen (wirkt sich nur
auf den DC-Fallback aus, siehe oben).

Ohne passende Konfiguration bei mehreren Geräten gibt die App beim Start
eine Warnung in den Logs aus (`docker compose logs -f`).

**Wichtig:** Ob die korrigierte Berechnung greift, hängt nur von der
Konfiguration ab (`has_grid_meter`), nicht davon, ob der nicht gemessene
Wechselrichter für den gerade betrachteten Zeitraum tatsächlich Messwerte
in der Datenbank hat. Hatte dieser z.B. an einem bestimmten Tag einen
Ausfall (keine gespeicherten Werte), wird für diesen Tag trotzdem die
korrigierte Formel verwendet (nur eben ohne dessen PV-/Batteriebeitrag,
da dafür schlicht keine Daten vorliegen) – nicht die rohe, potenziell
falsche Home_P-Summe des Master-Geräts alleine.

**Restungenauigkeit:** Auch mit der AC-basierten Formel bleibt eine kleine
Abweichung (in der Praxis meist niedriger einstelliger Prozentbereich der
Gesamtleistung) möglich, weil Netzzähler (KSEM) und die AC-Sensoren der
einzelnen Wechselrichter nicht exakt zeitsynchron messen und PV-Erzeugung
sich (z.B. bei Wolken) innerhalb von Sekunden ändern kann – das ist eine
grundsätzliche Grenze verteilter Messtechnik, kein Fehler dieser
Berechnung. Da Hausverbrauch physikalisch nie negativ sein kann, wird ein
durch diese Restungenauigkeit rechnerisch leicht negativer Wert immer auf
0 begrenzt (nie als negative Zahl angezeigt) – das gilt für Live-Kacheln,
alle Diagramme und die Tagesverbrauch-Aufteilung nach PV/Speicher/Netz
gleichermaßen.

### Diagnose: welches Gerät hat den echten Zähler?

Mit dem mitgelieferten Diagnose-Werkzeug lassen sich die Rohwerte eines
Geräts direkt einsehen (rein lesend, verändert nichts):

```bash
docker compose exec kostal-monitor python -m app.debug_live --device-id wr1
```

Besonders relevant: `devices:local/Grid_P` (sollte mit dem echten
Netzbezug/der Einspeisung im Kostal-Portal übereinstimmen – am besten
gleichzeitig geöffnet halten) sowie `devices:local:powermeter/*` (falls
vorhanden: das ist meist der direkt durchgereichte KSEM-Wert, zur
Gegenprobe). Stimmen diese Werte mit der Portal-Anzeige überein, ist das
Gerät der richtige Kandidat für `has_grid_meter: true`.

### Diagnose: unplausibler Wert an einem vergangenen Tag

`debug_live.py` zeigt nur AKTUELLE Werte. Wirkt eine Kennzahl für einen
vergangenen Tag unplausibel (z.B. "0 kWh aus PV, komplett aus dem
Speicher" an einem Tag, an dem das kaum sein kann), zeigt
`debug_day.py` die dafür gespeicherten Rohmesswerte je Gerät (inkl. Anzahl
fehlender Werte) sowie die daraus berechnete "Alle (Summe)"-Bilanz und die
Tagesverbrauch-Aufteilung – also genau das, was auch das Dashboard für
diesen Tag anzeigen würde:

```bash
docker compose exec kostal-monitor python -m app.debug_day --date 2026-07-11
```

### Datenlücken: keine Interpolation über große Zeitlücken hinweg

Mit `debug_day.py` wurde ein konkreter Fall gefunden: an einem Tag mit
vielen fehlenden Netzbezugswerten (Zähler-Abfrage zeitweise fehlgeschlagen)
ergab die Tagessumme über 100 kWh PV-Ertrag für eine deutlich kleinere
Anlage – die Ursache war, dass `integrate_kwh()` (die Trapezregel-Funktion
hinter allen kWh-Summen dieser App) den letzten bekannten Leistungswert
über mehrstündige Datenlücken hinweg linear fortgeschrieben hat, statt die
Lücke als "unbekannt" zu behandeln. Ab jetzt wird ein Intervall zwischen
zwei Messpunkten, das mehr als 30 Minuten auseinanderliegt, nicht mehr
interpoliert, sondern übersprungen (trägt 0 zur Summe bei) – das
unterschätzt die tatsächliche Energiemenge in der Lücke leicht, ist aber
deutlich näher an der Wahrheit als eine grobe Fortschreibung über Stunden.
Betrifft alle kWh-Summen der App (Tagesverbrauch, PV-Ertrag,
Tagesvergleich, "heute"-Kacheln als Fallback), nicht nur die neue
PV/Speicher/Netz-Aufteilung.

### Neues Feld ac_power_w (automatische Datenbank-Migration)

Für die AC-basierte Hausverbrauchs-Berechnung erfasst die App seit diesem
Update zusätzlich `devices:local:ac/P` je Wechselrichter (`ac_power_w`). Die
bestehende `readings`-Tabelle wird beim nächsten Start automatisch um diese
Spalte ergänzt (einfaches `ALTER TABLE`, keine bestehenden Daten gehen
verloren oder werden verändert) – ein normales `docker compose up -d
--build` reicht aus. Für Messwerte von vor diesem Update bleibt das Feld
leer (`NULL`); für diese greift automatisch die ältere Fallback-Formel
(siehe oben).

## Daten sichern

Die komplette Historie liegt in `./data/kostal.db` (SQLite-Datei). Für ein
Backup reicht es, diese Datei zu kopieren (idealerweise bei gestopptem
Container, damit keine Schreiboperation mittendrin ist).

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

## Tests

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
Abschnitt "Polling stoppt nachts" oben), und die korrigierte
Energiebilanz-Berechnung bei mehreren Wechselrichtern (siehe "Mehrere
Wechselrichter: Hausverbrauch/Netz korrekt berechnen") ist sowohl auf
Ebene der Aggregations-Funktionen als auch End-to-End über die echten API-
Endpunkte getestet, anhand echter, per `debug_live.py` ausgelesener
Rohwerte.

## Grenzen / mögliche Erweiterungen

- Aktuell wird nur eine feste Auswahl an Prozessdaten erfasst (Verbrauch,
  Netz, PV, Batterie). Weitere Werte (z.B. je String) lassen sich in
  `PROCESS_DATA_CANDIDATES` in `backend/app/plenticore_client.py` ergänzen.
- Die SQLite-Datei wächst mit der Zeit (bei 15s-Intervall und 2 Geräten ca.
  11.000 Zeilen/Tag). Für viele Jahre Historie wäre irgendwann ein Umzug auf
  PostgreSQL oder eine Zeitreihen-DB sinnvoll – die Datenzugriffsschicht ist
  bewusst einfach gehalten, damit das leicht austauschbar bleibt.
- Die Benutzerverwaltung ist bewusst einfach gehalten (kein 2FA, kein
  Passwort-Reset per E-Mail, feste Rollen admin/betreiber) – siehe Abschnitt
  "Benutzerverwaltung / Login" oben für Details und Empfehlungen bei
  Zugriff von außerhalb des eigenen Netzes.
