"""Speicherbilanz je Zeitraum: geladene und entnommene Energie.

Deckt zwei Dinge ab, die bei der Batterie leicht schiefgehen:
1. Laden und Entladen muessen JE MESSPUNKT getrennt werden, nicht erst nach
   der Integration - sonst kuerzen sie sich ueber den Tag weg.
2. Die konfigurierbare Vorzeichen-Konvention (battery_power_inverted) muss
   auch hier greifen, wie in combine_devices.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.aggregation import (
    BATTERY_CHARGE,
    BATTERY_DISCHARGE,
    daily_battery_energy_totals,
    battery_flow_power_w,
    daily_battery_energy_flows,
    aggregate_per_device,
    combine_devices,
    daily_home_source_breakdown_kwh,
    day_profile,
)
from app.config import settings as app_settings
from app.models import Reading

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")
# Konstante Leistung; zwei Punkte 15 min auseinander -> Trapezregel ergibt
# 4000 W * 0.25 h = 1000 Wh = 1.0 kWh (wie in test_feed_in_summary.py).
POWER_W = 4000.0
KWH_PER_PAIR = 1.0


def _row(ts: datetime, battery_w: float, device_id: str = "wr1") -> Reading:
    return Reading(
        device_id=device_id,
        device_name=device_id,
        timestamp=ts,
        battery_power_w=battery_w,
    )


def _pair(local_hour: int, battery_w: float, device_id: str = "wr1") -> list[Reading]:
    """Zwei Messpunkte 15 min auseinander mit konstanter Batterieleistung."""
    base = datetime(2026, 6, 15, local_hour, 0, tzinfo=TZ)
    return [
        _row(base.astimezone(timezone.utc), battery_w, device_id),
        _row((base + timedelta(minutes=15)).astimezone(timezone.utc), battery_w, device_id),
    ]


def test_battery_flow_power_splits_by_sign():
    # Negativ = Laden (siehe Vorzeichen-Konvention in day_profile).
    assert battery_flow_power_w(-1000.0, BATTERY_CHARGE) == 1000.0
    assert battery_flow_power_w(-1000.0, BATTERY_DISCHARGE) == 0.0
    # Positiv = Entladen.
    assert battery_flow_power_w(2000.0, BATTERY_DISCHARGE) == 2000.0
    assert battery_flow_power_w(2000.0, BATTERY_CHARGE) == 0.0


def test_battery_flow_power_respects_inversion():
    assert battery_flow_power_w(1000.0, BATTERY_CHARGE, inverted=True) == 1000.0
    assert battery_flow_power_w(1000.0, BATTERY_DISCHARGE, inverted=True) == 0.0


def test_charge_and_discharge_do_not_cancel_out():
    """Mittags laden, abends entladen: beide Richtungen muessen ihren eigenen
    Betrag behalten (und nicht netto ~0 ergeben)."""
    rows = _pair(12, -POWER_W) + _pair(20, POWER_W)

    charge = daily_battery_energy_totals(rows, "Europe/Berlin", BATTERY_CHARGE)
    discharge = daily_battery_energy_totals(rows, "Europe/Berlin", BATTERY_DISCHARGE)

    assert len(charge) == 1 and len(discharge) == 1
    assert charge[0]["date"] == "2026-06-15"
    assert abs(charge[0]["kwh"] - KWH_PER_PAIR) < 1e-6
    assert abs(discharge[0]["kwh"] - KWH_PER_PAIR) < 1e-6


def test_inverted_device_swaps_directions():
    rows = _pair(12, -POWER_W)
    inverted = {"wr1": True}

    charge = daily_battery_energy_totals(rows, "Europe/Berlin", BATTERY_CHARGE, inverted)
    discharge = daily_battery_energy_totals(rows, "Europe/Berlin", BATTERY_DISCHARGE, inverted)

    # Nichts geladen, da Vorzeichen gedreht - der Tag bleibt aber mit 0.0
    # enthalten (es LIEGEN Batteriewerte vor, es wurde nur nicht geladen).
    assert charge[0]["kwh"] == 0.0
    assert abs(discharge[0]["kwh"] - KWH_PER_PAIR) < 1e-6


def test_multiple_devices_are_summed():
    rows = _pair(12, -POWER_W, "wr1") + _pair(12, -POWER_W, "wr2")
    charge = daily_battery_energy_totals(rows, "Europe/Berlin", BATTERY_CHARGE)
    assert abs(charge[0]["kwh"] - 2 * KWH_PER_PAIR) < 1e-6


def test_rows_without_battery_are_ignored():
    rows = _pair(12, -POWER_W) + [
        _row(datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc), None),  # type: ignore[arg-type]
    ]
    charge = daily_battery_energy_totals(rows, "Europe/Berlin", BATTERY_CHARGE)
    # Nur der Tag mit Batteriewerten kommt vor - der andere fehlt ganz.
    assert [d["date"] for d in charge] == ["2026-06-15"]


def test_sign_change_integrates_each_side_of_zero():
    start = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
    for first, second in ((-4000, 4000), (4000, -4000)):
        rows = [_row(start, first), _row(start + timedelta(minutes=15), second)]
        result = daily_battery_energy_flows(rows, "Europe/Berlin")
        assert result == [{"date": "2026-06-15", "charge": 0.25, "discharge": 0.25}]


def test_asymmetric_sign_change_and_inversion():
    start = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
    rows = [_row(start, -3000), _row(start + timedelta(minutes=20), 1000)]
    assert daily_battery_energy_flows(rows, "Europe/Berlin") == [
        {"date": "2026-06-15", "charge": 0.375, "discharge": 0.042}
    ]
    assert daily_battery_energy_flows(rows, "Europe/Berlin", {"wr1": True}) == [
        {"date": "2026-06-15", "charge": 0.042, "discharge": 0.375}
    ]


def test_midnight_interval_is_split_between_local_days():
    start = datetime(2026, 6, 15, 23, 50, tzinfo=TZ).astimezone(timezone.utc)
    rows = [_row(start, -3000), _row(start + timedelta(minutes=20), -3000)]
    assert daily_battery_energy_flows(rows, "Europe/Berlin") == [
        {"date": "2026-06-15", "charge": 0.5, "discharge": 0.0},
        {"date": "2026-06-16", "charge": 0.5, "discharge": 0.0},
    ]


def test_missing_intervals_are_not_reported_as_measured_zero():
    start = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
    rows = [_row(start, -3000), _row(start + timedelta(hours=1), -3000)]
    assert daily_battery_energy_flows(rows, "Europe/Berlin") == []


def test_combined_inverted_battery_preserves_solar_share():
    start = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
    rows = [
        Reading(
            device_id="wr1", device_name="WR1", timestamp=start + timedelta(minutes=m),
            pv_power_w=1000.0, battery_power_w=-1000.0, ac_power_w=3000.0,
            home_power_w=3000.0, feed_in_power_w=0.0, grid_draw_power_w=0.0,
        ) for m in (0, 15)
    ]
    combined = combine_devices(
        aggregate_per_device(rows, 60), {"wr1": True, "wr2": False}, {"wr1": True},
        raw_battery_output=True,
    )
    synthetic = [
        Reading(device_id="all", device_name="all", timestamp=datetime.fromtimestamp(ts, timezone.utc), **values)
        for ts, values in combined.items()
    ]
    assert daily_home_source_breakdown_kwh(synthetic, "Europe/Berlin") == [
        {"date": "2026-06-15", "pv_kwh": 0.5, "battery_kwh": 0.25, "grid_kwh": 0.0}
    ]
    point = day_profile(synthetic, 15, "Europe/Berlin")[0]["points"][0]
    assert point["pv_power_w"] == 2000.0


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _seed_today(device_id: str) -> None:
    """Laden (1 kWh) und Entladen (2 kWh) am heutigen lokalen Kalendertag."""
    from app.database import SessionLocal

    today = datetime.now(TZ).date()
    midnight = datetime(today.year, today.month, today.day, tzinfo=TZ)
    rows: list[Reading] = []
    for minute, power in ((0, -POWER_W), (15, -POWER_W)):
        rows.append(_row((midnight + timedelta(hours=10, minutes=minute)).astimezone(timezone.utc), power, device_id))
    # Entladen doppelt so lang -> 2 kWh.
    for minute in (0, 15, 30):
        rows.append(_row((midnight + timedelta(hours=20, minutes=minute)).astimezone(timezone.utc), POWER_W, device_id))

    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


PERIOD_KEYS = {
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


def test_battery_summary_endpoint(client):
    _login(client)
    _seed_today(app_settings.inverters[0].id)

    res = client.get("/api/readings/battery-summary")
    assert res.status_code == 200
    body = res.json()

    charge = {p["key"]: p for p in body["charge_periods"]}
    discharge = {p["key"]: p for p in body["discharge_periods"]}
    assert set(charge) == PERIOD_KEYS
    assert set(discharge) == PERIOD_KEYS

    assert abs(charge["today"]["kwh"] - 1.0) < 1e-6
    assert abs(discharge["today"]["kwh"] - 2.0) < 1e-6
    # Zeitraeume, die heute enthalten, sehen dieselben Werte; gestern nicht.
    assert abs(charge["this_week"]["kwh"] - 1.0) < 1e-6
    assert charge["yesterday"]["kwh"] is None


def test_battery_summary_without_data_is_none(client):
    _login(client)
    res = client.get("/api/readings/battery-summary")
    assert res.status_code == 200
    body = res.json()
    for period in body["charge_periods"] + body["discharge_periods"]:
        assert period["kwh"] is None


def test_battery_summary_reuses_reads_and_invalidates_inverted_cache(client, frozen_now, monkeypatch):
    import app.daily_summary as module
    from app.database import SessionLocal

    start = (frozen_now - timedelta(days=1)).replace(hour=10)
    with SessionLocal() as db:
        db.add_all([_row(start, -4000), _row(start + timedelta(minutes=15), -4000)])
        db.commit()
    original = module._load_readings_range
    calls = []

    def tracked(start, end, **kwargs):
        calls.append((start, end))
        return original(start, end, **kwargs)

    monkeypatch.setattr(module, "_load_readings_range", tracked)
    monkeypatch.setattr(module, "_battery_inverted_map", lambda: {"wr1": False})
    result = module.build_battery_energy_summary()
    assert len(calls) == 2  # Ein Historienfenster und heute fuer beide Richtungen.
    assert next(p.kwh for p in result["charge_periods"] if p.key == "yesterday") == 1.0
    calls.clear()
    module.build_battery_energy_summary()
    assert len(calls) == 1  # Historie kommt aus dem Cache.
    monkeypatch.setattr(module, "_battery_inverted_map", lambda: {"wr1": True})
    result = module.build_battery_energy_summary()
    assert next(p.kwh for p in result["charge_periods"] if p.key == "yesterday") == 0.0
    assert next(p.kwh for p in result["discharge_periods"] if p.key == "yesterday") == 1.0
