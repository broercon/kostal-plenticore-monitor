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

import pytest

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


def _insert_two_device_readings(now):
    """Legt fuer WR1 (Batterie, echter Zaehler) und WR2 (kein Zaehler, laedt
    WR1s Batterie per AC mit) je zwei Messwerte an - dieselben Werte wie in
    den anderen Tests dieser Datei (WR1s eigener Home_P ist kaputt/negativ,
    WR2 erfindet Grid-Werte). Fuer History/Tagesverbrauch-Tests, die pruefen,
    dass bei ausgewaehltem Einzelgeraet trotzdem die hausweite, korrigierte
    Energiebilanz verwendet wird."""
    from app.database import SessionLocal
    from app.models import Reading

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


def test_get_history_uses_house_wide_home_for_single_device_selection(client, monkeypatch):
    """Kernstueck des Fixes: bei ausgewaehltem Einzelgeraet (z.B. "Dach Sued"
    mit Batterie) muss das Hauptdiagramm trotzdem den korrigierten,
    hausweiten Hausverbrauch zeigen (nicht WR1s eigenen, kaputten/negativen
    Home_P) - PV-Leistung bleibt dagegen die von WR1 selbst."""
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _login(client)
    now = datetime.now(timezone.utc)
    _insert_two_device_readings(now)

    res = client.get("/api/readings/history?device_id=wr1&hours=1&bucket_minutes=60")
    assert res.status_code == 200
    points = res.json()
    assert len(points) >= 1
    point = points[-1]

    # Nicht mehr WR1s kaputter Rohwert (-1371.8), sondern die hausweite
    # Energiebilanz ueber beide Geraete.
    expected_home = (1332.9 + 1300.0) + 40.7 - 0.0 + (-2597.5)
    assert point["home_power_w"] == pytest.approx(expected_home)
    assert point["grid_draw_power_w"] == pytest.approx(40.7)
    # PV bleibt WR1s eigene Erzeugung, nicht die Summe beider Geraete.
    assert point["pv_power_w"] == pytest.approx(1332.9)


def test_get_daily_totals_home_metric_ignores_device_id_when_multi(client, monkeypatch):
    """Fuer die hausweite Metrik "home" muss /daily-totals denselben Wert
    liefern, egal ob device_id=wr1 mitgegeben wird oder nicht - Hausverbrauch
    laesst sich nicht sinnvoll einem einzelnen Wechselrichter zuordnen."""
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _login(client)
    now = datetime.now(timezone.utc)
    _insert_two_device_readings(now)

    res_all = client.get("/api/readings/daily-totals?metric=home&days=1")
    res_wr1 = client.get("/api/readings/daily-totals?metric=home&device_id=wr1&days=1")
    assert res_all.status_code == res_wr1.status_code == 200
    assert res_all.json()["days"] == res_wr1.json()["days"]

    # Fuer "pv" bleibt device_id dagegen wirksam (WR1 liefert weniger als die
    # Summe beider Geraete).
    res_pv_all = client.get("/api/readings/daily-totals?metric=pv&days=1")
    res_pv_wr1 = client.get("/api/readings/daily-totals?metric=pv&device_id=wr1&days=1")
    kwh_all = res_pv_all.json()["days"][0]["kwh"]
    kwh_wr1 = res_pv_wr1.json()["days"][0]["kwh"]
    assert kwh_all is not None and kwh_wr1 is not None
    assert kwh_wr1 < kwh_all


def test_get_daily_home_breakdown_sums_to_total_home_consumption(client, monkeypatch):
    """Die drei Anteile (PV/Speicher/Netz) muessen sich zum bekannten,
    korrigierten Gesamt-Hausverbrauch aufsummieren (fuer den gestapelten
    Balken im Tagesverbrauch-Diagramm)."""
    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])
    _login(client)
    now = datetime.now(timezone.utc)
    _insert_two_device_readings(now)

    res_breakdown = client.get("/api/readings/daily-home-breakdown?days=1")
    res_total = client.get("/api/readings/daily-totals?metric=home&days=1")
    assert res_breakdown.status_code == res_total.status_code == 200

    day = res_breakdown.json()["days"][0]
    total_kwh = res_total.json()["days"][0]["kwh"]
    assert total_kwh is not None
    assert day["pv_kwh"] is not None
    assert day["battery_kwh"] is not None
    assert day["grid_kwh"] is not None
    assert (day["pv_kwh"] + day["battery_kwh"] + day["grid_kwh"]) == pytest.approx(
        total_kwh, abs=0.01
    )


def test_daily_summaries_combined_yield_equals_sum_of_devices(client, monkeypatch):
    """Regression: der PV-Gesamtwert ("_all_") muss exakt die Summe der je
    Wechselrichter angezeigten Tageswerte sein - nicht eine separat
    integrierte, leicht abweichende Zahl (Mail-Report / Dashboard)."""
    from app.daily_summary import build_daily_summaries
    from app.database import SessionLocal
    from app.models import Reading
    from app.poller import poller as poller_singleton

    monkeypatch.setattr(app_settings, "inverters", [WR1, WR2])

    now = datetime.now(timezone.utc)
    # Geraeteeigene Tagesstatistik (wie vom Poller geliefert): die exakt so
    # in der Geraetetabelle angezeigt werden.
    poller_singleton.latest = {
        "wr1": {"device_id": "wr1", "device_name": WR1.name, "timestamp": now,
                "yield_day_kwh": 95.64, "home_consumption_day_kwh": 8.0,
                "energy_grid_day_kwh": 1.0},
        "wr2": {"device_id": "wr2", "device_name": WR2.name, "timestamp": now,
                "yield_day_kwh": 53.08, "home_consumption_day_kwh": None,
                "energy_grid_day_kwh": None},
    }
    db = SessionLocal()
    try:
        db.add_all(
            Reading(device_id=d, device_name=d, timestamp=now - timedelta(minutes=m),
                    pv_power_w=1000.0, home_power_w=200.0, grid_draw_power_w=0.0,
                    feed_in_power_w=800.0)
            for d in ("wr1", "wr2") for m in (0, 10)
        )
        db.commit()
    finally:
        db.close()

    try:
        summaries = build_daily_summaries()
    finally:
        poller_singleton.latest = {}

    by_id = {s.device_id: s for s in summaries}
    assert {"wr1", "wr2", "_all_"} <= set(by_id)
    device_sum = by_id["wr1"].yield_day_kwh + by_id["wr2"].yield_day_kwh
    assert by_id["_all_"].yield_day_kwh == round(device_sum, 3)
    assert by_id["_all_"].yield_day_kwh == 148.72
