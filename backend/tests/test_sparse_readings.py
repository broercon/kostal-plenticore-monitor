"""Sparse raw readings must never be mistaken for hourly averages."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.aggregation import (
    daily_battery_energy_flows,
    daily_home_source_breakdown_kwh,
    daily_kwh_totals,
    daily_pv_yield_totals,
)


def _rows(minutes):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            timestamp=base + timedelta(minutes=minute),
            device_id="wr1", pv_power_w=2000.0, battery_power_w=1000.0,
            home_power_w=2000.0, grid_draw_power_w=0.0, feed_in_power_w=0.0,
        )
        for minute in minutes
    ]


def test_sparse_daily_totals_do_not_bridge_one_hour_outage():
    rows = _rows([0, 60])
    assert daily_kwh_totals(rows, "pv_power_w", "UTC")[0]["kwh"] == 0.0
    assert daily_pv_yield_totals(rows, "UTC")[0]["kwh"] == 0.0
    breakdown = daily_home_source_breakdown_kwh(rows, "UTC")[0]
    assert all(breakdown[key] == 0.0 for key in ("pv_kwh", "battery_kwh", "grid_kwh"))


def test_sparse_battery_series_does_not_bridge_two_hour_outages():
    assert daily_battery_energy_flows(_rows(range(0, 24 * 60, 120)), "UTC") == []


def test_valid_intervals_survive_sparse_day():
    rows = _rows([0, 15, 120, 135])
    assert daily_kwh_totals(rows, "pv_power_w", "UTC")[0]["kwh"] == 1.0
    assert daily_pv_yield_totals(rows, "UTC")[0]["kwh"] == 0.5
    assert daily_battery_energy_flows(rows, "UTC") == [
        {"date": "2026-06-01", "charge": 0.0, "discharge": 0.5},
    ]
