"""End-to-End-Test fuer /api/readings/feed-in-summary (Einspeisung je
Zeitraum). Fuegt fuer jeden der letzten 70 Kalendertage ein Messwertpaar mit
konstanter Einspeisung ein, sodass die integrierte Tagesenergie bekannt ist
(1.0 kWh/Tag), und prueft die Summen der einzelnen Zeitraeume gegen die
gleiche Datumslogik wie im Endpunkt.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings as app_settings

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")
# Konstante Einspeiseleistung; zwei Punkte 15 min auseinander -> Trapezregel
# ergibt 4000 W * 0.25 h = 1000 Wh = 1.0 kWh pro Tag.
FEED_IN_W = 4000.0
KWH_PER_DAY = 1.0
DAYS_BACK = 70


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _seed_daily_feed_in(device_id: str) -> None:
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
                    feed_in_power_w=FEED_IN_W,
                    pv_power_w=FEED_IN_W,
                    home_power_w=0.0,
                    grid_draw_power_w=0.0,
                )
            )
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def test_feed_in_summary_periods(client):
    _login(client)
    device_id = app_settings.inverters[0].id
    _seed_daily_feed_in(device_id)

    res = client.get("/api/readings/feed-in-summary")
    assert res.status_code == 200
    periods = {p["key"]: p for p in res.json()["periods"]}

    # Alle erwarteten Zeitraeume vorhanden.
    assert set(periods) == {
        "today",
        "yesterday",
        "day_before_yesterday",
        "this_week",
        "last_week",
        "this_month",
        "last_month",
    }

    # Erwartete Tagesanzahl je Zeitraum (gleiche Datumslogik wie im Endpunkt).
    today = datetime.now(TZ).date()
    this_week_start = today - timedelta(days=today.weekday())
    last_month_end = today.replace(day=1) - timedelta(days=1)
    days_last_month = last_month_end.day  # 1..letzter Tag des Vormonats

    expected_days = {
        "today": 1,
        "yesterday": 1,
        "day_before_yesterday": 1,
        "this_week": today.weekday() + 1,  # Montag..heute inklusive
        "last_week": 7,
        "this_month": today.day,
        "last_month": days_last_month,
    }

    for key, n_days in expected_days.items():
        assert periods[key]["kwh"] is not None, key
        assert abs(periods[key]["kwh"] - n_days * KWH_PER_DAY) < 1e-6, (
            key,
            periods[key]["kwh"],
            n_days,
        )

    # Datumsgrenzen plausibel: heute-Zeitraum ist genau heute.
    assert periods["today"]["from_date"] == today.strftime("%Y-%m-%d")
    assert periods["today"]["to_date"] == today.strftime("%Y-%m-%d")
    # diese Woche beginnt am Montag.
    assert periods["this_week"]["from_date"] == this_week_start.strftime("%Y-%m-%d")


def test_feed_in_summary_period_without_data_is_none(client):
    """Ohne jegliche Messwerte liefert jeder Zeitraum kwh=None (nicht 0)."""
    _login(client)
    res = client.get("/api/readings/feed-in-summary")
    assert res.status_code == 200
    for period in res.json()["periods"]:
        assert period["kwh"] is None
