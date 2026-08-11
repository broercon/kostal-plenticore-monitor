"""Open-Meteo-Zugriff fuer historische und vorhergesagte Strahlungsdaten."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import aiohttp

HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARIABLES = (
    "shortwave_radiation,direct_radiation,diffuse_radiation,temperature_2m"
)


class WeatherServiceError(RuntimeError):
    """Open-Meteo war nicht erreichbar oder lieferte ungueltige Daten."""


@dataclass(frozen=True)
class WeatherPoint:
    timestamp: datetime
    shortwave_w_m2: float
    direct_w_m2: float
    diffuse_w_m2: float
    temperature_c: float


def _parse_points(payload: dict) -> list[WeatherPoint]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    variables = [
        hourly.get("shortwave_radiation") or [],
        hourly.get("direct_radiation") or [],
        hourly.get("diffuse_radiation") or [],
        hourly.get("temperature_2m") or [],
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
        },
    )
