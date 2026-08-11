"""Minimale, persistente Konfiguration der datengetriebenen PV-Prognose."""
from __future__ import annotations

from datetime import datetime, timezone

from .config import settings
from .database import SessionLocal
from .models import ForecastSettings

_SINGLETON_ID = 1


class InvalidForecastConfig(ValueError):
    """Semantisch ungueltige Prognose-Konfiguration."""


def _file_defaults() -> dict:
    configured_inverter = next(
        (
            inverter
            for inverter in settings.inverters
            if inverter.latitude is not None and inverter.longitude is not None
        ),
        None,
    )
    latitude = configured_inverter.latitude if configured_inverter else None
    longitude = configured_inverter.longitude if configured_inverter else None
    return {
        "enabled": latitude is not None and longitude is not None,
        "latitude": latitude,
        "longitude": longitude,
        "source": "inverters.json",
    }


def get_config() -> dict:
    session = SessionLocal()
    try:
        row = session.get(ForecastSettings, _SINGLETON_ID)
        if row is None:
            return _file_defaults()
        return {
            "enabled": row.enabled,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "source": "database",
        }
    finally:
        session.close()


def update_config(data: dict) -> dict:
    if data["enabled"] and (data["latitude"] is None or data["longitude"] is None):
        raise InvalidForecastConfig(
            "Fuer eine aktive Prognose sind Breiten- und Laengengrad erforderlich"
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
        row.latitude = data["latitude"]
        row.longitude = data["longitude"]
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()
    return get_config()
