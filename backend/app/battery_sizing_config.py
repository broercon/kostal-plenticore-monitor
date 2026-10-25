"""Persistente Rahmendaten der Speicher-Erweiterungs-Auswertung.

Alle optionalen Werte sind "leer = aus den Messdaten ableiten" (siehe
battery_sizing.calibrate); nur Nenn- und Modulkapazität haben feste
Standardwerte (BYD HVM: 4 Module à 2,76 kWh).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .database import SessionLocal
from .models import BatterySizingSettings

_SINGLETON_ID = 1

DEFAULT_NOMINAL_KWH = 11.04
DEFAULT_MODULE_KWH = 2.76

OPTIONAL_FIELDS = (
    "usable_capacity_kwh",
    "min_soc_percent",
    "max_charge_w",
    "max_discharge_w",
    "inverter_battery_limit_w",
    "eta_charge",
    "eta_discharge",
    "standby_w",
    "feed_in_limit_w",
    "cost_plus_one_eur",
    "cost_plus_two_eur",
    "price_ct_per_kwh",
    "feed_in_ct_per_kwh",
)


class InvalidBatterySizingConfig(ValueError):
    """Semantisch ungueltige Rahmendaten."""


def _defaults() -> dict:
    return {
        "nominal_capacity_kwh": DEFAULT_NOMINAL_KWH,
        "module_capacity_kwh": DEFAULT_MODULE_KWH,
        **{name: None for name in OPTIONAL_FIELDS},
        "source": "default",
    }


def get_config() -> dict:
    session = SessionLocal()
    try:
        row = session.get(BatterySizingSettings, _SINGLETON_ID)
        if row is None:
            return _defaults()
        return {
            "nominal_capacity_kwh": row.nominal_capacity_kwh,
            "module_capacity_kwh": row.module_capacity_kwh,
            **{name: getattr(row, name) for name in OPTIONAL_FIELDS},
            "source": "database",
        }
    finally:
        session.close()


def update_config(data: dict) -> dict:
    if data["module_capacity_kwh"] > data["nominal_capacity_kwh"]:
        raise InvalidBatterySizingConfig(
            "Die Modulkapazitaet kann nicht groesser als die Nennkapazitaet sein"
        )
    usable = data.get("usable_capacity_kwh")
    if usable is not None and usable > data["nominal_capacity_kwh"] * 1.1:
        raise InvalidBatterySizingConfig(
            "Die nutzbare Kapazitaet liegt deutlich ueber der Nennkapazitaet"
        )

    session = SessionLocal()
    try:
        row = session.get(BatterySizingSettings, _SINGLETON_ID)
        if row is None:
            row = BatterySizingSettings(id=_SINGLETON_ID, updated_at=datetime.now(timezone.utc))
            session.add(row)
        row.nominal_capacity_kwh = data["nominal_capacity_kwh"]
        row.module_capacity_kwh = data["module_capacity_kwh"]
        for name in OPTIONAL_FIELDS:
            setattr(row, name, data.get(name))
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()
    return get_config()
