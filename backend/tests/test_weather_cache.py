"""Tests fuer den lokalen Wetterhistorie-Cache (app.weather_cache)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from app.database import SessionLocal
from app.forecast_weather import WeatherPoint
from app.models import WeatherHourly
from app.weather_cache import (
    WEATHER_CACHE_MATURITY_DAYS,
    _cached_dates,
    fetch_historical_weather_cached,
)


def _points_for_day(day: date) -> list[WeatherPoint]:
    return [
        WeatherPoint(
            timestamp=datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc),
            shortwave_w_m2=100.0 + hour,
            direct_w_m2=70.0 + hour,
            diffuse_w_m2=30.0 + hour,
            temperature_c=15.0,
        )
        for hour in range(24)
    ]


def test_mature_range_is_fetched_once_and_then_served_from_cache(client, monkeypatch):
    now = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)
    day1 = date(2026, 6, 1)
    day2 = date(2026, 6, 2)

    calls: list[tuple[date, date]] = []

    async def fake_fetch(latitude, longitude, start, end):
        calls.append((start, end))
        points: list[WeatherPoint] = []
        current = start
        while current <= end:
            points.extend(_points_for_day(current))
            current += timedelta(days=1)
        return points

    monkeypatch.setattr("app.weather_cache.fetch_historical_weather", fake_fetch)

    result_1 = asyncio.run(fetch_historical_weather_cached(50.0, 8.0, day1, day2, now=now))
    assert len(result_1) == 48
    assert calls == [(day1, day2)]

    # Zweiter Aufruf mit denselben (ausgereiften) Tagen darf Open-Meteo NICHT
    # erneut anfragen - die Werte kommen komplett aus dem Cache.
    result_2 = asyncio.run(fetch_historical_weather_cached(50.0, 8.0, day1, day2, now=now))
    assert len(result_2) == 48
    assert calls == [(day1, day2)]  # unveraendert, kein zweiter Fetch
    assert [p.shortwave_w_m2 for p in result_2] == [p.shortwave_w_m2 for p in result_1]


def test_recent_immature_hours_are_always_fetched_live(client, monkeypatch):
    now = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)
    mature_start = date(2026, 6, 1)
    mature_end = (now - timedelta(days=WEATHER_CACHE_MATURITY_DAYS)).date()
    recent_end = now.date() - timedelta(days=1)  # innerhalb der Reifegrenze
    assert mature_end < recent_end

    calls: list[tuple[date, date]] = []

    async def fake_fetch(latitude, longitude, start, end):
        calls.append((start, end))
        points: list[WeatherPoint] = []
        current = start
        while current <= end:
            points.extend(_points_for_day(current))
            current += timedelta(days=1)
        return points

    monkeypatch.setattr("app.weather_cache.fetch_historical_weather", fake_fetch)

    asyncio.run(
        fetch_historical_weather_cached(50.0, 8.0, mature_start, recent_end, now=now)
    )
    # der komplette (bisher ungecachte) reife Bereich wird einmal geholt,
    # dazu getrennt der junge, noch nicht ausgereifte Rest.
    assert calls == [
        (mature_start, mature_end),
        (mature_end + timedelta(days=1), recent_end),
    ]

    calls.clear()
    asyncio.run(
        fetch_historical_weather_cached(50.0, 8.0, mature_start, recent_end, now=now)
    )
    # der reife Bereich kommt beim zweiten Mal komplett aus dem Cache, der
    # junge Rest wird trotzdem erneut live angefragt.
    assert calls == [(mature_end + timedelta(days=1), recent_end)]


def test_cache_is_scoped_per_location(client, monkeypatch):
    now = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)
    day = date(2026, 6, 1)

    calls: list[tuple[float, float]] = []

    async def fake_fetch(latitude, longitude, start, end):
        calls.append((latitude, longitude))
        return _points_for_day(start)

    monkeypatch.setattr("app.weather_cache.fetch_historical_weather", fake_fetch)

    asyncio.run(fetch_historical_weather_cached(50.0, 8.0, day, day, now=now))
    asyncio.run(fetch_historical_weather_cached(52.5, 13.4, day, day, now=now))
    assert calls == [(50.0, 8.0), (52.5, 13.4)]  # zweiter Standort ist kein Cache-Treffer

    calls.clear()
    asyncio.run(fetch_historical_weather_cached(50.0, 8.0, day, day, now=now))
    assert calls == []  # erster Standort weiterhin aus dem Cache


def test_cached_dates_ignores_incomplete_days(client):
    day = date(2026, 6, 1)
    db = SessionLocal()
    try:
        db.add(
            WeatherHourly(
                latitude=50.0,
                longitude=8.0,
                timestamp=datetime(2026, 6, 1, 5, tzinfo=timezone.utc),
                shortwave_w_m2=100.0,
                direct_w_m2=70.0,
                diffuse_w_m2=30.0,
                temperature_c=15.0,
                fetched_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()

    # Nur eine von 24 Stunden gespeichert -> Tag zaehlt nicht als vollstaendig.
    assert _cached_dates(50.0, 8.0, day, day) == set()
