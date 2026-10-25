"""Speicher-Erweiterung: Simulation, Kalibrierung, Auswertung und Endpunkte.

Die Simulation wird an kleinen, von Hand nachrechenbaren Tagen geprueft
(Wirkungsgrad 1, keine Wandlungsverluste). Die Kalibrierung bekommt
synthetische Messdaten aus einem unabhaengig implementierten "echten"
Speicher mit bekannten Kenngroessen und muss diese wiederfinden.
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import app.battery_sizing as bs
from app.battery_sizing import (
    USAGE_FULL,
    USAGE_MULTI,
    USAGE_PARTIAL,
    USAGE_UNUSED,
    Calibration,
    StorageParams,
    calibrate,
    curtailment_summary,
    evaluate,
    expected_slots,
    extra_usable_kwh,
    grid_charge_kwh_per_day,
    simulate,
    usage_category,
    variant_params,
)
from app.battery_sizing_config import _defaults, get_config, update_config
from app.database import SessionLocal
from app.models import BatterySizingDay, Reading

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")
SLOT = 300


def _ideal(capacity_kwh: float = 10.0, **overrides) -> StorageParams:
    values = dict(
        capacity_kwh=capacity_kwh, min_soc=0.0, max_charge_w=10000, max_discharge_w=10000,
        eta_charge=1.0, eta_discharge=1.0, standby_w=0.0,
    )
    values.update(overrides)
    return StorageParams(**values)


def _day(day: str, profile: list[tuple[float, float]], start_hour: int = 0) -> list[list]:
    """Schritte fuer einen Tag: profile = [(pv_w, home_w), ...] je 5 Minuten
    ab start_hour (lokal)."""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=TZ) + timedelta(hours=start_hour)
    epoch = int(start.timestamp())
    return [[epoch + i * SLOT, pv, home, None, None, None, None] for i, (pv, home) in enumerate(profile)]


def _hours(hours: float, pv: float, home: float) -> list[tuple[float, float]]:
    return [(pv, home)] * int(hours * 12)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def test_surplus_charges_and_later_deficit_discharges():
    # 3 h mit 2 kW Ueberschuss = 6 kWh in den Speicher, danach 3 h mit
    # 1 kW Bedarf = 3 kWh aus dem Speicher, kein Netzbezug.
    inputs = {"2026-06-15": _day("2026-06-15", _hours(3, 3000, 1000) + _hours(3, 0, 1000), start_hour=10)}
    result = simulate(inputs, _ideal(), start_energy_kwh=0)
    day = result.days["2026-06-15"]
    assert day.charge_kwh == pytest.approx(6.0)
    assert day.discharge_kwh == pytest.approx(3.0)
    assert day.grid_kwh == pytest.approx(0.0)
    assert day.feed_kwh == pytest.approx(0.0)
    assert result.end_energy_kwh == pytest.approx(3.0)


def test_surplus_after_demand_cannot_cover_it():
    # Erst Bedarf, dann Ueberschuss: der leere Speicher hilft nicht.
    inputs = {"2026-06-15": _day("2026-06-15", _hours(2, 0, 1000) + _hours(2, 2000, 0), start_hour=6)}
    day = simulate(inputs, _ideal(), start_energy_kwh=0).days["2026-06-15"]
    assert day.grid_kwh == pytest.approx(2.0)
    assert day.discharge_kwh == pytest.approx(0.0)
    assert day.charge_kwh == pytest.approx(4.0)


def test_full_storage_feeds_in_and_empty_storage_draws_from_grid():
    inputs = {"2026-06-15": _day("2026-06-15", _hours(6, 3000, 1000) + _hours(6, 0, 1000), start_hour=8)}
    day = simulate(inputs, _ideal(capacity_kwh=5), start_energy_kwh=0).days["2026-06-15"]
    assert day.charge_kwh == pytest.approx(5.0)
    assert day.feed_kwh == pytest.approx(12.0 - 5.0)
    assert day.discharge_kwh == pytest.approx(5.0)
    assert day.grid_kwh == pytest.approx(1.0)


def test_state_of_charge_carries_over_midnight():
    inputs = {
        "2026-06-15": _day("2026-06-15", _hours(2, 4000, 0), start_hour=12),
        "2026-06-16": _day("2026-06-16", _hours(4, 0, 1000), start_hour=0),
    }
    result = simulate(inputs, _ideal(), start_energy_kwh=0)
    assert result.days["2026-06-16"].discharge_kwh == pytest.approx(4.0)
    assert result.days["2026-06-16"].grid_kwh == pytest.approx(0.0)


def test_power_limits_and_minimum_charge():
    params = _ideal(max_charge_w=1000, max_discharge_w=500, min_soc=0.2)
    inputs = {"2026-06-15": _day("2026-06-15", _hours(2, 3000, 0) + _hours(2, 0, 2000), start_hour=10)}
    day = simulate(inputs, params, start_energy_kwh=2.0).days["2026-06-15"]
    # Laden mit hoechstens 1 kW: 2 kWh in 2 h, Rest eingespeist.
    assert day.charge_kwh == pytest.approx(2.0)
    assert day.feed_kwh == pytest.approx(4.0)
    # Entladen mit hoechstens 0,5 kW: 1 kWh in 2 h, Rest aus dem Netz.
    assert day.discharge_kwh == pytest.approx(1.0)
    assert day.grid_kwh == pytest.approx(3.0)

    # Mindestladung 20 % von 10 kWh: aus 3 kWh lassen sich nur 1 kWh entnehmen.
    params = _ideal(min_soc=0.2)
    inputs = {"2026-06-15": _day("2026-06-15", _hours(3, 0, 1000))}
    day = simulate(inputs, params, start_energy_kwh=3.0).days["2026-06-15"]
    assert day.discharge_kwh == pytest.approx(1.0)
    assert day.grid_kwh == pytest.approx(2.0)


def test_efficiencies_and_conversion_reduce_usable_energy():
    params = _ideal(eta_charge=0.9, eta_discharge=0.9)
    inputs = {"2026-06-15": _day("2026-06-15", _hours(1, 1000, 0) + _hours(2, 0, 1000), start_hour=10)}
    day = simulate(inputs, params, start_energy_kwh=0).days["2026-06-15"]
    # 1 kWh Ueberschuss -> 0,9 kWh gespeichert -> 0,81 kWh am Haus.
    assert day.discharge_kwh == pytest.approx(0.81)
    assert day.grid_kwh == pytest.approx(2.0 - 0.81)

    # Wandlungsstufe: je Schritt muss die Batterie 1,1 x Bedarf + 100 W
    # liefern (100 Wh je 5 Minuten bei 1 kW). 0,6 kWh reichen fuer genau
    # 6 Schritte; der Versatz faellt nur in diesen an: (0,6 - 0,05) / 1,1.
    params = _ideal(discharge_factor=1.1, discharge_offset_w=100)
    inputs = {"2026-06-15": _day("2026-06-15", _hours(1, 0, 1000))}
    result = simulate(inputs, params, start_energy_kwh=0.6)
    assert result.days["2026-06-15"].discharge_kwh == pytest.approx(0.5)
    assert result.days["2026-06-15"].grid_kwh == pytest.approx(0.5)


def test_gaps_are_skipped_without_changing_charge():
    inputs = {"2026-06-15": _day("2026-06-15", [(None, 500)] * 24 + _hours(1, 0, 1000))}
    result = simulate(inputs, _ideal(standby_w=50), start_energy_kwh=5.0)
    assert result.days["2026-06-15"].slots == 12
    assert result.end_energy_kwh == pytest.approx(5.0 - 1.0 - 0.05)


def test_standby_comes_from_grid_when_storage_is_at_minimum():
    inputs = {"2026-06-15": _day("2026-06-15", _hours(1, 0, 0))}
    day = simulate(inputs, _ideal(standby_w=24), start_energy_kwh=0).days["2026-06-15"]
    assert day.grid_kwh == pytest.approx(0.024)


def test_daylight_saving_days_have_fewer_or_more_slots():
    assert expected_slots(date(2026, 6, 15), "Europe/Berlin") == 288
    assert expected_slots(date(2026, 3, 29), "Europe/Berlin") == 276
    assert expected_slots(date(2026, 10, 25), "Europe/Berlin") == 300


# ---------------------------------------------------------------------------
# Varianten und Nutzungshaeufigkeit
# ---------------------------------------------------------------------------

def _calibration(params: StorageParams, inverter_limit_w=None) -> Calibration:
    return Calibration(params=params, nominal_kwh=11.04, module_kwh=2.76, base_modules=4,
                       inverter_limit_w=inverter_limit_w, sources={})


def test_variants_add_module_capacity_and_scale_power():
    cal = _calibration(_ideal(capacity_kwh=10.5, max_charge_w=4000, max_discharge_w=4000))
    plus2 = variant_params(cal, 2)
    assert plus2.capacity_kwh == pytest.approx(10.5 + 5.52)
    assert plus2.max_charge_w == pytest.approx(6000)

    limited = variant_params(_calibration(cal.params, inverter_limit_w=5000), 2)
    assert limited.max_charge_w == pytest.approx(5000)
    # Eine Wechselrichtergrenze unter der gemessenen Leistung senkt den
    # vorhandenen Speicher nicht ab.
    assert variant_params(_calibration(cal.params, inverter_limit_w=3000), 1).max_charge_w == pytest.approx(4000)


def test_extra_usable_capacity_respects_minimum_and_losses():
    cal = _calibration(_ideal(min_soc=0.1, eta_discharge=0.95))
    assert extra_usable_kwh(cal, 1) == pytest.approx(2.76 * 0.9 * 0.95)
    assert extra_usable_kwh(cal, 2) == pytest.approx(5.52 * 0.9 * 0.95)


@pytest.mark.parametrize(
    ("ratio", "category"),
    [(0.0, USAGE_UNUSED), (0.049, USAGE_UNUSED), (0.05, USAGE_PARTIAL), (0.94, USAGE_PARTIAL),
     (0.95, USAGE_FULL), (1.0, USAGE_FULL), (1.01, USAGE_MULTI), (2.5, USAGE_MULTI)],
)
def test_usage_category_thresholds(ratio, category):
    assert usage_category(ratio) == category


def test_evaluate_counts_avoided_grid_and_usage_per_day():
    # Basis 10 kWh (ideal). Tag 1: 16 kWh Ueberschuss, danach 16 kWh Bedarf.
    # +1 Modul (12,76 kWh) vermeidet 2,76 kWh, +2 (15,52 kWh) 5,52 kWh -
    # beide Zusatzmodule werden also vollstaendig genutzt. Tag 2 ohne Bedarf.
    params = _ideal(capacity_kwh=10.0)
    cal = _calibration(params)
    inputs = {
        "2026-06-15": [s for s in _day("2026-06-15", _hours(4, 4000, 0) + _hours(8, 0, 2000), start_hour=8)],
        "2026-06-16": _day("2026-06-16", _hours(2, 1000, 0), start_hour=10),
    }
    result = evaluate(inputs, cal, "Europe/Berlin")
    day1 = result["daily"][0]
    assert day1["avoided_kwh"] == pytest.approx([2.76, 5.52], abs=1e-3)
    assert day1["usage"] == [USAGE_FULL, USAGE_FULL]
    assert result["daily"][1]["usage"] == [USAGE_UNUSED, USAGE_UNUSED]
    assert result["total"]["avoided_kwh"] == pytest.approx([2.76, 5.52], abs=0.01)
    assert result["months"][0]["month"] == "2026-06"
    assert result["months"][0]["usage"][0] == {"unused": 1, "partial": 0, "full": 1, "multi": 0}
    # Nur zwei Teil-Tage: nicht vollstaendig.
    assert result["total"]["complete_days"] == 0


def test_two_cycles_per_day_count_as_multi():
    # Zweimal am Tag laden und entladen: die Zusatzkapazitaet wird doppelt genutzt.
    cycle = _hours(4, 4000, 0) + _hours(8, 0, 2000)
    inputs = {"2026-06-15": _day("2026-06-15", cycle + cycle)}
    result = evaluate(inputs, _calibration(_ideal()), "Europe/Berlin")
    assert result["daily"][0]["usage"] == [USAGE_MULTI, USAGE_MULTI]
    assert result["daily"][0]["usage_ratio"][0] == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# Kalibrierung gegen einen synthetischen "echten" Speicher
# ---------------------------------------------------------------------------

TRUE = dict(capacity=10.0, eta=0.95, min_soc=10.0, max_charge=5000.0, max_discharge=4000.0,
            charge_factor=0.96, charge_offset=-100.0, discharge_factor=1.03, discharge_offset=70.0)


def _synthetic_inputs(days: int = 40, seed: int = 7) -> dict[str, list[list]]:
    """Messdaten eines Speichers mit den Kenngroessen aus TRUE - bewusst
    unabhaengig von battery_sizing.simulate implementiert. Ladestand wie beim
    echten Geraet auf ganze Prozent gerundet."""
    rng = random.Random(seed)
    cap_wh = TRUE["capacity"] * 1000
    min_wh = cap_wh * TRUE["min_soc"] / 100
    energy = cap_wh * 0.5
    dt = SLOT / 3600
    result = {}
    for d in range(days):
        day = date(2026, 5, 1) + timedelta(days=d)
        peak = rng.choice([800, 1500, 3000, 6000, 9000])
        start = int(datetime.combine(day, datetime.min.time(), tzinfo=TZ).timestamp())
        slots = []
        for i in range(288):
            hour = i / 12
            pv = max(0.0, peak * math.sin(math.pi * (hour - 6) / 14)) if 6 <= hour <= 20 else 0.0
            home = 250 + rng.random() * 300 + (1800 if 7 <= hour < 8 or 18 <= hour < 21 else 0)
            net = pv - home
            if net > 0:
                dc = max(0.0, TRUE["charge_factor"] * net + TRUE["charge_offset"])
                dc = min(dc, TRUE["max_charge"], (cap_wh - energy) / TRUE["eta"] / dt)
                energy += dc * TRUE["eta"] * dt
                bat = -dc
            else:
                need = TRUE["discharge_factor"] * -net + TRUE["discharge_offset"]
                dc = min(need, TRUE["max_discharge"], (energy - min_wh) * TRUE["eta"] / dt)
                energy -= dc / TRUE["eta"] * dt
                bat = dc
            slots.append([start + i * SLOT, round(pv, 1), round(home, 1), None, None, round(bat, 1),
                          float(round(100 * energy / cap_wh))])
        result[day.isoformat()] = slots
    return result


def test_calibration_recovers_known_storage():
    cal = calibrate(_synthetic_inputs(), _defaults() | {"nominal_capacity_kwh": 11.04})
    p = cal.params
    assert cal.sources["capacity"] == "measured"
    assert p.capacity_kwh == pytest.approx(TRUE["capacity"], rel=0.04)
    assert p.eta_charge == pytest.approx(TRUE["eta"], abs=0.02)
    assert p.eta_discharge == pytest.approx(TRUE["eta"], abs=0.02)
    assert p.min_soc * 100 == pytest.approx(TRUE["min_soc"], abs=1.0)
    assert p.max_charge_w == pytest.approx(TRUE["max_charge"], rel=0.02)
    assert p.charge_factor == pytest.approx(TRUE["charge_factor"], abs=0.02)
    assert p.charge_offset_w == pytest.approx(TRUE["charge_offset"], abs=30)
    assert p.discharge_factor == pytest.approx(TRUE["discharge_factor"], abs=0.02)
    assert p.discharge_offset_w == pytest.approx(TRUE["discharge_offset"], abs=30)


def test_calibrated_model_reproduces_measured_state_of_charge():
    inputs = _synthetic_inputs()
    cal = calibrate(inputs, _defaults())
    result = evaluate(inputs, cal, "Europe/Berlin")
    assert result["validation"]["soc_rmse"] < 3.0
    assert abs(result["validation"]["soc_mean_error"]) < 1.5


def test_configured_values_take_precedence_over_measurement():
    config = _defaults() | {"usable_capacity_kwh": 10.0, "min_soc_percent": 20.0, "standby_w": 0.0}
    cal = calibrate(_synthetic_inputs(), config)
    assert cal.params.capacity_kwh == 10.0
    assert cal.params.min_soc == pytest.approx(0.2)
    assert cal.sources["capacity"] == cal.sources["min_soc"] == cal.sources["standby"] == "config"
    # Mit bekannter Kapazitaet werden beide Wirkungsgrade getrennt bestimmt.
    assert cal.sources["eta_charge"] == cal.sources["eta_discharge"] == "measured"
    assert cal.params.eta_charge == pytest.approx(TRUE["eta"], abs=0.03)


def test_too_little_data_falls_back_to_defaults():
    cal = calibrate({}, _defaults())
    assert cal.params.capacity_kwh == 11.04
    assert cal.params.eta_charge == bs.DEFAULT_EFFICIENCY
    assert cal.params.min_soc * 100 == bs.DEFAULT_MIN_SOC_PERCENT
    assert all(cal.sources[k] == "default" for k in ("capacity", "eta_charge", "min_soc", "max_charge"))


# ---------------------------------------------------------------------------
# Netzladung und Abregelung
# ---------------------------------------------------------------------------

def test_grid_charging_is_detected():
    slots = _day("2026-10-23", _hours(1, 500, 800))
    for s in slots:
        s[bs.I_GRID] = 1300.0  # 800 W Haus + 500 W in die Batterie, obwohl kein Ueberschuss
        s[bs.I_BAT] = -1000.0
    assert grid_charge_kwh_per_day({"2026-10-23": slots})["2026-10-23"] == pytest.approx(1.0)


def test_pv_charging_is_not_grid_charging():
    slots = _day("2026-10-23", _hours(1, 3000, 800))
    for s in slots:
        s[bs.I_GRID] = 150.0
        s[bs.I_BAT] = -2000.0
    assert grid_charge_kwh_per_day({"2026-10-23": slots})["2026-10-23"] == pytest.approx(0.0)


def test_curtailment_plateau_is_suspected():
    slots = _day("2026-06-15", _hours(4, 9000, 500))
    for i, s in enumerate(slots):
        s[bs.I_FEED] = 7000.0 if i % 2 else 3000.0 + i
    assert curtailment_summary({"2026-06-15": slots}, None)["suspected"] is True
    assert curtailment_summary({"2026-06-15": slots}, 10000.0)["suspected"] is False

    smooth = _day("2026-06-15", _hours(4, 9000, 500))
    for i, s in enumerate(smooth):
        s[bs.I_FEED] = 100.0 + 100 * i
    assert curtailment_summary({"2026-06-15": smooth}, None)["suspected"] is False


# ---------------------------------------------------------------------------
# Endpunkte, Cache und Einstellungen
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def frozen_sizing_now(monkeypatch):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(bs, "datetime", _Frozen)
    bs.clear_memo()
    yield NOW
    bs.clear_memo()


def _insert_days(first: date, days: int) -> None:
    """Ein Wechselrichter (wr1) mit Speicher: tagsueber Ueberschuss, abends
    Bedarf, der den Speicher leert - mit Messwerten alle 5 Minuten."""
    session = SessionLocal()
    try:
        for d in range(days):
            day = first + timedelta(days=d)
            start = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
            soc = 50.0
            for i in range(288):
                hour = i / 12
                pv = 4000.0 if 9 <= hour < 15 else 0.0
                home = 1500.0 if 17 <= hour < 23 else 300.0
                bat = -min(3000.0, pv - home) if pv > home else (home if soc > 10 else 0.0)
                soc = max(10.0, min(100.0, soc - bat * (SLOT / 3600) / 110))
                session.add(Reading(
                    device_id="wr1", device_name="WR", timestamp=(start + timedelta(seconds=i * SLOT)).astimezone(timezone.utc),
                    pv_power_w=pv, home_power_w=home, battery_power_w=bat, battery_soc_percent=round(soc),
                    grid_draw_power_w=max(0.0, home - pv - max(0.0, bat)), feed_in_power_w=max(0.0, pv - home + min(0.0, bat)),
                ))
        session.commit()
    finally:
        session.close()


def _login(client, role: str = "betreiber") -> None:
    make_user(f"sizing-{role}", "sizing-password", role=role)
    response = client.post("/api/auth/login", json={"username": f"sizing-{role}", "password": "sizing-password"})
    assert response.status_code == 200


def test_endpoint_returns_evaluation_and_caches_closed_days(client, frozen_sizing_now):
    _insert_days(date(2026, 6, 15), 4)  # 15.-18.6., der 18. ist "heute"
    _login(client)

    response = client.get("/api/readings/battery-sizing")
    assert response.status_code == 200
    body = response.json()
    assert [v["nominal_kwh"] for v in body["variants"]] == [11.04, 13.8, 16.56]
    assert body["coverage"]["first_day"] == "2026-06-15"
    assert body["coverage"]["last_day"] == "2026-06-17"
    assert body["coverage"]["days"] == 3 and body["coverage"]["complete_days"] == 3
    assert body["coverage"]["full_year"] is False
    assert "2026-05" in body["coverage"]["missing_months"]
    assert [m["month"] for m in body["months"]] == ["2026-06"]
    assert len(body["daily"]) == 3
    assert body["validation"]["soc_points"] > 0
    assert body["financial"] is None
    assert any("Mindestladung" in a for a in body["assumptions"])
    # Abends wird mehr gebraucht als gespeichert: ein groesserer Speicher hilft.
    assert body["total"]["avoided_kwh"][0] >= 0

    session = SessionLocal()
    try:
        cached = {row.date: row for row in session.query(BatterySizingDay)}
    finally:
        session.close()
    assert sorted(cached) == ["2026-06-15", "2026-06-16", "2026-06-17"]
    assert len(json.loads(cached["2026-06-16"].payload)) == 288


def test_invalidation_drops_cached_days(client, frozen_sizing_now):
    _insert_days(date(2026, 6, 15), 3)
    bs.build_battery_sizing()
    bs.invalidate_battery_sizing_cache(date(2026, 6, 16), date(2026, 6, 16))
    session = SessionLocal()
    try:
        remaining = sorted(row.date for row in session.query(BatterySizingDay))
    finally:
        session.close()
    assert remaining == ["2026-06-15", "2026-06-17"]


def test_endpoint_without_data(client, frozen_sizing_now):
    _login(client)
    body = client.get("/api/readings/battery-sizing").json()
    assert body["coverage"]["days"] == 0
    assert body["months"] == [] and body["validation"] is None


def test_endpoint_requires_login(client):
    assert client.get("/api/readings/battery-sizing").status_code == 401


def test_admin_config_roundtrip_and_permissions(client, frozen_sizing_now):
    _login(client, role="admin")
    defaults = client.get("/api/admin/battery-sizing/config").json()
    assert defaults["nominal_capacity_kwh"] == 11.04 and defaults["source"] == "default"

    payload = {"nominal_capacity_kwh": 11.04, "module_capacity_kwh": 2.76, "min_soc_percent": 15,
               "price_ct_per_kwh": 32, "feed_in_ct_per_kwh": 8, "cost_plus_one_eur": 1500}
    saved = client.put("/api/admin/battery-sizing/config", json=payload).json()
    assert saved["min_soc_percent"] == 15 and saved["source"] == "database"
    assert get_config()["cost_plus_one_eur"] == 1500

    _insert_days(date(2026, 6, 15), 3)
    body = client.get("/api/readings/battery-sizing").json()
    assert body["calibration"]["min_soc_percent"] == 15
    assert body["calibration"]["sources"]["min_soc"] == "config"
    assert body["financial"]["variants"][0]["cost_eur"] == 1500
    # Weniger als ein Jahr Daten: keine Amortisationszeit.
    assert body["financial"]["variants"][0]["payback_years"] is None

    bad = client.put("/api/admin/battery-sizing/config", json=payload | {"module_capacity_kwh": 20})
    assert bad.status_code == 400
    assert client.put("/api/admin/battery-sizing/config", json=payload | {"eta_charge": 1.5}).status_code == 422


def test_config_requires_admin(client):
    _login(client)
    assert client.get("/api/admin/battery-sizing/config").status_code == 403
    assert client.put("/api/admin/battery-sizing/config", json={}).status_code == 403


def test_update_config_keeps_optional_fields_empty(client):
    saved = update_config({"nominal_capacity_kwh": 13.8, "module_capacity_kwh": 2.76})
    assert saved["nominal_capacity_kwh"] == 13.8
    assert saved["usable_capacity_kwh"] is None


def test_days_with_measured_grid_draw_over_threshold_are_counted():
    # Gezaehlt wird der GEMESSENE Netzbezug (> 0,5 kWh), unabhaengig davon,
    # ob ein groesserer Speicher geholfen haette. Tage ohne Netzmessung
    # zaehlen nicht.
    def day(name: str, grid_w: float | None) -> list[list]:
        slots = _day(name, _hours(1, 0, 600))
        for s in slots:
            s[bs.I_GRID] = grid_w
        return slots

    inputs = {
        "2026-06-15": day("2026-06-15", 600.0),  # 0,6 kWh
        "2026-06-16": day("2026-06-16", 400.0),  # 0,4 kWh
        "2026-06-17": day("2026-06-17", 500.0),  # genau 0,5 kWh: nicht mehr als
        "2026-07-01": day("2026-07-01", None),
    }
    result = evaluate(inputs, _calibration(_ideal()), "Europe/Berlin")
    assert result["total"]["grid_days_over_threshold"] == 1
    assert [m["grid_days_over_threshold"] for m in result["months"]] == [1, 0]
    assert result["daily"][0]["measured_grid_kwh"] == pytest.approx(0.6)
    assert result["daily"][3]["measured_grid_kwh"] is None
    assert result["grid_day_threshold_kwh"] == 0.5
