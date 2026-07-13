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
"""
from __future__ import annotations

from app.aggregation import HISTORY_FIELDS, combine_devices, combine_latest_readings


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


def test_combine_devices_corrected_energy_balance_matches_real_world_scenario():
    """Nachgebaut aus echten Werten (siehe Modul-Docstring): WR1 hat den
    echten Netzzaehler und die Batterie, WR2 hat keinen Zaehler und laedt
    per AC mit. Erwartet: Home_P wird NEU aus der Energiebilanz berechnet
    (nicht aus WR1s kaputtem, negativem Home_P uebernommen), und WR2s
    Grid-Werte werden komplett ignoriert."""
    per_device = {
        "wr1": {
            0: _bucket_row(
                pv_power_w=1332.9,
                home_power_w=-1371.8,  # WR1s eigene (kaputte) Berechnung
                grid_draw_power_w=40.7,
                feed_in_power_w=0.0,
                battery_power_w=-2597.5,  # laedt gerade
            )
        },
        "wr2": {
            0: _bucket_row(
                pv_power_w=1300.0,
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
