# Datenerfassung und Historienimport

[Zurück zum README](../README.md)

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
[Alte Daten nachträglich importieren](#alte-daten-nachträglich-importieren).

Der heutige PV-Ertrag wird immer aus den seit lokaler Mitternacht
gespeicherten Leistungswerten integriert. Dadurch wird bei Hybridgeräten die
Batterieentladung nicht fälschlich als PV-Ertrag gezählt. Für Hausverbrauch
und Einspeisung verwendet die App bevorzugt die Tageszähler des Geräts und
fällt nur bei fehlenden Werten auf die Integration zurück.

Ein frisch gestarteter Container kennt bei integrierten Kennzahlen nur den
seit dem Start erfassten Teil des Tages, sofern die früheren Messpunkte nicht
über den Historienimport ergänzt wurden.

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
der interne Logger der letzten `AUTO_IMPORT_DAYS` Tage (Standard: 35)
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
dafür keine Werte (KSEM-Limitation). Für PV-Erzeugung und
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

Weitere optionale Argumente sind `--port`, `--device-name`,
`--raw-lines` und `--raw-tail`. Die beiden `--raw-*`-Optionen dienen der
Formatdiagnose und geben Ausschnitte der vom Wechselrichter gelieferten
Rohdaten aus. Ohne `--commit` bleibt jeder Aufruf eine reine Vorschau.
