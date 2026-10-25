# TODO: Speicher-Erweiterung (BYD HVM 11,04 → 13,8 / 16,56 kWh)

Branch: `feature/speicher-erweiterung`. Alle Punkte müssen am Ende des Branches
abgehakt sein; diese Datei wird vor dem Merge gelöscht.

Ziel: Eine neue Ansicht „Speicher-Erweiterung“ unter „Auswertungen“, die aus
den gemessenen Daten simuliert, wie viel Netzbezug ein größerer Speicher
(+1 / +2 Module) vermieden hätte. Sie wächst automatisch mit jedem neuen Tag.
Fehlende Monate werden nicht hochgerechnet, sondern als „ohne Daten“ gezeigt.

## Datenbasis
- [x] Tages-Eingangsreihen in 5-Minuten-Schritten aufbereiten: reine PV (alle
      Wechselrichter), Hausverbrauch (korrigierte Bilanz), Netzbezug,
      Einspeisung, Batterieleistung, Ladestand
- [x] Cache-Tabelle für abgeschlossene Tage (einmal berechnen, danach nur lesen)
- [x] Cache nach Logdaten-Import für die betroffenen Tage verwerfen (`auto_import.py`)
- [x] Datenlücken je Tag erkennen und als Abdeckung ausweisen

## Simulation
- [x] Speichermodell: Laden nur aus PV-Überschuss, Entladen bis zur Mindestladung,
      Ladestand über Tagesgrenzen fortführen, mehrere Zyklen pro Tag
- [x] Lade-/Entladeleistung modulabhängig (Strombegrenzung × Modulspannung),
      zusätzlich durch Wechselrichter begrenzbar
- [x] Lade- und Entladewirkungsgrad getrennt, Standby-Verbrauch
- [x] Drei Varianten mit identischer Logik: 11,04 / 13,8 / 16,56 kWh
      (vorhandene Module mit kalibrierter nutzbarer Kapazität, neue mit Nennwert)

## Kalibrierung und Validierung
- [x] Mindestladung, Leistungsgrenzen, Wirkungsgrade und nutzbare Kapazität (SoH)
      aus den Messdaten ableiten
- [x] 11,04-kWh-Modell gegen gemessenen Ladestand und Netzbezug prüfen,
      Abweichung ausweisen
- [x] Netzladung erkennen (Laden ohne ausreichend PV bei Netzbezug), Menge ausweisen
      und aus der Validierung herausrechnen
- [x] Mögliche Abregelung (Einspeisung an der Begrenzung) erkennen und als Hinweis
      mit Zeitumfang ausweisen

## Auswertung
- [x] Vermiedener Netzbezug je Variante: gesamt und je Monat
- [x] Zusatznutzen des zweiten Moduls ((+2) − (+1)): gesamt und je Monat
- [x] Nutzungshäufigkeit je Kalendertag (ungenutzt < 5 %, teilweise, vollständig ≥ 95 %,
      > 100 % gesondert), Anzahl Tage gesamt und je Monat
- [x] Liste der Annahmen und fehlenden Daten, Datenabdeckung (Tage, Monate ohne Daten)
- [x] Finanzielle Eingaben (Erweiterungskosten, Strompreis, Einspeisevergütung) als
      optionale Felder; Bewertung nur, wenn alle drei gesetzt sind

## API und Einstellungen
- [x] Endpunkt `GET /api/readings/battery-sizing`
- [x] Admin-Einstellungen (Mindestladung, Leistungsgrenzen, Wirkungsgrade, Standby,
      Einspeisebegrenzung, Modulgröße, Preise); leer = aus den Daten ableiten
- [x] `GET/PUT /api/admin/battery-sizing/config`

## Frontend
- [x] Neue Unteransicht „Speicher-Erweiterung“ im Tab „Auswertungen“
- [x] Kennzahlen: vermiedener Netzbezug +1 / +2 Module, Zusatznutzen 2. Modul
- [x] Monatsdiagramm mit zwei Balken je Monat (+1 / +2), Monate ohne Daten erkennbar
- [x] Diagramm/Tabelle Zusatznutzen 2. Modul je Monat
- [x] Tabelle Nutzungshäufigkeit je Monat und Variante
- [x] Kasten Validierung, Datenabdeckung, Annahmen, Hinweis BYD-Erweiterungsvorgaben
- [x] Admin-Formular für die Einstellungen

## Qualität und Abschluss
- [x] Backend-Tests (Simulation, Kalibrierung, Kategorien, Cache, Endpunkt, Admin)
- [x] Frontend-Tests (Ansicht, Diagramm, Admin-Formular)
- [x] Gegen die echten Daten laufen lassen und Ergebnisse auf Plausibilität prüfen
- [x] Doku: `docs/CALCULATIONS.md`, `docs/API.md`, README
- [x] Komplette Test-Suiten grün
- [ ] Diese TODO-Datei löschen (beim Merge)
