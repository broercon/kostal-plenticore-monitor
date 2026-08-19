"""Open-Meteo-Zugriff fuer historische und vorhergesagte Strahlungsdaten."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import aiohttp

HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARIABLES = (
    "shortwave_radiation,direct_radiation,diffuse_radiation,temperature_2m,"
    "cloud_cover,wind_speed_10m,relative_humidity_2m,snow_depth,surface_pressure"
)
# Windgeschwindigkeit explizit in m/s anfordern (Open-Meteo liefert sonst
# km/h) - passend zum Namensschema der uebrigen WeatherPoint-Felder
# (z.B. shortwave_w_m2, temperature_c), siehe auch energy_forecast.py's
# _estimate_cell_temperature_c(), die m/s erwartet (Faiman-Modell).
WIND_SPEED_UNIT = "ms"


class WeatherServiceError(RuntimeError):
    """Open-Meteo war nicht erreichbar oder lieferte ungueltige Daten."""


@dataclass(frozen=True)
class WeatherPoint:
    timestamp: datetime
    shortwave_w_m2: float
    direct_w_m2: float
    diffuse_w_m2: float
    temperature_c: float
    # Zusaetzliche Werte fuer die Prognose (siehe energy_forecast.py's
    # _forecast_feature_vector/_estimate_cell_temperature_c) - cloud_cover
    # ist zwar teilweise redundant zu den Strahlungswerten (die deren
    # Ursache widerspiegeln), kann aber z.B. Dunst von echtem Klarhimmel
    # unterscheiden helfen. wind_speed_ms fliesst nicht direkt, sondern nur
    # ueber die abgeleitete Zelltemperatur ein (siehe dort).
    # Defaults entsprechen einer neutralen "Standardatmosphaere" (kein Wind,
    # mittlere Bewoelkung/Feuchte, kein Schnee, Normaldruck) - vor allem
    # damit bestehender Testcode, der WeatherPoint ohne diese neuen Felder
    # konstruiert, unveraendert weiterlaeuft. Produktionscode
    # (forecast_weather._parse_points/weather_cache) setzt immer die
    # tatsaechlich von Open-Meteo bzw. dem Cache gelieferten Werte.
    cloud_cover_percent: float = 50.0
    wind_speed_ms: float = 0.0
    humidity_percent: float = 50.0
    snow_depth_m: float = 0.0
    pressure_hpa: float = 1013.25


def _parse_points(payload: dict) -> list[WeatherPoint]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    variables = [
        hourly.get("shortwave_radiation") or [],
        hourly.get("direct_radiation") or [],
        hourly.get("diffuse_radiation") or [],
        hourly.get("temperature_2m") or [],
        hourly.get("cloud_cover") or [],
        hourly.get("wind_speed_10m") or [],
        hourly.get("relative_humidity_2m") or [],
        hourly.get("snow_depth") or [],
        hourly.get("surface_pressure") or [],
    ]
    if not times or any(len(values) != len(times) for values in variables):
        raise WeatherServiceError("Open-Meteo lieferte unvollstaendige Stundenwerte")

    result = []
    for index, raw_time in enumerate(times):
        values = [items[index] for items in variables]
        if any(value is None for value in values):
            continue
        timestamp = datetime.fromisoformat(raw_time)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        result.append(
            WeatherPoint(
                timestamp=timestamp.astimezone(timezone.utc),
                shortwave_w_m2=max(0.0, float(values[0])),
                direct_w_m2=max(0.0, float(values[1])),
                diffuse_w_m2=max(0.0, float(values[2])),
                temperature_c=float(values[3]),
                cloud_cover_percent=min(100.0, max(0.0, float(values[4]))),
                wind_speed_ms=max(0.0, float(values[5])),
                humidity_percent=min(100.0, max(0.0, float(values[6]))),
                snow_depth_m=max(0.0, float(values[7])),
                pressure_hpa=float(values[8]),
            )
        )
    return result


async def _fetch(url: str, params: dict) -> list[WeatherPoint]:
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    detail = (await response.text())[:300]
                    raise WeatherServiceError(
                        f"Open-Meteo antwortete mit HTTP {response.status}: {detail}"
                    )
                return _parse_points(await response.json())
    except WeatherServiceError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise WeatherServiceError(f"Open-Meteo ist nicht erreichbar: {exc}") from exc


async def fetch_historical_weather(
    latitude: float, longitude: float, start: date, end: date
) -> list[WeatherPoint]:
    return await _fetch(
        HISTORICAL_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": HOURLY_VARIABLES,
            "timezone": "UTC",
            "wind_speed_unit": WIND_SPEED_UNIT,
        },
    )


async def fetch_forecast_weather(
    latitude: float, longitude: float, days: int = 7
) -> list[WeatherPoint]:
    return await _fetch(
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "forecast_days": days,
            "hourly": HOURLY_VARIABLES,
            "timezone": "UTC",
            "wind_speed_unit": WIND_SPEED_UNIT,
        },
    )
