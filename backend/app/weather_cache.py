"""Lokaler Zwischenspeicher fuer historische Open-Meteo-Wetterstunden.

Grundidee: Historische Wetterdaten fuer eine bestimmte Stunde aendern sich
irgendwann nicht mehr ("ausgereift"), sobald der zugrunde liegende
Modelllauf bei Open-Meteo abgeschlossen ist (siehe WeatherHourly-Docstring
in models.py fuer die ausfuehrliche Begruendung). Nur ausgereifte Stunden
werden dauerhaft gespeichert und nie wieder ueberschrieben - juengere
Stunden werden bewusst jedes Mal frisch von Open-Meteo geholt, da sie sich
noch aendern koennen.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import SessionLocal
from .forecast_weather import WeatherPoint, fetch_historical_weather
from .models import WeatherHourly

# Puffer, bevor eine historische Stunde als "ausgereift" gilt und dauerhaft
# im Cache landet. Grosszuegig gewaehlt (deutlich mehr als die 1-6h-
# Aktualisierungsintervalle der zugrunde liegenden Wettermodelle), um auch
# langsamere Nachkorrekturen sicher abzudecken.
WEATHER_CACHE_MATURITY_DAYS = 3

_COORD_PRECISION = 4  # ca. 11m Genauigkeit


def _round_coord(value: float) -> float:
    return round(value, _COORD_PRECISION)


def _load_cached_points(
    latitude: float, longitude: float, start: date, end: date
) -> list[WeatherPoint]:
    """Bereits gespeicherte Stunden im Bereich [start, end] (inklusive)."""
    range_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    range_end = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(
        days=1
    )
    session = SessionLocal()
    try:
        rows = session.execute(
            select(WeatherHourly).where(
                WeatherHourly.latitude == _round_coord(latitude),
                WeatherHourly.longitude == _round_coord(longitude),
                WeatherHourly.timestamp >= range_start,
                WeatherHourly.timestamp < range_end,
            )
        ).scalars().all()
    finally:
        session.close()
    return [
        WeatherPoint(
            # SQLite gibt DateTime(timezone=True)-Spalten naiv zurueck (die
            # tz-Info wird beim Schreiben nicht mitgespeichert) - ohne dieses
            # Wiederanheften waeren die aus dem Cache geladenen Zeitstempel
            # nicht mehr mit den frisch von Open-Meteo geholten (aware)
            # Zeitstempeln vergleichbar. Alle Zeitstempel in dieser Tabelle
            # sind laut Vertrag UTC (siehe _store_points/fetch_historical_weather).
            timestamp=row.timestamp.replace(tzinfo=timezone.utc),
            shortwave_w_m2=row.shortwave_w_m2,
            direct_w_m2=row.direct_w_m2,
            diffuse_w_m2=row.diffuse_w_m2,
            temperature_c=row.temperature_c,
            cloud_cover_percent=row.cloud_cover_percent,
            wind_speed_ms=row.wind_speed_ms,
            humidity_percent=row.humidity_percent,
            snow_depth_m=row.snow_depth_m,
            pressure_hpa=row.pressure_hpa,
        )
        for row in rows
        # Zeilen aus einer Zeit vor den zusaetzlichen Wetterwerten (siehe
        # database._ensure_weather_hourly_extra_columns) haben fuer die
        # neuen Spalten NULL - die Migration loescht solche Zeilen zwar
        # bereits beim Start, dieser Filter ist nur ein zusaetzliches
        # Sicherheitsnetz, damit WeatherPoint nie mit None-Werten gebaut
        # wird (die Felder sind dort als float, nicht float | None, typisiert).
        if row.cloud_cover_percent is not None
    ]


def _cached_dates(latitude: float, longitude: float, start: date, end: date) -> set[date]:
    """Kalendertage im Bereich, fuer die bereits alle 24 Stunden vorliegen.

    Ein Tag zaehlt nur als vollstaendig gecacht, wenn genau 24 Stunden
    gespeichert sind - bei einer nur teilweise gefuellten Zeile (sollte im
    Normalbetrieb nicht vorkommen) wird der Tag sicherheitshalber erneut
    komplett von Open-Meteo abgerufen statt mit Luecken weiterverwendet.
    """
    points = _load_cached_points(latitude, longitude, start, end)
    counts: dict[date, int] = {}
    for point in points:
        day = point.timestamp.date()
        counts[day] = counts.get(day, 0) + 1
    return {day for day, count in counts.items() if count == 24}


def _store_points(latitude: float, longitude: float, points: list[WeatherPoint]) -> None:
    if not points:
        return
    now = datetime.now(timezone.utc)
    lat = _round_coord(latitude)
    lon = _round_coord(longitude)
    session = SessionLocal()
    try:
        existing = set(
            session.execute(
                select(WeatherHourly.timestamp).where(
                    WeatherHourly.latitude == lat,
                    WeatherHourly.longitude == lon,
                    WeatherHourly.timestamp.in_([p.timestamp for p in points]),
                )
            ).scalars()
        )
        for point in points:
            if point.timestamp in existing:
                continue
            session.add(
                WeatherHourly(
                    latitude=lat,
                    longitude=lon,
                    timestamp=point.timestamp,
                    shortwave_w_m2=point.shortwave_w_m2,
                    direct_w_m2=point.direct_w_m2,
                    diffuse_w_m2=point.diffuse_w_m2,
                    temperature_c=point.temperature_c,
                    cloud_cover_percent=point.cloud_cover_percent,
                    wind_speed_ms=point.wind_speed_ms,
                    humidity_percent=point.humidity_percent,
                    snow_depth_m=point.snow_depth_m,
                    pressure_hpa=point.pressure_hpa,
                    fetched_at=now,
                )
            )
        try:
            session.commit()
        except IntegrityError:
            # Gleichzeitiger Schreibzugriff (z.B. zwei parallele
            # Prognoselaeufe) hat die Zeile inzwischen bereits angelegt -
            # das ist unproblematisch, da ausgereifte Stunden ohnehin nie
            # widerspruechlich sind (dieselbe historische Stunde liefert
            # immer denselben Wert).
            session.rollback()
    finally:
        session.close()


def _contiguous_ranges(missing: set[date], start: date, end: date) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    current = start
    while current <= end:
        if current not in missing:
            current += timedelta(days=1)
            continue
        range_start = current
        while current <= end and current in missing:
            current += timedelta(days=1)
        ranges.append((range_start, current - timedelta(days=1)))
    return ranges


async def fetch_historical_weather_cached(
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    now: datetime | None = None,
) -> list[WeatherPoint]:
    """Wie fetch_historical_weather(), aber mit lokalem Cache fuer
    ausgereifte (siehe WEATHER_CACHE_MATURITY_DAYS) Vergangenheitsstunden.

    Juengere Stunden im angefragten Bereich werden weiterhin bei jedem
    Aufruf live von Open-Meteo geholt, da sie sich noch aendern koennen.
    """
    if start > end:
        return []
    now = now or datetime.now(timezone.utc)
    mature_end = min(end, (now - timedelta(days=WEATHER_CACHE_MATURITY_DAYS)).date())

    points: list[WeatherPoint] = []

    if start <= mature_end:
        cached_dates = _cached_dates(latitude, longitude, start, mature_end)
        all_dates = {
            start + timedelta(days=offset)
            for offset in range((mature_end - start).days + 1)
        }
        missing = all_dates - cached_dates
        for range_start, range_end in _contiguous_ranges(missing, start, mature_end):
            fetched = await fetch_historical_weather(
                latitude, longitude, range_start, range_end
            )
            _store_points(latitude, longitude, fetched)
        points.extend(_load_cached_points(latitude, longitude, start, mature_end))

    recent_start = mature_end + timedelta(days=1)
    if recent_start <= end:
        points.extend(
            await fetch_historical_weather(latitude, longitude, recent_start, end)
        )

    points.sort(key=lambda point: point.timestamp)
    return points
