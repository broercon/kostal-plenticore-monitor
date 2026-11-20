"""Tests fuer die Energiebilanz je Monat (/api/readings/energy-balance,
app.daily_summary.build_energy_balance): Erzeugung, Einspeisung, Speicher
und Hausverbrauch nach Quelle, ueber die vorhandenen Tages-Caches
zusammengefasst.

Nutzt frozen_now (2026-06-15 14:00 Europe/Berlin), damit "heute" fest ist.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models import Reading

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _seed(day: date, hour: int, **values) -> None:
    """Zwei Messpunkte im Abstand von 15 Minuten mit konstanter Leistung -
    jedes Feld ergibt so power_w * 0.25 h / 1000 kWh."""
    from app.database import SessionLocal

    start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
    db = SessionLocal()
    try:
        db.add_all(
            Reading(
                device_id="wr1",
                device_name="Wechselrichter",
                timestamp=(start + timedelta(minutes=m)).astimezone(ZoneInfo("UTC")),
                **values,
            )
            for m in (0, 15)
        )
        db.commit()
    finally:
        db.close()


def _sunny_noon(day: date) -> None:
    # 1600 W PV: 1000 W ins Haus, 400 W eingespeist, 200 W in den Speicher.
    _seed(day, 12, pv_power_w=1600.0, home_power_w=1000.0, grid_draw_power_w=0.0,
          feed_in_power_w=400.0, battery_power_w=-200.0)


def _evening(day: date) -> None:
    # 1000 W Haus: 800 W aus dem Speicher, 200 W aus dem Netz.
    _seed(day, 21, pv_power_w=0.0, home_power_w=1000.0, grid_draw_power_w=200.0,
          feed_in_power_w=0.0, battery_power_w=800.0)


def test_energy_balance_without_data_is_empty(client, frozen_now):
    _login(client)
    res = client.get("/api/readings/energy-balance")
    assert res.status_code == 200
    assert res.json() == {"today": "2026-06-15", "months": []}


def test_energy_balance_sums_days_per_month(client, frozen_now):
    _login(client)
    # Zwei abgeschlossene Tage (gehen ueber den Cache) plus heute.
    for day in (date(2026, 6, 13), date(2026, 6, 14), date(2026, 6, 15)):
        _sunny_noon(day)
        _evening(day)

    body = client.get("/api/readings/energy-balance").json()
    assert body["today"] == "2026-06-15"
    assert [m["month"] for m in body["months"]] == ["2026-06"]
    june = body["months"][0]
    assert june["days"] == 3
    assert june["days_in_month"] == 30
    assert june["pv_kwh"] == pytest.approx(3 * 0.4)
    assert june["feed_in_kwh"] == pytest.approx(3 * 0.1)
    assert june["battery_charge_kwh"] == pytest.approx(3 * 0.05)
    assert june["battery_discharge_kwh"] == pytest.approx(3 * 0.2)
    assert june["home_pv_kwh"] == pytest.approx(3 * 0.25)
    assert june["home_battery_kwh"] == pytest.approx(3 * 0.2)
    assert june["home_grid_kwh"] == pytest.approx(3 * 0.05)
    assert june["autarky_home_kwh"] == pytest.approx(3 * 0.5)
    assert june["autarky_grid_kwh"] == pytest.approx(3 * 0.05)

    # Zweiter Aufruf liest die abgeschlossenen Tage aus dem Cache - gleiches Ergebnis.
    again = client.get("/api/readings/energy-balance").json()
    assert again == body


def test_energy_balance_separates_months_and_skips_gaps(client, frozen_now):
    _login(client)
    _sunny_noon(date(2026, 3, 31))
    _sunny_noon(date(2026, 6, 1))

    months = client.get("/api/readings/energy-balance").json()["months"]
    # April und Mai ohne Messwerte tauchen nicht auf.
    assert [m["month"] for m in months] == ["2026-03", "2026-06"]
    assert months[0]["days"] == 1
    assert months[0]["days_in_month"] == 31


def test_energy_balance_autarky_basis_needs_grid_measurement(client, frozen_now):
    """Importierte Altdaten ohne Netzmessung: die Aufteilung ist da (Netz
    als 0 angenommen, wie im Tagesverbrauch), die Grundlage fuer den
    Autarkiegrad aber nicht - sonst erschiene der Monat 100 % autark."""
    _login(client)
    _seed(date(2026, 5, 10), 12, pv_power_w=600.0, home_power_w=1000.0)

    may = client.get("/api/readings/energy-balance").json()["months"][0]
    assert may["month"] == "2026-05"
    assert may["home_pv_kwh"] == pytest.approx(0.15)
    assert may["home_grid_kwh"] == pytest.approx(0.0)
    assert may["autarky_home_kwh"] is None
    assert may["autarky_grid_kwh"] is None
    assert may["feed_in_kwh"] is None


def test_energy_balance_requires_login(client):
    assert client.get("/api/readings/energy-balance").status_code == 401
