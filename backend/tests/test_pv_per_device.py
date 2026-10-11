"""Tests fuer den PV-Ertrag je Wechselrichter: /api/readings/daily-pv-per-device
(Auswertungen-Tab, "Erzeugung je Tag") und yearly-comparison?per_device=true.

Nutzt die frozen_now-Fixture (FROZEN_NOW = 2026-06-15 12:00 UTC).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import InverterConfig
from app.config import settings as app_settings
from app.models import Reading

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")
WR1 = InverterConfig(id="wr1", name="Garage", host="192.0.2.1", password="x")
WR2 = InverterConfig(id="wr2", name="Carport", host="192.0.2.2", password="x")


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _seed_day(device_id: str, day: date, pv_w: float) -> None:
    """Zwei Messpunkte (12:00/12:15 Lokalzeit) mit konstanter PV-Leistung -
    ergibt pv_w * 0.25h / 1000 kWh."""
    from app.database import SessionLocal

    noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
    db = SessionLocal()
    try:
        db.add_all(
            Reading(
                device_id=device_id,
                device_name=device_id,
                timestamp=(noon + timedelta(minutes=m)).astimezone(ZoneInfo("UTC")),
                pv_power_w=pv_w,
                home_power_w=0.0,
                feed_in_power_w=0.0,
                grid_draw_power_w=0.0,
            )
            for m in (0, 15)
        )
        db.commit()
    finally:
        db.close()


def test_daily_pv_per_device_splits_by_inverter_and_fills_all_days(client, frozen_now, monkeypatch):
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _login(client)
    today = frozen_now.astimezone(TZ).date()
    _seed_day("wr1", today, 4000.0)  # 1.0 kWh
    _seed_day("wr2", today, 2000.0)  # 0.5 kWh
    _seed_day("wr1", today - timedelta(days=2), 8000.0)  # 2.0 kWh, wr2 ohne Daten

    res = client.get("/api/readings/daily-pv-per-device?days=3")
    assert res.status_code == 200
    body = res.json()
    assert body["devices"] == [
        {"device_id": "wr1", "device_name": "Garage"},
        {"device_id": "wr2", "device_name": "Carport"},
    ]
    assert [d["date"] for d in body["days"]] == [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    assert body["days"][0]["values"] == {"wr1": 2.0, "wr2": None}
    assert body["days"][1]["values"] == {"wr1": None, "wr2": None}
    assert body["days"][2]["values"] == {"wr1": 1.0, "wr2": 0.5}


def test_daily_pv_per_device_second_call_uses_cache(client, frozen_now, monkeypatch):
    """Abgeschlossene Tage kommen beim zweiten Aufruf aus dem Cache - eine
    nachtraeglich veraenderte Rohmessung aendert das Ergebnis nicht mehr."""
    monkeypatch.setattr(app_settings, "inverters", [WR1])
    _login(client)
    yesterday = frozen_now.astimezone(TZ).date() - timedelta(days=1)
    _seed_day("wr1", yesterday, 4000.0)
    first = client.get("/api/readings/daily-pv-per-device?days=2").json()
    _seed_day("wr1", yesterday, 4000.0)
    second = client.get("/api/readings/daily-pv-per-device?days=2").json()
    assert first["days"][0]["values"] == {"wr1": 1.0}
    assert second == first


def test_daily_pv_per_device_validates_days(client):
    _login(client)
    assert client.get("/api/readings/daily-pv-per-device?days=0").status_code == 422
    assert client.get("/api/readings/daily-pv-per-device?days=32").status_code == 422


def test_yearly_comparison_per_device(client, frozen_now, monkeypatch):
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _login(client)
    today = frozen_now.astimezone(TZ).date()  # 2026-06-15
    _seed_day("wr1", today, 4000.0)  # 1.0
    _seed_day("wr2", today, 2000.0)  # 0.5
    _seed_day("wr1", today.replace(year=today.year - 1), 8000.0)  # 2.0

    body = client.get("/api/readings/yearly-comparison?per_device=true").json()
    assert [d["device_id"] for d in body["devices"]] == ["wr1", "wr2"]
    by_year = {y["year"]: y for y in body["years"]}
    juni = 5
    assert by_year[2026]["values"][juni] == 1.5
    assert by_year[2026]["device_values"]["wr1"][juni] == 1.0
    assert by_year[2026]["device_values"]["wr2"][juni] == 0.5
    assert by_year[2025]["device_values"]["wr1"][juni] == 2.0
    assert by_year[2025]["device_values"]["wr2"][juni] is None


def test_yearly_comparison_without_per_device_has_no_device_data(client, frozen_now, monkeypatch):
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _login(client)
    _seed_day("wr1", frozen_now.astimezone(TZ).date(), 4000.0)
    body = client.get("/api/readings/yearly-comparison").json()
    assert body["devices"] == []
    assert body["years"][0]["device_values"] == {}
