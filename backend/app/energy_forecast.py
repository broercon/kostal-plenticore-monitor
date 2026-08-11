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

# Regularisierungsstaerke der Ridge-Regression in fit_distance_weights()
# (auf standardisierten Merkmalen, siehe dort). Kein Gradientenabstieg
# noetig - die Loesung ist geschlossen (siehe _solve_linear_system()).
RIDGE_LAMBDA = 5.0


@dataclass(frozen=True)
class TrainingPoint:
    weather: WeatherPoint
    power_w: float


@dataclass(frozen=True)
class DistanceWeights:
    """Relative Wichtigkeit jeder Merkmalsdimension in _sample_distance().

    Ersetzt handgeschaetzte Konstanten durch pro Wechselrichter aus dessen
    eigener Historie gelernte Werte, siehe fit_distance_weights().
    """

    hour: float
    day: float
    ghi: float
    direct: float
    diffuse: float
    temperature: float


# Bisherige, von Hand geschaetzte Gewichte - Fallback, wenn fit_distance_
# weights() nicht genug/zu entartete Daten fuer eine stabile Schaetzung hat.
# Die Vorhersage kann dadurch nie schlechter werden als vor der Umstellung
# auf gelernte Gewichte, bestenfalls besser.
DEFAULT_DISTANCE_WEIGHTS = DistanceWeights(
    hour=1.8, day=1.0, ghi=2.5, direct=1.0, diffuse=1.0, temperature=0.25
)


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


def _sample_distance(
    sample: TrainingPoint, target: WeatherPoint, weights: DistanceWeights
) -> float:
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
        weights.hour * hour_distance
        + weights.day * day_distance
        + weights.ghi * ghi_distance
        + weights.direct * direct_distance
        + weights.diffuse * diffuse_distance
        + weights.temperature * temperature_distance
    )


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Loest matrix @ x = vector per Gauss-Elimination mit Zeilenpivotisierung.

    Reicht fuer die kleinen (hier: 9x9) Normalgleichungssysteme der
    Ridge-Regression unten voellig aus - eine numpy/scipy-Abhaengigkeit nur
    dafuer waere unverhaeltnismaessig. Gibt None zurueck, wenn die Matrix
    (trotz Ridge-Regularisierung) numerisch singulaer ist; matrix/vector
    werden nicht veraendert.
    """
    n = len(vector)
    aug = [matrix[i][:] + [vector[i]] for i in range(n)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-12:
            return None
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pivot = aug[col][col]
        aug[col] = [value / pivot for value in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor:
                aug[row] = [a - factor * b for a, b in zip(aug[row], aug[col])]
    return [aug[i][n] for i in range(n)]


def _forecast_feature_vector(point: WeatherPoint) -> list[float]:
    """8 Merkmale je Messpunkt fuer fit_distance_weights(): Tageszeit und
    Jahrestag als Sinus/Kosinus (damit der Jahres-/Tageswechsel fuer die
    lineare Regression keinen kuenstlichen Sprung erzeugt), plus Strahlung
    und Temperatur direkt.
    """
    hour_angle = 2 * math.pi * (point.timestamp.hour + point.timestamp.minute / 60) / 24
    day_angle = 2 * math.pi * point.timestamp.timetuple().tm_yday / 366
    return [
        math.sin(hour_angle),
        math.cos(hour_angle),
        math.sin(day_angle),
        math.cos(day_angle),
        point.shortwave_w_m2,
        point.direct_w_m2,
        point.diffuse_w_m2,
        point.temperature_c,
    ]


def fit_distance_weights(training: list[TrainingPoint]) -> DistanceWeights:
    """Lernt die relative Wichtigkeit jeder Merkmalsdimension in
    _sample_distance() aus der Historie eines einzelnen Wechselrichters,
    statt sie (wie bisher in DEFAULT_DISTANCE_WEIGHTS) von Hand zu schaetzen.

    Vorgehen: Ridge-Regression (kleinste Quadrate + L2-Strafe, geschlossene
    Loesung - kein Gradientenabstieg/Backpropagation) auf standardisierten
    Merkmalen (siehe _forecast_feature_vector) sagt die beobachtete Leistung
    voraus; die Betrags-Koeffizienten zeigen dann, wie stark jedes Merkmal
    tatsaechlich mit der Leistung zusammenhaengt. Diese Wichtigkeiten werden
    auf dieselbe Gesamtsumme wie DEFAULT_DISTANCE_WEIGHTS skaliert, damit die
    uebrigen Konstanten der k-NN-Vorhersage (Nachbarnzahl, Distanz-Offset in
    predict_power) ihre bisherige Bedeutung behalten - nur die *relative*
    Gewichtung der Merkmale wird durch die Regression ersetzt.

    Der eigentliche Vorhersage-Mechanismus (Analogie-Suche + physikalisch
    begrenzte Strahlungs-Skalierung in predict_power) bleibt unveraendert.
    Reicht die Historie nicht (weniger als MIN_TRAINING_SAMPLES) oder ist ein
    Merkmal darin konstant (z.B. noch keine Temperaturstreuung) bzw. das
    Gleichungssystem trotz Regularisierung singulaer, wird auf die
    bisherigen Standardgewichte zurueckgefallen - die Vorhersage kann dadurch
    nie schlechter werden als vor dieser Umstellung.
    """
    if len(training) < MIN_TRAINING_SAMPLES:
        return DEFAULT_DISTANCE_WEIGHTS

    rows = [_forecast_feature_vector(sample.weather) for sample in training]
    targets = [sample.power_w for sample in training]
    n_features = len(rows[0])
    n_samples = len(rows)

    means = [sum(row[j] for row in rows) / n_samples for j in range(n_features)]
    stds = [
        math.sqrt(sum((row[j] - means[j]) ** 2 for row in rows) / n_samples)
        for j in range(n_features)
    ]
    if any(std < 1e-9 for std in stds):
        return DEFAULT_DISTANCE_WEIGHTS

    standardized = [
        [(row[j] - means[j]) / stds[j] for j in range(n_features)] for row in rows
    ]
    design = [row + [1.0] for row in standardized]  # Bias-Spalte fuer den Achsenabschnitt.
    n_params = n_features + 1

    xtx = [
        [sum(design[i][a] * design[i][b] for i in range(n_samples)) for b in range(n_params)]
        for a in range(n_params)
    ]
    for i in range(n_features):  # Ridge-Strafe; Bias-Spalte bleibt unregularisiert.
        xtx[i][i] += RIDGE_LAMBDA
    xty = [sum(design[i][a] * targets[i] for i in range(n_samples)) for a in range(n_params)]

    coefficients = _solve_linear_system(xtx, xty)
    if coefficients is None:
        return DEFAULT_DISTANCE_WEIGHTS

    hour_importance = math.hypot(coefficients[0], coefficients[1])
    day_importance = math.hypot(coefficients[2], coefficients[3])
    ghi_importance = abs(coefficients[4])
    direct_importance = abs(coefficients[5])
    diffuse_importance = abs(coefficients[6])
    temperature_importance = abs(coefficients[7])
    total_importance = (
        hour_importance
        + day_importance
        + ghi_importance
        + direct_importance
        + diffuse_importance
        + temperature_importance
    )
    if total_importance < 1e-9:
        return DEFAULT_DISTANCE_WEIGHTS

    default_total = (
        DEFAULT_DISTANCE_WEIGHTS.hour
        + DEFAULT_DISTANCE_WEIGHTS.day
        + DEFAULT_DISTANCE_WEIGHTS.ghi
        + DEFAULT_DISTANCE_WEIGHTS.direct
        + DEFAULT_DISTANCE_WEIGHTS.diffuse
        + DEFAULT_DISTANCE_WEIGHTS.temperature
    )
    scale = default_total / total_importance
    return DistanceWeights(
        hour=hour_importance * scale,
        day=day_importance * scale,
        ghi=ghi_importance * scale,
        direct=direct_importance * scale,
        diffuse=diffuse_importance * scale,
        temperature=temperature_importance * scale,
    )


def predict_power(
    training: list[TrainingPoint],
    target: WeatherPoint,
    weights: DistanceWeights | None = None,
) -> tuple[float, float, float]:
    """KNN-Prognose mit Strahlungsskalierung und empirischem Streubereich.

    weights bestimmt die relative Wichtigkeit der Merkmalsdimensionen in der
    Distanzmetrik (siehe fit_distance_weights). Per Default (None) werden sie
    aus training gelernt; wer wiederholt fuer denselben Trainingsdatensatz
    vorhersagt (siehe _summarize(), eine Vorhersage je Prognosestunde),
    sollte sie einmal vorab berechnen und explizit uebergeben, statt sie bei
    jedem Aufruf neu zu lernen.
    """
    if target.shortwave_w_m2 < 3 or not training:
        return 0.0, 0.0, 0.0
    if weights is None:
        weights = fit_distance_weights(training)

    nearest = sorted(training, key=lambda point: _sample_distance(point, target, weights))[:24]
    observed_max = max(point.power_w for point in training)
    estimates = []
    sample_weights = []
    for sample in nearest:
        source_ghi = sample.weather.shortwave_w_m2
        if source_ghi < 3:
            continue
        scale = max(0.35, min(2.75, target.shortwave_w_m2 / source_ghi))
        estimate = min(observed_max * 1.08, sample.power_w * scale)
        distance = _sample_distance(sample, target, weights)
        estimates.append(estimate)
        sample_weights.append(1.0 / (0.15 + distance))

    if not estimates:
        return 0.0, 0.0, 0.0
    weight_sum = sum(sample_weights)
    expected = (
        sum(value * weight for value, weight in zip(estimates, sample_weights)) / weight_sum
    )
    variance = sum(
        weight * (value - expected) ** 2 for value, weight in zip(estimates, sample_weights)
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
        # Gewichte einmal je Geraet lernen (nicht je Prognosestunde) - sie
        # haengen nur von der Trainingshistorie ab, siehe fit_distance_weights.
        weights = fit_distance_weights(samples)
        per_device_hour[device_id] = {
            point.timestamp - timedelta(hours=1): predict_power(samples, point, weights)
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
