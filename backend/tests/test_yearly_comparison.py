"""Tests fuer /api/readings/yearly-comparison (app.daily_summary.
build_yearly_comparison()): PV-Ertrag je Kalendermonat oder ISO-
Kalenderwoche, gruppiert nach Jahr - fuer den Jahresvergleich im
"Verlauf"-Tab (mehrere Jahre auf einer festen Jan-Dez- bzw. KW1-53-Achse
uebereinandergelegt).

Nutzt die frozen_now-Fixture (siehe conftest.py, FROZEN_NOW =
2026-06-15 12:00 UTC), damit "heute" unabhaengig vom tatsaechlichen
Testlaufzeitpunkt eindeutig ist.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import Reading

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _seed_day(device_id: str, day: date, pv_w: float) -> None:
    """Zwei Messpunkte (12:00/12:15 Lokalzeit) mit konstanter PV-Leistung -
    ergibt bei der Integration pv_w * 0.5h / 1000 kWh (siehe
    test_pv_yield_summary.py)."""
    from app.database import SessionLocal

    noon_local = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
    rows = [
        Reading(
            device_id=device_id,
            device_name="Wechselrichter",
            timestamp=(noon_local + timedelta(minutes=m)).astimezone(ZoneInfo("UTC")),
            pv_power_w=pv_w,
            home_power_w=0.0,
            feed_in_power_w=0.0,
            grid_draw_power_w=0.0,
        )
        for m in (0, 15)
    ]
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def test_yearly_comparison_without_data_is_empty(client):
    _login(client)
    res = client.get("/api/readings/yearly-comparison")
    assert res.status_code == 200
    body = res.json()
    assert body["granularity"] == "month"
    assert body["years"] == []


def test_yearly_comparison_month_groups_by_calendar_month_and_year(client, frozen_now):
    """Zwei Tage im Juni, ein Jahr auseinander - muessen als zwei separate
    Jahres-Eintraege auf Position 5 (Juni = Index 5, 0-basiert) erscheinen,
    alle anderen 11 Positionen bleiben None."""
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()  # 2026-06-15
    last_year_day = today_local.replace(year=today_local.year - 1)  # 2025-06-15

    _seed_day("wr1", today_local, pv_w=4000.0)  # 4000W * 0.25h = 1.0 kWh
    _seed_day("wr1", last_year_day, pv_w=2000.0)  # 0.5 kWh

    res = client.get("/api/readings/yearly-comparison")
    assert res.status_code == 200
    body = res.json()
    assert body["granularity"] == "month"
    assert body["labels"] == [
        "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
        "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
    ]
    assert [y["year"] for y in body["years"]] == [2025, 2026]

    last_year_entry, this_year_entry = body["years"]
    assert last_year_entry["values"][5] == 0.5
    assert this_year_entry["values"][5] == 1.0
    # Alle anderen 11 Positionen ohne Daten -> None, nicht 0 oder fehlend.
    assert [v for i, v in enumerate(last_year_entry["values"]) if i != 5] == [None] * 11
    assert len(this_year_entry["values"]) == 12


def test_yearly_comparison_week_uses_iso_calendar_not_calendar_year(client, frozen_now):
    """Regression: 2024-12-30 gehoert laut Kalenderjahr zu 2024, aber laut
    ISO 8601 zu Kalenderwoche 1 des Jahres 2025 (siehe date.isocalendar()).
    Eine naive Gruppierung nach dem Kalenderjahr des Tages wuerde diesen Tag
    faelschlich unter "2024" statt "2025" einsortieren."""
    _login(client)
    boundary_day = date(2024, 12, 30)
    assert boundary_day.isocalendar()[:2] == (2025, 1)
    _seed_day("wr1", boundary_day, pv_w=4000.0)  # 1.0 kWh

    res = client.get("/api/readings/yearly-comparison?granularity=week")
    assert res.status_code == 200
    body = res.json()
    assert body["granularity"] == "week"
    assert len(body["labels"]) == 53
    assert body["labels"][0] == "KW 1"

    assert [y["year"] for y in body["years"]] == [2025]
    year_entry = body["years"][0]
    assert year_entry["values"][0] == 1.0
    assert all(v is None for v in year_entry["values"][1:])


def test_yearly_comparison_years_param_limits_to_most_recent(client, frozen_now):
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    for offset_years in range(3):  # 2026, 2025, 2024
        day = today_local.replace(year=today_local.year - offset_years)
        _seed_day("wr1", day, pv_w=2000.0)

    res_all = client.get("/api/readings/yearly-comparison")
    assert [y["year"] for y in res_all.json()["years"]] == [2024, 2025, 2026]

    res_limited = client.get("/api/readings/yearly-comparison?years=2")
    assert res_limited.status_code == 200
    assert [y["year"] for y in res_limited.json()["years"]] == [2025, 2026]


def test_yearly_comparison_years_param_above_five_is_rejected(client):
    _login(client)
    res = client.get("/api/readings/yearly-comparison?years=6")
    assert res.status_code == 422


def test_yearly_comparison_invalid_granularity_is_rejected(client):
    _login(client)
    res = client.get("/api/readings/yearly-comparison?granularity=day")
    assert res.status_code == 422


def test_yearly_comparison_shares_cache_with_pv_yield_summary(client, frozen_now):
    """Beide Endpunkte nutzen denselben Cache-Feldnamen ("pv_yield", siehe
    daily_summary._compute_pv_yield_days) - ein bereits ueber
    /api/readings/pv-yield-summary aufgewaermter Cache-Eintrag fuer einen
    abgeschlossenen Tag darf nicht doppelt berechnet werden."""
    from app.database import SessionLocal
    from app.models import DailyEnergyCache

    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    last_year_day = today_local.replace(year=today_local.year - 1)
    _seed_day("wr1", last_year_day, pv_w=2000.0)

    # Cache zuerst ueber den bestehenden Endpunkt aufwaermen.
    assert client.get("/api/readings/pv-yield-summary").status_code == 200

    db = SessionLocal()
    try:
        cached_fields = {
            row.field
            for row in db.query(DailyEnergyCache)
            .filter(DailyEnergyCache.date == last_year_day.strftime("%Y-%m-%d"))
            .all()
        }
    finally:
        db.close()
    assert cached_fields == {"pv_yield"}

    res = client.get("/api/readings/yearly-comparison")
    assert res.status_code == 200
    assert res.json()["years"][0]["values"][last_year_day.month - 1] == 0.5
