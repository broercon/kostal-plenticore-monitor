"""Tests fuer die korrigierte Energiebilanz-Logik in combine_devices()/
combine_latest_readings() (aggregation.py) - der Fix fuer einen konkreten
Praxis-Bug: bei zwei Wechselrichtern am selben Hausanschluss (einer mit
Batterie + echtem Netzzaehler/KSEM, ein zweiter ohne eigenen Zaehler, der
per AC die Batterie des ersten mitlaedt) berechnete das einfache Summieren
der von jedem Geraet gemeldeten Home_P-Werte einen stark negativen,
unsinnigen Gesamt-Hausverbrauch, weil der Master-Wechselrichter nichts von
der Einspeisung des zweiten Geraets weiss und sie faelschlich als
"Ladung aus dem Netz" verbucht.

Die Testwerte unten sind bewusst nah an echten, per app/debug_live.py vom
Nutzer ausgelesenen Rohwerten gewaehlt (WR mit Batterie: Grid_P ~41 W,
Home_P ~-1372 W (kaputt), battery/P ~-2597 W beim Laden; zweiter WR ohne
Zaehler liefert ~1300 W PV), damit der Test den echten Fall abbildet statt
nur ein abstraktes Beispiel.

Zweite Iteration: die anfaengliche Formel (PV_gesamt [DC] + Netzbezug -
Einspeisung + Batterie) unterschaetzte Wechselrichter-eigene DC->AC-
Umwandlungsverluste als "Hausverbrauch" (bei einer realen Messung ca.
500 W "Phantom-Verbrauch", obwohl das KSEM-Portal 0 W Hausverbrauch zeigte).
Die bevorzugte Formel nutzt seitdem die AC-seitige Nettoleistung
(ac_power_w, aus devices:local:ac/P) statt der DC-PV-Summe - das reduzierte
den Fehler in der echten Messung auf ca. -160 bis -345 W (Rest ist
Mess-/Zeitversatz zwischen zwei nicht ganz gleichzeitigen Abfragen). Die
alte Formel bleibt als Fallback fuer Messwerte von vor diesem Feature
(ac_power_w noch nicht erfasst, also NULL) erhalten.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.aggregation import (
    HISTORY_FIELDS,
    combine_devices,
    combine_latest_readings,
    daily_home_source_breakdown_kwh,
)
from app.models import Reading


def _bucket_row(**overrides) -> dict:
    row = {field: None for field in HISTORY_FIELDS}
    row.update(overrides)
    return row


def test_combine_devices_default_naive_sum_unchanged():
    """Ohne has_grid_meter-Angabe (oder wenn alle Geraete True sind) bleibt
    das alte Verhalten erhalten: einfach ueber alle Geraete summieren -
    wichtig fuer Rueckwaertskompatibilitaet bestehender Installationen."""
    per_device = {
        "wr1": {0: _bucket_row(home_power_w=100.0, pv_power_w=200.0)},
        "wr2": {0: _bucket_row(home_power_w=50.0, pv_power_w=80.0)},
    }
    combined = combine_devices(per_device)
    assert combined[0]["home_power_w"] == 150.0
    assert combined[0]["pv_power_w"] == 280.0

    # Explizit alle True uebergeben muss dasselbe Ergebnis liefern.
    combined_explicit = combine_devices(per_device, {"wr1": True, "wr2": True})
    assert combined_explicit[0]["home_power_w"] == 150.0
    assert combined_explicit[0]["pv_power_w"] == 280.0


def test_combine_devices_ac_based_formula_matches_real_world_scenario():
    """Nachgebaut aus einer echten Messung mit ruhender (voller, 100%,
    weder ladender noch entladender) Batterie: WR1 hat den echten
    Netzzaehler, WR2 hat keinen Zaehler und speist trotzdem mit ein.
    devices:local:ac/P (ac_power_w) ist bei beiden Geraeten vorhanden ->
    die bevorzugte AC-basierte Formel muss verwendet werden (nicht der
    DC-PV-Fallback), und WR2s erfundener Grid-Wert wird ignoriert.

    Die rohe Energiebilanz ergibt hier rechnerisch leicht NEGATIV (-345 W,
    Mess-/Zeitversatz zwischen KSEM und Wechselrichter-Sensoren) - das ist
    nahe an 0 (echter Hausverbrauch laut KSEM-Portal), aber Hausverbrauch
    kann physikalisch nicht negativ sein. combine_devices() begrenzt einen
    solchen Wert daher auf 0, statt eine negative Zahl zurueckzugeben."""
    per_device = {
        "wr1": {
            0: _bucket_row(
                pv_power_w=6443.5,  # DC, wird NICHT fuer Home verwendet
                ac_power_w=6323.6,
                home_power_w=-4050.4,  # WR1s eigene (kaputte) Berechnung
                grid_draw_power_w=0.0,
                feed_in_power_w=10474.6,
                battery_power_w=0.0,  # Batterie voll, ruht gerade
            )
        },
        "wr2": {
            0: _bucket_row(
                pv_power_w=3927.2,
                ac_power_w=3806.0,
                home_power_w=0.0,
                # WR2 hat "kein Sensor verwendet" - dieser Wert ist erfunden
                # und darf nicht einfliessen.
                grid_draw_power_w=0.0,
                feed_in_power_w=3806.0,
                battery_power_w=None,  # kein eigener Speicher
            )
        },
    }
    has_grid_meter = {"wr1": True, "wr2": False}

    combined = combine_devices(per_device, has_grid_meter)
    point = combined[0]

    assert point["ac_power_w"] == 6323.6 + 3806.0
    # Nur WR1s Netzwerte werden verwendet, WR2s erfundene 3806 W Einspeisung
    # taucht hier nicht auf.
    assert point["feed_in_power_w"] == 10474.6
    assert point["grid_draw_power_w"] == 0.0

    raw_home = (6323.6 + 3806.0) - 10474.6 + 0.0
    assert raw_home < 0  # rechnerisch leicht negativ (Restungenauigkeit)
    # combine_devices() begrenzt das auf 0 - nie eine negative Zahl.
    assert point["home_power_w"] == 0.0


def test_combine_devices_falls_back_to_dc_pv_formula_without_ac_power():
    """Messwerte von VOR dem ac_power_w-Feature (z.B. importierte Altdaten)
    haben ac_power_w=None - dann greift die alte, etwas ungenauere Formel
    (PV_gesamt [DC] + Netzbezug - Einspeisung + Batterie), nicht ein
    fehlender/kaputter Wert."""
    per_device = {
        "wr1": {
            0: _bucket_row(
                pv_power_w=1332.9,
                ac_power_w=None,
                home_power_w=-1371.8,  # WR1s eigene (kaputte) Berechnung
                grid_draw_power_w=40.7,
                feed_in_power_w=0.0,
                battery_power_w=-2597.5,  # laedt gerade
            )
        },
        "wr2": {
            0: _bucket_row(
                pv_power_w=1300.0,
                ac_power_w=None,
                home_power_w=500.0,  # irrelevant, wird ignoriert
                grid_draw_power_w=9999.0,  # darf NICHT einfliessen (kein Zaehler)
                feed_in_power_w=0.0,
                battery_power_w=None,  # kein eigener Speicher
            )
        },
    }
    has_grid_meter = {"wr1": True, "wr2": False}

    combined = combine_devices(per_device, has_grid_meter)
    point = combined[0]

    assert point["pv_power_w"] == 1332.9 + 1300.0
    # Nur WR1s Grid-Werte werden verwendet, WR2s 9999 W tauchen nicht auf.
    assert point["grid_draw_power_w"] == 40.7
    assert point["feed_in_power_w"] == 0.0
    assert point["battery_power_w"] == -2597.5

    expected_home = (1332.9 + 1300.0) + 40.7 - 0.0 + (-2597.5)
    assert point["home_power_w"] == expected_home
    assert expected_home > 0  # plausibler (kleiner, positiver) Hausverbrauch
    assert point["home_power_w"] != -1371.8  # nicht mehr der kaputte WR1-Rohwert


def test_combine_devices_battery_power_inverted_flips_sign():
    per_device = {
        "wr1": {
            0: _bucket_row(
                pv_power_w=1000.0,
                grid_draw_power_w=0.0,
                feed_in_power_w=0.0,
                battery_power_w=500.0,  # bei umgekehrter Konvention: Laden
            )
        },
        "wr2": {0: _bucket_row(pv_power_w=0.0, grid_draw_power_w=0.0, feed_in_power_w=0.0)},
    }
    has_grid_meter = {"wr1": True, "wr2": False}
    battery_inverted = {"wr1": True}

    combined = combine_devices(per_device, has_grid_meter, battery_inverted)
    # 500 W wird zu -500 W (jetzt "Laden" in der App-eigenen Konvention).
    assert combined[0]["battery_power_w"] == -500.0
    assert combined[0]["home_power_w"] == 1000.0 + 0.0 - 0.0 + (-500.0)


def test_combine_devices_no_metered_device_falls_back_to_all():
    """Sicherheitsnetz: wenn (fehlerhaft) ALLE Geraete has_grid_meter=False
    haben, lieber alle fuer die Netzwerte verwenden als gar keinen Wert."""
    per_device = {
        "wr1": {0: _bucket_row(pv_power_w=100.0, grid_draw_power_w=10.0, feed_in_power_w=0.0)},
        "wr2": {0: _bucket_row(pv_power_w=200.0, grid_draw_power_w=20.0, feed_in_power_w=0.0)},
    }
    combined = combine_devices(per_device, {"wr1": False, "wr2": False})
    assert combined[0]["grid_draw_power_w"] == 30.0
    assert combined[0]["pv_power_w"] == 300.0


def test_combine_latest_readings_matches_combine_devices():
    readings = [
        {
            "device_id": "wr1",
            "pv_power_w": 1332.9,
            "home_power_w": -1371.8,
            "grid_draw_power_w": 40.7,
            "feed_in_power_w": 0.0,
            "battery_power_w": -2597.5,
        },
        {
            "device_id": "wr2",
            "pv_power_w": 1300.0,
            "home_power_w": 500.0,
            "grid_draw_power_w": 9999.0,
            "feed_in_power_w": 0.0,
            "battery_power_w": None,
        },
    ]
    has_grid_meter = {"wr1": True, "wr2": False}

    combined = combine_latest_readings(readings, has_grid_meter)
    assert combined is not None
    assert combined["pv_power_w"] == 1332.9 + 1300.0
    assert combined["grid_draw_power_w"] == 40.7
    expected_home = (1332.9 + 1300.0) + 40.7 - 0.0 + (-2597.5)
    assert combined["home_power_w"] == expected_home


def test_combine_latest_readings_empty_list_returns_none():
    assert combine_latest_readings([]) is None


def test_combine_devices_clamps_negative_home_to_zero_corrected_logic():
    """Hausverbrauch kann physikalisch nicht negativ sein. Ergibt die
    korrigierte Energiebilanz (AC- oder DC-Fallback-Formel) dennoch einen
    negativen Wert (Mess-/Zeitversatz o.ae.), muss combine_devices() 0.0
    zurueckgeben - nie eine negative Zahl."""
    per_device = {
        "wr1": {
            0: _bucket_row(
                ac_power_w=1000.0,
                grid_draw_power_w=0.0,
                feed_in_power_w=2000.0,  # deutlich mehr als ac_power_w -> negativ
                battery_power_w=0.0,
            )
        },
        # Zweites Geraet nur, um die korrigierte Energiebilanz-Logik
        # ueberhaupt zu aktivieren (die greift erst, sobald mindestens ein
        # Geraet explizit has_grid_meter=False hat) - traegt selbst nichts bei.
        "wr2": {0: _bucket_row()},
    }
    combined = combine_devices(per_device, {"wr1": True, "wr2": False}, None)
    assert combined[0]["home_power_w"] == 0.0


def test_combine_devices_clamps_negative_home_to_zero_naive_logic():
    """Dieselbe physikalische Grenze gilt auch fuer die einfache
    Standard-Summe (kein Geraet mit has_grid_meter=False konfiguriert) -
    z.B. bei einem einzelnen Wechselrichter, der kurzzeitig einen leicht
    negativen Home_P meldet (Sensorrauschen)."""
    per_device = {
        "wr1": {0: _bucket_row(home_power_w=-12.5, pv_power_w=100.0)},
    }
    combined = combine_devices(per_device)
    assert combined[0]["home_power_w"] == 0.0


def test_daily_home_source_breakdown_never_negative_even_with_bad_point():
    """Regressionstest fuer den vom Nutzer gemeldeten Bug: ein Tag mit einem
    (durch Mess-/Zeitversatz) rechnerisch leicht negativen Hausverbrauch
    darf im gestapelten Tagesverbrauch-Diagramm NIE zu einer negativen
    'Aus Netz'-Saeule fuehren. Zwei Messpunkte am selben Tag: einer normal
    (PV deckt den Hausverbrauch), einer mit negativem Home_P (Einspeisung
    minimal groesser als PV+Netzbezug, wie es durch Zeitversatz vorkommen
    kann)."""
    base = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    rows = [
        Reading(
            device_id="_combined_",
            device_name="_combined_",
            timestamp=base,
            home_power_w=1000.0,
            grid_draw_power_w=0.0,
            feed_in_power_w=0.0,
            pv_power_w=1000.0,
        ),
        Reading(
            device_id="_combined_",
            device_name="_combined_",
            timestamp=base + timedelta(minutes=10),
            home_power_w=-50.0,  # physikalisch unmoeglich, Restungenauigkeit
            grid_draw_power_w=0.0,
            feed_in_power_w=100.0,
            pv_power_w=100.0,
        ),
    ]
    days = daily_home_source_breakdown_kwh(rows, "Europe/Berlin")
    assert len(days) == 1
    day = days[0]
    for key in ("pv_kwh", "battery_kwh", "grid_kwh"):
        assert day[key] is not None
        assert day[key] >= 0
