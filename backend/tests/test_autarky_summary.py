"""Tests fuer den Autarkiegrad: taeglicher Wert in
/api/readings/daily-home-breakdown sowie die monatliche Uebersicht
/api/readings/autarky-monthly (app.daily_summary.build_autarky_monthly_summary).

Nutzt die frozen_now-Fixture (siehe conftest.py, FROZEN_NOW = 2026-06-15
12:00 UTC = 14:00 Europe/Berlin), damit "heute"/"dieser Monat"/"letzter
Monat" unabhaengig vom tatsaechlichen Testlaufzeitpunkt eindeutig sind.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import Reading

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _add(rows: list[Reading]) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def _seed_day(device_id: str, day, home_w: float, pv_w: float, grid_draw_w: float) -> None:
    """Zwei Messpunkte (12:00/12:15 Lokalzeit) mit konstanter Leistung, wie
    in test_pv_yield_summary.py - ergibt bei der Integration jeweils
    power_w * 0.25h / 1000 kWh fuer den Anteil, der laut Energiebilanz aus
    PV/Netz/Batterie kommt (siehe aggregation.daily_home_source_breakdown_kwh)."""
    noon_local = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
    rows = [
        Reading(
            device_id=device_id,
            device_name="Wechselrichter",
            timestamp=(noon_local + timedelta(minutes=m)).astimezone(ZoneInfo("UTC")),
            home_power_w=home_w,
            pv_power_w=pv_w,
            grid_draw_power_w=grid_draw_w,
            feed_in_power_w=0.0,
        )
        for m in (0, 15)
    ]
    _add(rows)


def test_daily_home_breakdown_includes_autarky_percent(client, frozen_now):
    """home=1000W, grid_draw=400W, pv=600W -> 600W aus PV, 400W aus Netz,
    Autarkiegrad = 600/1000 = 60%."""
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    _seed_day("wr1", today_local, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)

    res = client.get("/api/readings/daily-home-breakdown?days=1")
    assert res.status_code == 200
    days = res.json()["days"]
    assert len(days) == 1
    assert days[0]["pv_kwh"] == 0.15
    assert days[0]["grid_kwh"] == 0.1
    assert days[0]["autarky_percent"] == 60.0


def test_autarky_monthly_summary_two_months(client, frozen_now):
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    last_month_day = today_local.replace(day=1) - timedelta(days=1)  # letzter Tag Vormonat

    # Heute: 60 % Autarkiegrad (siehe test oben).
    _seed_day("wr1", today_local, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)
    # Vormonat: home=1000W, grid_draw=800W, pv=200W -> 20 % Autarkiegrad.
    _seed_day("wr1", last_month_day, home_w=1000.0, pv_w=200.0, grid_draw_w=800.0)

    res = client.get("/api/readings/autarky-monthly")
    assert res.status_code == 200
    months = res.json()["months"]
    assert [m["month"] for m in months] == [
        last_month_day.strftime("%Y-%m"),
        today_local.strftime("%Y-%m"),
    ]

    last_month_entry, this_month_entry = months
    assert last_month_entry["autarky_percent"] == 20.0
    assert last_month_entry["pv_kwh"] == 0.05
    assert last_month_entry["grid_kwh"] == 0.2
    assert last_month_entry["home_kwh"] == 0.25

    assert this_month_entry["autarky_percent"] == 60.0
    assert this_month_entry["pv_kwh"] == 0.15
    assert this_month_entry["grid_kwh"] == 0.1
    assert this_month_entry["home_kwh"] == 0.25


def test_autarky_monthly_months_param_limits_result(client, frozen_now):
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    last_month_day = today_local.replace(day=1) - timedelta(days=1)

    _seed_day("wr1", today_local, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)
    _seed_day("wr1", last_month_day, home_w=1000.0, pv_w=200.0, grid_draw_w=800.0)

    res = client.get("/api/readings/autarky-monthly?months=1")
    assert res.status_code == 200
    months = res.json()["months"]
    assert len(months) == 1
    assert months[0]["month"] == today_local.strftime("%Y-%m")
    assert months[0]["autarky_percent"] == 60.0


def test_autarky_monthly_without_data_is_empty(client):
    _login(client)
    res = client.get("/api/readings/autarky-monthly")
    assert res.status_code == 200
    assert res.json()["months"] == []


def test_autarky_monthly_uses_daily_energy_cache(client, frozen_now):
    """Abgeschlossene Tage (Vormonat) muessen nach dem ersten Aufruf im
    daily_energy_cache liegen - wie bei build_pv_yield_summary/
    build_energy_period_summary (siehe test_energy_cache.py)."""
    from app.database import SessionLocal
    from app.models import DailyEnergyCache

    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    last_month_day = today_local.replace(day=1) - timedelta(days=1)
    _seed_day("wr1", last_month_day, home_w=1000.0, pv_w=200.0, grid_draw_w=800.0)
    _seed_day("wr1", today_local, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)

    res = client.get("/api/readings/autarky-monthly")
    assert res.status_code == 200

    db = SessionLocal()
    try:
        cached_fields = {
            row.field
            for row in db.query(DailyEnergyCache)
            .filter(DailyEnergyCache.date == last_month_day.strftime("%Y-%m-%d"))
            .all()
        }
    finally:
        db.close()
    assert cached_fields == {"home_source_pv", "home_source_battery", "home_source_grid"}

    # Der laufende Tag ("heute") darf NIE im Cache landen (siehe
    # _cached_daily_totals) - er aendert sich noch im Tagesverlauf.
    db = SessionLocal()
    try:
        today_cached = (
            db.query(DailyEnergyCache)
            .filter(DailyEnergyCache.date == today_local.strftime("%Y-%m-%d"))
            .count()
        )
    finally:
        db.close()
    assert today_cached == 0
