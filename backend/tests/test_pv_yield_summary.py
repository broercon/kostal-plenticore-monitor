"""End-to-End-Test fuer /api/readings/pv-yield-summary (PV-Ertrag je
Zeitraum). Seedet fuer die letzten Tage ein Messwertpaar mit konstanter
PV-Leistung (4000 W, 15 min auseinander -> 1.0 kWh/Tag) und prueft die
Summen je Zeitraum. Analog zu test_feed_in_summary, aber fuer pv_power_w.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings as app_settings

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")
PV_W = 4000.0
KWH_PER_DAY = 1.0
DAYS_BACK = 40


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _seed_daily_pv(device_id: str) -> None:
    from app.database import SessionLocal
    from app.models import Reading

    today = datetime.now(TZ).date()
    rows = []
    for offset in range(DAYS_BACK + 1):
        day = today - timedelta(days=offset)
        noon_local = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
        for minute in (0, 15):
            ts = (noon_local + timedelta(minutes=minute)).astimezone(ZoneInfo("UTC"))
            rows.append(
                Reading(
                    device_id=device_id,
                    device_name="Wechselrichter",
                    timestamp=ts,
                    pv_power_w=PV_W,
                    home_power_w=0.0,
                    feed_in_power_w=0.0,
                    grid_draw_power_w=0.0,
                )
            )
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def test_pv_yield_summary_periods(client):
    _login(client)
    device_id = app_settings.inverters[0].id
    _seed_daily_pv(device_id)

    res = client.get("/api/readings/pv-yield-summary")
    assert res.status_code == 200
    periods = {p["key"]: p for p in res.json()["periods"]}

    assert set(periods) == {
        "today",
        "yesterday",
        "day_before_yesterday",
        "this_week",
        "last_week",
        "this_month",
        "last_month",
        "this_year",
        "last_year",
    }

    today = datetime.now(TZ).date()
    # Heute genau ein Tag mit 1.0 kWh PV-Ertrag.
    assert periods["today"]["kwh"] is not None
    assert abs(periods["today"]["kwh"] - KWH_PER_DAY) < 1e-6
    # Diese Woche: Montag..heute inklusive.
    assert abs(periods["this_week"]["kwh"] - (today.weekday() + 1) * KWH_PER_DAY) < 1e-6


def test_pv_yield_summary_period_without_data_is_none(client):
    _login(client)
    res = client.get("/api/readings/pv-yield-summary")
    assert res.status_code == 200
    for period in res.json()["periods"]:
        assert period["kwh"] is None
