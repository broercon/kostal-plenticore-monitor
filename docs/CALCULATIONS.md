# Dashboard und Berechnungen

[Zurück zum README](../README.md)

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
1 Tag bis 30 Tage – Standard beim Start ist bewusst "1 Tag" (nur der
aktuellste Tag), passend zum Leistungsverlauf ("24 Std") und zum
Wechselrichter-Vergleich ("1 Tag"); weitere Tage lassen sich jederzeit über
die Buttons dazuwählen.

Drei Kennzahlen stehen zur Auswahl:

- **PV-Erzeugung** – reine PV-Leistungskurve je Tag.
- **Verbrauch aus Solar & Batterie** – Hausverbrauch aufgeteilt in den
  Anteil, der direkt aus PV gedeckt wurde (durchgezogene Linie), und den
  Anteil aus der Batterie (gestrichelte Linie, gleiche Farbe wie der
  zugehörige Tag). Die Aufteilung wird rein aus der Leistungsbilanz
  berechnet (PV + Netzbezug + Batterie = Hausverbrauch + Einspeisung) und
  braucht daher Netzbezugs-/Einspeisungswerte – bei importierten Altdaten
  ohne Netzmessung bleibt sie leer, es
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
hausweite Größe (siehe [Berechnungen bei mehreren Wechselrichtern](CALCULATIONS.md#mehrere-wechselrichter-hausverbrauchnetz-korrekt-berechnen)) – bei
mehreren konfigurierten Wechselrichtern zeigt dieses Diagramm daher immer
die Gesamtsumme, unabhängig vom oben ausgewählten Tab.

## Autarkiegrad

Der Reiter "Autarkie" zeigt den **Autarkiegrad** – welcher Anteil des
Hausverbrauchs aus eigener Erzeugung (PV, direkt oder über die Batterie
zwischengespeichert) statt aus dem Netz gedeckt wurde – je Kalendermonat als
Balkendiagramm, seit dem allerersten gespeicherten Messwert. Zusätzlich zeigt
die Übersicht oben eine Kachel "Autarkiegrad heute" mit dem Wert für den
laufenden Tag.

Berechnung: `Autarkiegrad = (PV-Anteil + Speicher-Anteil) / Hausverbrauch
gesamt`, in Prozent – dieselbe PV-/Speicher-/Netz-Aufteilung, die auch das
"Tagesverbrauch"-Diagramm verwendet (siehe oben,
`daily_home_source_breakdown_kwh`). Ein Monatswert ist dabei **nicht** der
Mittelwert der täglichen Prozentsätze, sondern wird aus den über den Monat
aufsummierten kWh-Anteilen gebildet – sonst würden Tage mit wenig
Hausverbrauch (z.B. Abwesenheit) das Monatsergebnis unverhältnismäßig
verzerren, obwohl sie kaum zum tatsächlichen Monatsverbrauch beitragen. Wie
beim Tagesverbrauch ist dies eine hausweite Größe, unabhängig vom oben
gewählten Wechselrichter-Tab.

Wie bei der PV-Ertrag-/Einspeisungs-Übersicht (siehe "Performance:
Energie-Zeitraum-Cache" unten) werden abgeschlossene Tage über den
`daily_energy_cache` zwischengespeichert – nur der laufende Monat/Tag wird
bei jeder Anfrage frisch berechnet. Kalendermonate ganz ohne Messwerte (z.B.
vor Inbetriebnahme) fehlen in der Übersicht, statt mit 0 % aufzutauchen.
Für Zeiträume ohne tatsächlich gespeicherte Netzmessung wird ebenfalls kein
Autarkiegrad ausgewiesen. Insbesondere ältere Importdaten ohne KSEM-Werte
dürfen dadurch nicht fälschlich als 100 % autark erscheinen.

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
auf den [DC-Fallback](#die-lösung-dieser-app) aus).

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
leer (`NULL`); für diese greift automatisch die [ältere Fallback-Formel](#die-lösung-dieser-app).

## Kennzahlen: PV-Ertrag, Hausverbrauch & Einspeisung – wie sie berechnet werden

Damit dieselbe Größe überall denselben Wert zeigt, gelten feste Regeln.

### PV-Ertrag = reine PV-Erzeugung

Der PV-Ertrag (Kachel „PV-Ertrag heute", Wechselrichter-Tabelle,
PV-Ertrag-Übersicht je Zeitraum, Kurve „PV-Leistung" im Leistungsverlauf und
der Mail-Report) ist die **reine Erzeugung der PV-Module**, integriert aus
der Momentanleistung (Trapezregel).

Bewusst **nicht** der geräteeigene Tageszähler `Statistic:Yield:Day`: dieser
misst den *Wechselrichter-Ausgang* und enthält bei Hybrid-Geräten auch die
Batterieentladung. Er zeigt deshalb nachts einen „PV-Ertrag" > 0, obwohl die
Module nichts erzeugen.

**Batterie am PV3-String:** Hängt die Batterie am dritten PV-Eingang (PV3),
steckt ihre Leistung bereits in `pv_power_w` (= pv1 + pv2 + pv3). Die reine
PV wird deshalb als `pv_power_w − battery_power_w` berechnet. Beide Werte
werden roh gespeichert, die Subtraktion ist damit vorzeichensicher
(unabhängig von Laden/Entladen) und auf ≥ 0 begrenzt; nachts ergibt sich so
0. Der Gesamtwert („Alle (Summe)") ist die Summe der Wechselrichter, da PV
additiv ist.

### Gerätezähler vs. Integration

Für Hausverbrauch (`Statistic:EnergyHome:Day`) und Einspeisung/Netz wird der
geräteeigene Tageszähler als maßgeblich genutzt, sofern vorhanden. Fehlt er
(z.B. bei per Logdaten-Import eingespielten Altdaten), wird aus den
gespeicherten Momentanleistungen integriert. Die Integration ist eine
Näherung und kann um wenige Prozent vom Zählerwert abweichen.

### Hausverbrauch nach Quelle

Die Aufschlüsselung des Hausverbrauchs in PV / Batterie / Netz wird immer per
Integration aus der Energiebilanz gebildet und summiert sich (bis auf
Rundung) zum Hausverbrauch des Tages. Bei **mehreren** Wechselrichtern nutzen
sowohl die Kachel „Hausverbrauch heute" als auch die Aufschlüsselung dieselbe
integrierte, korrigierte Hausbilanz und stimmen überein. Bei **nur einem**
Wechselrichter kann die Kachel (Gerätezähler) minimal von der Summe der drei
Anteile (Integration) abweichen – zwei legitime Methoden derselben Größe.

## Performance: Energie-Zeitraum-Cache

Die Zeitraum-Übersichten (PV-Ertrag und Einspeisung von "heute" bis
"letztes Jahr") integrieren die Rohmesswerte je Kalendertag. Damit das
Dashboard sie nicht bei jeder automatischen Aktualisierung (alle 5 Minuten)
komplett neu aus sämtlichen Rohmesswerten seit Anfang des Vorjahres
berechnen muss (bei 15s-Poll-Intervall potenziell mehrere Millionen Zeilen
pro Anfrage), werden abgeschlossene (vergangene) Kalendertage in der
Tabelle `daily_energy_cache` zwischengespeichert - nur "heute" (der noch
laufende, sich ändernde Tag) wird bei jeder Anfrage frisch berechnet.

Ein [nachträglicher Logdaten-Import](DATA_IMPORT.md), der rückwirkend Messwerte
für vergangene Tage ergänzt, invalidiert automatisch die betroffenen
Cache-Einträge (siehe `app/auto_import.py`), damit sich geänderte
Altdaten auch tatsächlich in den Übersichten niederschlagen.

Zusätzlich läuft die Datenbank im SQLite-WAL-Modus (`PRAGMA
journal_mode=WAL`), damit lesende Zugriffe (Dashboard/API) und der
alle paar Sekunden schreibende Poller sich nicht gegenseitig blockieren,
sowie mit einem zusätzlichen Index rein auf `readings.timestamp` (der
bestehende zusammengesetzte Index beginnt mit `device_id` und hilft
Abfragen ohne Geräte-Filter kaum). Beide Änderungen wirken automatisch
auch auf Bestandsdatenbanken (siehe `app/database.py`), ein manueller
Migrationsschritt ist nicht nötig.
