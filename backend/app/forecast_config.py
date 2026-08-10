"""Persistente Prognose-Konfiguration mit Fallback auf inverters.json."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select

from .config import settings
from .database import SessionLocal
from .models import ForecastSettings, PVArraySettings

_SINGLETON_ID = 1


class InvalidForecastConfig(ValueError):
    """Semantisch ungueltige Prognose-Konfiguration."""


def _effective_kwp(array: dict) -> float:
    if array.get("peak_power_kwp") is not None:
        return float(array["peak_power_kwp"])
    count = array.get("module_count")
    power = array.get("module_power_wp")
    if count is not None and power is not None:
        return float(count) * float(power) / 1000.0
    return 0.0


def _device_names() -> dict[str, str]:
    return {inv.id: inv.name for inv in settings.inverters}


def _file_defaults() -> dict:
    latitude = next(
        (inv.latitude for inv in settings.inverters if inv.latitude is not None), None
    )
    longitude = next(
        (inv.longitude for inv in settings.inverters if inv.longitude is not None), None
    )
    location_name = next(
        (inv.location_name for inv in settings.inverters if inv.location_name), ""
    )
    arrays: list[dict] = []
    for inv in settings.inverters:
        for array in inv.pv_arrays or []:
            item = {
                "id": None,
                "device_id": inv.id,
                "device_name": inv.name,
                "name": array.name,
                "module_count": array.module_count,
                "module_power_wp": array.module_power_wp,
                "peak_power_kwp": array.peak_power_kwp,
                "tilt_degrees": array.tilt_degrees,
                "azimuth_degrees": array.azimuth_degrees,
                "inverter_limit_kw": array.inverter_limit_kw,
                "enabled": array.enabled,
            }
            item["effective_peak_power_kwp"] = _effective_kwp(item)
            arrays.append(item)
    return {
        "enabled": bool(latitude is not None and longitude is not None and arrays),
        "location_name": location_name,
        "latitude": latitude,
        "longitude": longitude,
        "forecast_days": 7,
        "system_loss_percent": 14.0,
        "source": "inverters.json",
        "arrays": arrays,
    }


def _row_to_dict(settings_row: ForecastSettings, array_rows: list[PVArraySettings]) -> dict:
    names = _device_names()
    arrays = []
    for row in array_rows:
        item = {
            "id": row.id,
            "device_id": row.device_id,
            "device_name": names.get(row.device_id, row.device_id),
            "name": row.name,
            "module_count": row.module_count,
            "module_power_wp": row.module_power_wp,
            "peak_power_kwp": row.peak_power_kwp,
            "tilt_degrees": row.tilt_degrees,
            "azimuth_degrees": row.azimuth_degrees,
            "inverter_limit_kw": row.inverter_limit_kw,
            "enabled": row.enabled,
        }
        item["effective_peak_power_kwp"] = _effective_kwp(item)
        arrays.append(item)
    return {
        "enabled": settings_row.enabled,
        "location_name": settings_row.location_name,
        "latitude": settings_row.latitude,
        "longitude": settings_row.longitude,
        "forecast_days": settings_row.forecast_days,
        "system_loss_percent": settings_row.system_loss_percent,
        "source": "database",
        "arrays": arrays,
    }


def get_config() -> dict:
    session = SessionLocal()
    try:
        row = session.get(ForecastSettings, _SINGLETON_ID)
        if row is None:
            return _file_defaults()
        arrays = list(
            session.scalars(
                select(PVArraySettings).order_by(
                    PVArraySettings.sort_order, PVArraySettings.id
                )
            )
        )
        return _row_to_dict(row, arrays)
    finally:
        session.close()


def update_config(data: dict) -> dict:
    known_devices = _device_names()
    for array in data["arrays"]:
        if array["device_id"] not in known_devices:
            raise InvalidForecastConfig(
                f"Unbekannter Wechselrichter: {array['device_id']}"
            )
        if _effective_kwp(array) <= 0:
            raise InvalidForecastConfig(
                f"{array['name']}: kWp oder Modulanzahl und Wp je Modul angeben"
            )
    if data["enabled"] and (data["latitude"] is None or data["longitude"] is None):
        raise InvalidForecastConfig(
            "Fuer eine aktive Prognose sind Breiten- und Laengengrad erforderlich"
        )
    if data["enabled"] and not any(a.get("enabled", True) for a in data["arrays"]):
        raise InvalidForecastConfig(
            "Fuer eine aktive Prognose ist mindestens ein PV-Feld erforderlich"
        )

    session = SessionLocal()
    try:
        row = session.get(ForecastSettings, _SINGLETON_ID)
        if row is None:
            row = ForecastSettings(
                id=_SINGLETON_ID, updated_at=datetime.now(timezone.utc)
            )
            session.add(row)
        row.enabled = data["enabled"]
        row.location_name = data["location_name"].strip()
        row.latitude = data["latitude"]
        row.longitude = data["longitude"]
        row.forecast_days = data["forecast_days"]
        row.system_loss_percent = data["system_loss_percent"]
        row.updated_at = datetime.now(timezone.utc)

        session.execute(delete(PVArraySettings))
        for index, array in enumerate(data["arrays"]):
            session.add(
                PVArraySettings(
                    device_id=array["device_id"],
                    name=array["name"].strip(),
                    module_count=array.get("module_count"),
                    module_power_wp=array.get("module_power_wp"),
                    peak_power_kwp=array.get("peak_power_kwp"),
                    tilt_degrees=array["tilt_degrees"],
                    azimuth_degrees=array["azimuth_degrees"],
                    inverter_limit_kw=array.get("inverter_limit_kw"),
                    enabled=array.get("enabled", True),
                    sort_order=index,
                )
            )
        session.commit()
    finally:
        session.close()
    return get_config()
