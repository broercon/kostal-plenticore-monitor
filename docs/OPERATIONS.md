# Betrieb und Fehlerdiagnose

[Zurück zum README](../README.md)

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

Zusätzlich gibt es einen **Watchdog**: Kommt über mehrere Minuten kein
einziger erfolgreicher Abruf zustande (Standard: `max(5 Minuten, 20 ×
POLL_INTERVAL_SECONDS)`), startet die App das Polling **intern selbst neu** –
sie bricht den womöglich hängenden Abruf ab, baut die Geräteverbindungen
frisch auf und pollt weiter, ohne dass ein manueller Container-Neustart
(`docker compose up -d --build`) nötig ist. Das fängt auch seltene *Hänger*
ab, bei denen der Prozess zwar noch läuft (also `restart: unless-stopped`
nicht greift), aber keine neuen Daten mehr liefert. Falls ein
Wechselrichter nachts nur schläft, ist der Neustart unschädlich – es wird
dann einfach weiter erfolglos versucht, bis das Gerät morgens wieder
antwortet.

## Daten sichern

Die komplette Historie liegt in `./data/kostal.db` (SQLite-Datei). Für ein
Backup reicht es, diese Datei zu kopieren (idealerweise bei gestopptem
Container, damit keine Schreiboperation mittendrin ist).
