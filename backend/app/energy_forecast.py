"""Datengetriebene PV-Prognose aus Messhistorie und Open-Meteo-Strahlung."""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select

from .config import settings
from .database import SessionLocal
from .forecast_config import get_config
from .forecast_weather import (
    WeatherPoint,
    WeatherServiceError,
    fetch_forecast_weather,
    fetch_historical_weather,
)
from .models import Reading

FORECAST_DAYS = 7
TRAINING_DAYS = 365
MIN_TRAINING_SAMPLES = 48
CACHE_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class TrainingPoint:
    weather: WeatherPoint
    power_w: float


def _hour_key(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _pure_pv_sql_expression():
    """SQL-Ausdruck fuer reine PV-Leistung je Messpunkt (fuer AVG-Aggregation
    in load_hourly_pv_history()).

    Muss exakt aggregation.pure_pv_power_w() entsprechen (max(0, pv_power_w -
    battery_power_w), Details dort). Diese Aggregation laeuft bewusst in SQL
    statt in Python, weil load_hourly_pv_history() bis zu TRAINING_DAYS Tage
    Rohmessungen (Sekunden-/Minutentakt) zusammenfasst - das rohreihenweise
    Laden in Python waere fuer diese Datenmenge zu teuer (siehe die
    Performance-Arbeit an den Energie-Zeitraum-Uebersichten). Ein
    Cross-Check-Test (test_pure_pv_sql_matches_python_helper) vergleicht
    beide Implementierungen gegen dieselben Beispieldaten, damit sie nicht
    unbemerkt auseinanderlaufen.
    """
    return case(
        (Reading.pv_power_w.is_(None), None),
        else_=func.max(
            0.0,
            Reading.pv_power_w - func.coalesce(Reading.battery_power_w, 0.0),
        ),
    )


def load_hourly_pv_history(
    since: datetime, until: datetime
) -> dict[str, dict[datetime, float]]:
    """Mittlere reine PV-Leistung je UTC-Stunde direkt in SQLite bilden."""
    # Open-Meteo kennzeichnet Strahlung als Mittel der vorangegangenen Stunde.
    # Daher bekommt z.B. die Messstunde 12:00-13:00 den Endzeitpunkt 13:00.
    bucket = func.strftime("%Y-%m-%dT%H:00:00", Reading.timestamp, "+1 hour")
    pure_pv = _pure_pv_sql_expression()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Reading.device_id, bucket, func.avg(pure_pv))
            .where(
                Reading.timestamp >= since,
                Reading.timestamp < until,
                Reading.pv_power_w.is_not(None),
            )
            .group_by(Reading.device_id, bucket)
            .order_by(bucket)
        ).all()
    finally:
        session.close()

    result: dict[str, dict[datetime, float]] = defaultdict(dict)
    for device_id, raw_bucket, power_w in rows:
        if raw_bucket is None or power_w is None:
            continue
        timestamp = datetime.fromisoformat(raw_bucket).replace(tzinfo=timezone.utc)
        result[device_id][timestamp] = max(0.0, float(power_w))
    return dict(result)


def build_training_data(
    pv_history: dict[str, dict[datetime, float]], weather: list[WeatherPoint]
) -> dict[str, list[TrainingPoint]]:
    weather_by_hour = {_hour_key(point.timestamp): point for point in weather}
    result: dict[str, list[TrainingPoint]] = {}
    for device_id, device_history in pv_history.items():
        samples = []
        for timestamp, power_w in device_history.items():
            weather_point = weather_by_hour.get(_hour_key(timestamp))
            if weather_point is not None:
                samples.append(TrainingPoint(weather_point, power_w))
        result[device_id] = samples
    return result


def _cyclic_distance(a: int, b: int, period: int) -> float:
    difference = abs(a - b)
    return min(difference, period - difference)


def _sample_distance(sample: TrainingPoint, target: WeatherPoint) -> float:
    source = sample.weather
    hour_distance = _cyclic_distance(source.timestamp.hour, target.timestamp.hour, 24) / 3
    day_distance = _cyclic_distance(
        source.timestamp.timetuple().tm_yday,
        target.timestamp.timetuple().tm_yday,
        366,
    ) / 45
    ghi_distance = abs(source.shortwave_w_m2 - target.shortwave_w_m2) / 180
    direct_distance = abs(source.direct_w_m2 - target.direct_w_m2) / 180
    diffuse_distance = abs(source.diffuse_w_m2 - target.diffuse_w_m2) / 140
    temperature_distance = abs(source.temperature_c - target.temperature_c) / 20
    return (
        1.8 * hour_distance
        + day_distance
        + 2.5 * ghi_distance
        + direct_distance
        + diffuse_distance
        + 0.25 * temperature_distance
    )


def predict_power(
    training: list[TrainingPoint], target: WeatherPoint
) -> tuple[float, float, float]:
    """KNN-Prognose mit Strahlungsskalierung und empirischem Streubereich."""
    if target.shortwave_w_m2 < 3 or not training:
        return 0.0, 0.0, 0.0

    nearest = sorted(training, key=lambda point: _sample_distance(point, target))[:24]
    observed_max = max(point.power_w for point in training)
    estimates = []
    weights = []
    for sample in nearest:
        source_ghi = sample.weather.shortwave_w_m2
        if source_ghi < 3:
            continue
        scale = max(0.35, min(2.75, target.shortwave_w_m2 / source_ghi))
        estimate = min(observed_max * 1.08, sample.power_w * scale)
        distance = _sample_distance(sample, target)
        estimates.append(estimate)
        weights.append(1.0 / (0.15 + distance))

    if not estimates:
        return 0.0, 0.0, 0.0
    weight_sum = sum(weights)
    expected = sum(value * weight for value, weight in zip(estimates, weights)) / weight_sum
    variance = sum(
        weight * (value - expected) ** 2 for value, weight in zip(estimates, weights)
    ) / weight_sum
    spread = math.sqrt(max(0.0, variance))
    return expected, max(0.0, expected - 1.28 * spread), expected + 1.28 * spread


def _empty_result(message: str) -> dict:
    return {
        "available": False,
        "message": message,
        "generated_at": datetime.now(timezone.utc),
        "training_start": None,
        "training_end": None,
        "training_samples": 0,
        "weather_source": "Open-Meteo",
        "days": [],
        "hours": [],
    }


def _summarize(
    training: dict[str, list[TrainingPoint]], forecast_weather: list[WeatherPoint]
) -> dict:
    device_names = {device.id: device.name for device in settings.inverters}
    local_tz = ZoneInfo(settings.timezone_name)
    per_device_hour: dict[str, dict[datetime, tuple[float, float, float]]] = {}
    # Nur Geraete mit genuegend Historie fliessen in die Vorhersage UND in die
    # unten berichteten Trainings-Metadaten (training_samples/_start/_end)
    # ein - ein zu junges Geraet soll die gemeldete Datengrundlage nicht
    # verzerren.
    used_samples: dict[str, list[TrainingPoint]] = {}
    for device_id, samples in training.items():
        if len(samples) < MIN_TRAINING_SAMPLES:
            continue
        used_samples[device_id] = samples
        per_device_hour[device_id] = {
            point.timestamp - timedelta(hours=1): predict_power(samples, point)
            for point in forecast_weather
        }
    if not per_device_hour:
        return _empty_result(
            "Noch nicht genug historische PV-Daten "
            f"(mindestens {MIN_TRAINING_SAMPLES} Stunden je Wechselrichter)."
        )

    combined_hours = []
    for point in forecast_weather:
        interval_start = point.timestamp - timedelta(hours=1)
        values = [hours[interval_start] for hours in per_device_hour.values()]
        combined_hours.append(
            {
                "timestamp": interval_start,
                "expected_kw": round(sum(value[0] for value in values) / 1000, 3),
                "low_kw": round(sum(value[1] for value in values) / 1000, 3),
                "high_kw": round(sum(value[2] for value in values) / 1000, 3),
            }
        )

    hours_by_day: dict[str, list[dict]] = defaultdict(list)
    for hour in combined_hours:
        date_key = hour["timestamp"].astimezone(local_tz).date().isoformat()
        hours_by_day[date_key].append(hour)

    days = []
    for date_key, day_hours in hours_by_day.items():
        devices = []
        for device_id, predictions in per_device_hour.items():
            device_values = [
                predictions[hour["timestamp"]]
                for hour in day_hours
                if hour["timestamp"] in predictions
            ]
            devices.append(
                {
                    "device_id": device_id,
                    "device_name": device_names.get(device_id, device_id),
                    "expected_kwh": round(sum(value[0] for value in device_values) / 1000, 2),
                    "low_kwh": round(sum(value[1] for value in device_values) / 1000, 2),
                    "high_kwh": round(sum(value[2] for value in device_values) / 1000, 2),
                }
            )
        active = [hour for hour in day_hours if hour["expected_kw"] >= 0.1]
        peak = max(day_hours, key=lambda hour: hour["expected_kw"])
        days.append(
            {
                "date": date_key,
                "expected_kwh": round(sum(hour["expected_kw"] for hour in day_hours), 2),
                "low_kwh": round(sum(hour["low_kw"] for hour in day_hours), 2),
                "high_kwh": round(sum(hour["high_kw"] for hour in day_hours), 2),
                "production_start": active[0]["timestamp"] if active else None,
                "production_end": active[-1]["timestamp"] + timedelta(hours=1) if active else None,
                "peak_at": peak["timestamp"] if peak["expected_kw"] > 0 else None,
                "peak_kw": peak["expected_kw"],
                "devices": devices,
            }
        )

    all_samples = [sample for samples in used_samples.values() for sample in samples]
    return {
        "available": True,
        "message": "Prognose aus historischen PV- und Wetterdaten.",
        "generated_at": datetime.now(timezone.utc),
        "training_start": min(sample.weather.timestamp for sample in all_samples),
        "training_end": max(sample.weather.timestamp for sample in all_samples),
        "training_samples": len(all_samples),
        "weather_source": "Open-Meteo",
        "days": days,
        "hours": combined_hours,
    }


async def build_forecast() -> dict:
    config = get_config()
    if not config["enabled"]:
        return _empty_result("PV-Prognose ist im Admin-Bereich deaktiviert.")
    latitude, longitude = config["latitude"], config["longitude"]
    if latitude is None or longitude is None:
        return _empty_result("Standortkoordinaten fehlen.")

    now = datetime.now(timezone.utc)
    until = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = until - timedelta(days=TRAINING_DAYS)
    history = await asyncio.to_thread(load_hourly_pv_history, since, until)
    if not history:
        return _empty_result("Noch keine historischen PV-Daten vorhanden.")
    earliest = min(timestamp for device in history.values() for timestamp in device)
    historical_weather, forecast_weather = await asyncio.gather(
        fetch_historical_weather(
            latitude,
            longitude,
            earliest.date(),
            (until - timedelta(days=1)).date(),
        ),
        fetch_forecast_weather(latitude, longitude, FORECAST_DAYS),
    )
    training = build_training_data(history, historical_weather)
    return _summarize(training, forecast_weather)


class ForecastService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cached: dict | None = None
        self._cached_at: datetime | None = None

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = None

    async def get(self) -> dict:
        now = datetime.now(timezone.utc)
        if self._cached is not None and self._cached_at and now - self._cached_at < CACHE_TTL:
            return self._cached
        async with self._lock:
            now = datetime.now(timezone.utc)
            if self._cached is not None and self._cached_at and now - self._cached_at < CACHE_TTL:
                return self._cached
            try:
                result = await build_forecast()
            except WeatherServiceError as exc:
                result = _empty_result(str(exc))
            self._cached = result
            self._cached_at = now
            return result


forecast_service = ForecastService()
