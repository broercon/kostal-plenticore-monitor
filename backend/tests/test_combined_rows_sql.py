"""Kreuzvergleich: Verdichtung der Messwerte in SQL vs. in Python.

daily_summary._load_per_device_buckets() bildet dasselbe wie
aggregate_per_device(_load_readings_range(...), 60), nur als GROUP BY in
der Datenbank - damit bei mehreren Wechselrichtern nicht erst saemtliche
Rohmesswerte nach Python wandern muessen (gemessen an 35 Tagen Historie:
395.000 Rohmesswerte gegenueber 100.800 Minuten-Buckets, 5.197 ms
gegenueber 1.683 ms).

Zwei Implementierungen derselben Sache laufen auseinander, sobald jemand
nur eine davon anfasst. Dieser Test vergleicht sie deshalb gegen dieselben
Beispieldaten - dasselbe Vorgehen wie bei
test_energy_forecast.test_pure_pv_sql_matches_python_helper.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.aggregation import HISTORY_FIELDS, aggregate_per_device
from app.config import InverterConfig
from app.config import settings as app_settings
from app.daily_summary import (
    _COMBINE_BUCKET_SECONDS,
    _combined_rows,
    _load_per_device_buckets,
    _load_readings_range,
    _load_rows_for_range,
)
from app.database import SessionLocal
from app.models import Reading

WR1 = InverterConfig(
    id="wr1", name="Dach Sued (Batterie)", host="192.0.2.1", password="x", has_grid_meter=True
)
WR2 = InverterConfig(
    id="wr2", name="Dach Nord (kein Zaehler)", host="192.0.2.2", password="x", has_grid_meter=False
)

START = date(2026, 6, 1)
ENDE = date(2026, 6, 3)


def _seed() -> None:
    """Messwerte mit allem, was die Verdichtung auseinanderbringen kann:
    mehrere Geraete, mehrere Punkte im selben Minuten-Bucket, Luecken,
    einzelne fehlende Felder und ein Feld, das fuer einen ganzen Bucket
    fehlt (dort muss NULL herauskommen, nicht 0)."""
    basis = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    zeilen = [
        # Bucket 10:00 - zwei Punkte je Geraet, Mittelwert erwartet
        Reading(device_id="wr1", device_name="A", timestamp=basis,
                home_power_w=1000.0, pv_power_w=500.0, battery_power_w=-100.0,
                grid_draw_power_w=200.0, feed_in_power_w=0.0, ac_power_w=400.0),
        Reading(device_id="wr1", device_name="A", timestamp=basis + timedelta(seconds=30),
                home_power_w=2000.0, pv_power_w=700.0, battery_power_w=-300.0,
                grid_draw_power_w=400.0, feed_in_power_w=0.0, ac_power_w=600.0),
        Reading(device_id="wr2", device_name="B", timestamp=basis + timedelta(seconds=10),
                home_power_w=None, pv_power_w=800.0, battery_power_w=None,
                grid_draw_power_w=None, feed_in_power_w=50.0, ac_power_w=None),
        # Bucket 10:01 - nur ein Geraet, ein Feld fehlt komplett
        Reading(device_id="wr1", device_name="A", timestamp=basis + timedelta(minutes=1),
                home_power_w=1500.0, pv_power_w=None, battery_power_w=0.0,
                grid_draw_power_w=100.0, feed_in_power_w=0.0, ac_power_w=None),
        # Luecke, dann Bucket 10:05 am naechsten Tag
        Reading(device_id="wr2", device_name="B", timestamp=basis + timedelta(days=1, minutes=5),
                home_power_w=900.0, pv_power_w=0.0, battery_power_w=250.0,
                grid_draw_power_w=900.0, feed_in_power_w=0.0, ac_power_w=-250.0),
    ]
    db = SessionLocal()
    try:
        db.add_all(zeilen)
        db.commit()
    finally:
        db.close()


def test_sql_buckets_match_the_python_helper(client):
    _seed()

    in_python = aggregate_per_device(
        _load_readings_range(START, ENDE), bucket_seconds=_COMBINE_BUCKET_SECONDS
    )
    in_sql = _load_per_device_buckets(START, ENDE)

    assert set(in_sql) == set(in_python), "andere Geraete"
    for device_id, python_buckets in in_python.items():
        sql_buckets = in_sql[device_id]
        assert set(sql_buckets) == set(python_buckets), f"andere Buckets bei {device_id}"
        for bucket, python_werte in python_buckets.items():
            for feld in HISTORY_FIELDS:
                erwartet = python_werte[feld]
                bekommen = sql_buckets[bucket][feld]
                if erwartet is None:
                    assert bekommen is None, f"{device_id}/{bucket}/{feld}: {bekommen} statt None"
                else:
                    assert bekommen == pytest.approx(erwartet), (
                        f"{device_id}/{bucket}/{feld}: {bekommen} statt {erwartet}"
                    )


def test_bucket_without_any_value_for_a_field_stays_unknown(client):
    """Ein Feld, fuer das in einem Bucket kein einziger Messwert vorliegt,
    muss None bleiben - nicht 0. Sonst wuerde eine Messluecke als "null
    Watt" in die Energiebilanz eingehen, statt als unbekannt zu gelten."""
    _seed()
    in_sql = _load_per_device_buckets(START, ENDE)

    basis = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    bucket_1001 = int((basis + timedelta(minutes=1)).timestamp())
    assert in_sql["wr1"][bucket_1001]["pv_power_w"] is None
    assert in_sql["wr1"][bucket_1001]["ac_power_w"] is None
    # Gegenprobe: im selben Bucket vorhandene Felder sind gesetzt.
    assert in_sql["wr1"][bucket_1001]["home_power_w"] == pytest.approx(1500.0)


def test_padding_widens_the_loaded_window(client):
    """Das padding-Argument muss wie bei _load_readings_range zusaetzliche
    Messwerte vor/nach dem Zeitraum einbeziehen - build_battery_energy_summary
    braucht das fuer Messpaare ueber Mitternacht."""
    _seed()
    # Der Messwert am 2. Juni liegt ausserhalb von [1.6., 2.6.).
    ohne = _load_per_device_buckets(START, date(2026, 6, 2))
    mit = _load_per_device_buckets(START, date(2026, 6, 2), padding=timedelta(days=1))
    assert "wr2" in ohne
    assert sum(len(b) for b in mit.values()) > sum(len(b) for b in ohne.values())


def test_combined_rows_are_identical_to_the_previous_python_path(client, monkeypatch):
    """Der eigentliche Zweck der Umstellung: bei MEHREREN Wechselrichtern
    laedt _load_rows_for_range() die bereits verdichteten Buckets aus der
    Datenbank, statt saemtliche Rohmesswerte zu holen und in Python zu
    mitteln. Herauskommen muss exakt dieselbe kombinierte Zeitreihe wie
    auf dem alten Weg.

    Das ist der Pfad, der bei einer Anlage mit zwei Wechselrichtern im
    Alltag tatsaechlich laeuft - deshalb hier ausdruecklich gegen die
    vorherige Implementierung gestellt und nicht nur gegen erwartete
    Zahlen."""
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _seed()

    neu = _load_rows_for_range(START, ENDE)
    alt = _combined_rows(_load_readings_range(START, ENDE))

    def als_dict(rows):
        return {
            row.timestamp: {feld: getattr(row, feld) for feld in HISTORY_FIELDS}
            for row in rows
        }

    neu_map, alt_map = als_dict(neu), als_dict(alt)
    assert set(neu_map) == set(alt_map), "andere Zeitpunkte"
    assert neu_map, "Testdaten ergaben gar keine kombinierten Zeilen"
    for zeitpunkt, alte_werte in alt_map.items():
        for feld, erwartet in alte_werte.items():
            bekommen = neu_map[zeitpunkt][feld]
            if erwartet is None:
                assert bekommen is None, f"{zeitpunkt}/{feld}: {bekommen} statt None"
            else:
                assert bekommen == pytest.approx(erwartet), (
                    f"{zeitpunkt}/{feld}: {bekommen} statt {erwartet}"
                )


def test_single_inverter_still_gets_raw_readings(client, monkeypatch):
    """Bei genau einem Wechselrichter gibt es nichts zu kombinieren - dann
    muessen weiterhin die unveraenderten Rohmesswerte herauskommen, nicht
    auf Minuten gemittelte Buckets. Sonst verloere z.B. die Trapez-
    Integration ihre Stuetzstellen."""
    monkeypatch.setattr(app_settings, "inverters", [WR1])
    _seed()

    rows = _load_rows_for_range(START, ENDE)
    roh = _load_readings_range(START, ENDE)
    assert [r.timestamp for r in rows] == [r.timestamp for r in roh]
    assert {r.device_id for r in rows} == {r.device_id for r in roh}
