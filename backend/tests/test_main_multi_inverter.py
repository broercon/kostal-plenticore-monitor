"""End-to-End-Test (ueber die echten HTTP-Endpunkte, nicht nur die
aggregation.py-Funktionen direkt) fuer den synthetischen "_all_"-Eintrag in
/api/readings/latest und /api/readings/today-summary bei mehreren
konfigurierten Wechselrichtern.

Da app.config.settings ein beim Modul-Import einmal erzeugtes Singleton
ist, wird hier bewusst NICHT versucht, ueber Umgebungsvariablen eine zweite
Konfiguration zu laden (das wuerde einen Neustart des Prozesses/erneuten
Import erfordern) - stattdessen wird settings.inverters direkt fuer die
Dauer des Tests auf zwei Geraete umgebogen (monkeypatch), genau wie es
main.py zur Laufzeit bei jedem Request ohnehin frisch ausliest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import InverterConfig
from app.config import settings as app_settings
from app.poller import poller as poller_singleton

from .conftest import make_user

WR1 = InverterConfig(
    id="wr1", name="Dach Sued (Batterie)", host="192.0.2.1", password="x", has_grid_meter=True
)
WR2 = InverterConfig(
    id="wr2", name="Dach Nord (kein Zaehler)", host="192.0.2.2", password="x", has_grid_meter=False
)


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def test_get_latest_appends_corrected_combined_entry(client, monkeypatch):
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _login(client)

    poller_singleton.latest = {
        "wr1": {
            "device_id": "wr1",
            "device_name": WR1.name,
            "timestamp": datetime.now(timezone.utc),
            "home_power_w": -1371.8,
            "grid_power_w": 40.7,
            "feed_in_power_w": 0.0,
            "grid_draw_power_w": 40.7,
            "pv_power_w": 1332.9,
            "battery_power_w": -2597.5,
            "battery_soc_percent": 94.0,
            "yield_day_kwh": None,
            "home_consumption_day_kwh": None,
            "energy_grid_day_kwh": None,
        },
        "wr2": {
            "device_id": "wr2",
            "device_name": WR2.name,
            "timestamp": datetime.now(timezone.utc),
            "home_power_w": 500.0,
            "grid_power_w": -9999.0,
            "feed_in_power_w": 0.0,
            "grid_draw_power_w": 9999.0,
            "pv_power_w": 1300.0,
            "battery_power_w": None,
            "battery_soc_percent": None,
            "yield_day_kwh": None,
            "home_consumption_day_kwh": None,
            "energy_grid_day_kwh": None,
        },
    }
    try:
        res = client.get("/api/readings/latest")
        assert res.status_code == 200
        readings = res.json()

        by_id = {r["device_id"]: r for r in readings}
        assert {"wr1", "wr2", "_all_"} <= set(by_id)

        combined = by_id["_all_"]
        assert combined["pv_power_w"] == 1332.9 + 1300.0
        # Nur WR1 (has_grid_meter=True) darf in die Netzwerte einfliessen.
        assert combined["grid_draw_power_w"] == 40.7
        expected_home = (1332.9 + 1300.0) + 40.7 - 0.0 + (-2597.5)
        assert combined["home_power_w"] == expected_home
        assert combined["home_power_w"] > 0  # nicht mehr der kaputte WR1-Rohwert
    finally:
        poller_singleton.latest = {}


def test_get_today_summary_appends_corrected_combined_entry(client, monkeypatch):
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _login(client)

    from app.database import SessionLocal
    from app.models import Reading

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        db.add_all(
            [
                Reading(
                    device_id="wr1",
                    device_name=WR1.name,
                    timestamp=now - timedelta(minutes=10),
                    home_power_w=-1371.8,
                    grid_draw_power_w=40.7,
                    feed_in_power_w=0.0,
                    pv_power_w=1332.9,
                    battery_power_w=-2597.5,
                ),
                Reading(
                    device_id="wr1",
                    device_name=WR1.name,
                    timestamp=now,
                    home_power_w=-1371.8,
                    grid_draw_power_w=40.7,
                    feed_in_power_w=0.0,
                    pv_power_w=1332.9,
                    battery_power_w=-2597.5,
                ),
                Reading(
                    device_id="wr2",
                    device_name=WR2.name,
                    timestamp=now - timedelta(minutes=10),
                    home_power_w=500.0,
                    grid_draw_power_w=9999.0,
                    feed_in_power_w=0.0,
                    pv_power_w=1300.0,
                ),
                Reading(
                    device_id="wr2",
                    device_name=WR2.name,
                    timestamp=now,
                    home_power_w=500.0,
                    grid_draw_power_w=9999.0,
                    feed_in_power_w=0.0,
                    pv_power_w=1300.0,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    res = client.get("/api/readings/today-summary")
    assert res.status_code == 200
    summaries = res.json()
    by_id = {s["device_id"]: s for s in summaries}
    assert {"wr1", "wr2", "_all_"} <= set(by_id)

    combined = by_id["_all_"]
    # Konstante Leistung ueber 10 Minuten integriert (Trapezregel) -> home_kwh
    # muss ungefaehr expected_home_w * (10/60) Stunden entsprechen, deutlich
    # positiv statt wie bei WR1 allein negativ.
    assert combined["home_consumption_day_kwh"] is not None
    assert combined["home_consumption_day_kwh"] > 0
