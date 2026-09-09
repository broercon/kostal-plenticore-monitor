"""Regressionstest fuer die Core-Select-Optimierung in main.py/daily_summary.py:
select(Reading.__table__) statt select(Reading) fuer Bulk-Zeitraum-Anfragen.

Hintergrund (siehe docs/CALCULATIONS.md "Performance: Core-Select statt
ORM-Objekte"): fuer grosse Zeitraeume (mehrere Wochen bis Jahre Rohmesswerte)
war die ORM-Objekterzeugung von select(Reading) mit Abstand der teuerste
Teil dieser Anfragen (gemessen: >20s bei ~1 Mio. Zeilen fuer 90 Tage/2
Wechselrichter, ~1s als reiner SQL-Scan). select(Reading.__table__) liefert
stattdessen SQLAlchemy-Core-Row-Objekte - dieser Test stellt sicher, dass sie
sich fuer die nachgelagerten Aggregationsfunktionen (die nur per getattr()
zugreifen) IDENTISCH verhalten wie volle ORM-Objekte, damit ein kuenftiger
Wechsel zurueck (oder eine neue Bulk-Anfrage nach demselben, falschen Muster)
nicht unbemerkt bleibt."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.aggregation import aggregate_per_device, day_profile, integrate_kwh
from app.database import SessionLocal
from app.models import Reading


def _seed(n: int = 20) -> None:
    base = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)
    rows = [
        Reading(
            device_id="wr1",
            device_name="WR1",
            timestamp=base + timedelta(minutes=15 * i),
            home_power_w=500.0 + i,
            pv_power_w=1000.0 + i * 10,
            battery_power_w=-50.0 if i % 2 == 0 else 50.0,
            grid_draw_power_w=0.0,
            feed_in_power_w=max(0.0, 500.0 - i),
        )
        for i in range(n)
    ]
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def _load_orm(since):
    db = SessionLocal()
    try:
        return list(db.scalars(select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)))
    finally:
        db.close()


def _load_core(since):
    db = SessionLocal()
    try:
        return db.execute(
            select(Reading.__table__).where(Reading.timestamp >= since).order_by(Reading.timestamp)
        ).all()
    finally:
        db.close()


def test_core_table_select_matches_orm_for_aggregation(client):
    """client-Fixture nur wegen der frischen Test-DB, keine HTTP-Aufrufe noetig."""
    _seed()
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)

    orm_rows = _load_orm(since)
    core_rows = _load_core(since)

    assert len(orm_rows) == len(core_rows) == 20

    # Attributzugriff (getattr) liefert dieselben Werte - das ist alles, was
    # die Aggregationsfunktionen von einer "Reading-aehnlichen" Zeile brauchen.
    for o, c in zip(orm_rows, core_rows):
        assert o.device_id == c.device_id
        assert o.timestamp == c.timestamp
        assert o.pv_power_w == c.pv_power_w
        assert o.battery_power_w == c.battery_power_w
        assert isinstance(c.timestamp, datetime)  # kein roher String

    # Und die tatsaechlich genutzten Funktionen liefern bit-identische
    # Ergebnisse, unabhaengig davon, welche der beiden Ladevarianten sie
    # bekommen.
    assert integrate_kwh(orm_rows, "pv_power_w") == integrate_kwh(core_rows, "pv_power_w")
    assert aggregate_per_device(orm_rows, 300) == aggregate_per_device(core_rows, 300)
    assert day_profile(orm_rows, 15, "Europe/Berlin") == day_profile(core_rows, 15, "Europe/Berlin")

    # Attribut-gefiltertes Herausgreifen eines Geraets (wie in
    # main.get_day_profile fuer die Einzelgeraet-PV-Kurve) funktioniert
    # ebenso mit Core-Row-Objekten.
    filtered = [r for r in core_rows if r.device_id == "wr1"]
    assert len(filtered) == 20
