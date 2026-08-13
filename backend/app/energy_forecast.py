"""Datengetriebene PV-Prognose aus Messhistorie und Open-Meteo-Strahlung."""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
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
BACKTEST_MIN_SAMPLES = 14 * 24
BACKTEST_MIN_DAYLIGHT_SAMPLES = 24
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
    """Relative Wichtigkeit jeder Merkmalsdimension in _sample_distances().

    Ersetzt handgeschaetzte Konstanten durch pro Wechselrichter aus dessen
    eigener Historie gelernte Werte, siehe fit_distance_weights().
    """

    hour: float
    day: float
    ghi: float
    direct: float
    diffuse: float
    temperature: float


@dataclass(frozen=True)
class ModelProfile:
    """Ergebnis des zeitlich getrennten Modell-Rueckvergleichs."""

    weights: DistanceWeights
    method: str
    validation_samples: int
    validation_error_percent: float | None
    interval_error_fraction: float | None


# Bisherige, von Hand geschaetzte Gewichte - Fallback, wenn fit_distance_
# weights() nicht genug/zu entartete Daten fuer eine stabile Schaetzung hat.
# Ob gelernte Gewichte tatsaechlich genutzt werden, entscheidet zusaetzlich
# der zeitlich getrennte Rueckvergleich in select_model().
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


def forecast_weather_for_local_days(
    weather: list[WeatherPoint],
    start_date: date,
    days: int,
    timezone_name: str,
) -> list[WeatherPoint]:
    """Begrenzt UTC-Wetterstunden auf vollstaendige lokale Prognosetage.

    Open-Meteo liefert UTC-Kalendertage. Nach der Verschiebung auf das von
    den Strahlungswerten beschriebene Stundenintervall und der Umrechnung in
    die Anlagen-Zeitzone kann am Ende ein einzelner Wert in den Folgetag
    rutschen. Dieser unvollstaendige Randtag darf nicht als eigener Tag mit
    0 kWh im Dashboard erscheinen.
    """
    local_tz = ZoneInfo(timezone_name)
    end_date = start_date + timedelta(days=days)
    return [
        point
        for point in weather
        if start_date
        <= (point.timestamp - timedelta(hours=1)).astimezone(local_tz).date()
        < end_date
    ]


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
    _sample_distances() aus der Historie eines einzelnen Wechselrichters,
    statt sie (wie bisher in DEFAULT_DISTANCE_WEIGHTS) von Hand zu schaetzen.

    Vorgehen: Ridge-Regression (kleinste Quadrate + L2-Strafe, geschlossene
    Loesung ueber numpy - kein Gradientenabstieg/Backpropagation) auf
    standardisierten Merkmalen (siehe _forecast_feature_vector) sagt die
    beobachtete Leistung voraus; die Betrags-Koeffizienten zeigen dann, wie
    stark jedes Merkmal tatsaechlich mit der Leistung zusammenhaengt. Diese
    Wichtigkeiten werden auf dieselbe Gesamtsumme wie DEFAULT_DISTANCE_WEIGHTS
    skaliert, damit die uebrigen Konstanten der k-NN-Vorhersage (Nachbarnzahl,
    Distanz-Offset in predict_power) ihre bisherige Bedeutung behalten - nur
    die *relative* Gewichtung der Merkmale wird durch die Regression ersetzt.

    Der eigentliche Vorhersage-Mechanismus (Analogie-Suche + physikalisch
    begrenzte Strahlungs-Skalierung in predict_power) bleibt unveraendert.
    Reicht die Historie nicht (weniger als MIN_TRAINING_SAMPLES) oder ist ein
    Merkmal darin konstant (z.B. noch keine Temperaturstreuung) bzw. das
    Gleichungssystem trotz Regularisierung singulaer, wird auf die
    bisherigen Standardgewichte zurueckgefallen.
    """
    if len(training) < MIN_TRAINING_SAMPLES:
        return DEFAULT_DISTANCE_WEIGHTS

    rows = np.array([_forecast_feature_vector(sample.weather) for sample in training])
    targets = np.array([sample.power_w for sample in training])
    n_features = rows.shape[1]

    stds = rows.std(axis=0)
    if np.any(stds < 1e-9):
        return DEFAULT_DISTANCE_WEIGHTS
    standardized = (rows - rows.mean(axis=0)) / stds

    # Bias-Spalte fuer den Achsenabschnitt; bleibt unten unregularisiert.
    design = np.hstack([standardized, np.ones((len(training), 1))])
    penalty = np.eye(n_features + 1) * RIDGE_LAMBDA
    penalty[-1, -1] = 0.0

    try:
        coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ targets)
    except np.linalg.LinAlgError:
        return DEFAULT_DISTANCE_WEIGHTS

    hour_importance = float(np.hypot(coefficients[0], coefficients[1]))
    day_importance = float(np.hypot(coefficients[2], coefficients[3]))
    ghi_importance = float(abs(coefficients[4]))
    direct_importance = float(abs(coefficients[5]))
    diffuse_importance = float(abs(coefficients[6]))
    temperature_importance = float(abs(coefficients[7]))
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


@dataclass(frozen=True)
class _TrainingArrays:
    """training als numpy-Arrays statt als Liste von TrainingPoint-Objekten.

    predict_power() wird pro Wechselrichter bis zu 168x aufgerufen (7 Tage
    Stundenwerte); ohne diese Vorab-Umwandlung wuerde jeder Aufruf erneut
    ueber alle (ggf. bis zu TRAINING_DAYS*24) TrainingPoints iterieren und
    sortieren. Einmal mit _prepare_training_arrays() gebaut, laeuft die
    Distanzberechnung in predict_power() vektorisiert.
    """

    hours: np.ndarray
    days: np.ndarray
    ghi: np.ndarray
    direct: np.ndarray
    diffuse: np.ndarray
    temperature: np.ndarray
    power: np.ndarray


def _prepare_training_arrays(training: list[TrainingPoint]) -> _TrainingArrays:
    return _TrainingArrays(
        hours=np.array(
            [s.weather.timestamp.hour + s.weather.timestamp.minute / 60 for s in training]
        ),
        days=np.array([s.weather.timestamp.timetuple().tm_yday for s in training], dtype=float),
        ghi=np.array([s.weather.shortwave_w_m2 for s in training]),
        direct=np.array([s.weather.direct_w_m2 for s in training]),
        diffuse=np.array([s.weather.diffuse_w_m2 for s in training]),
        temperature=np.array([s.weather.temperature_c for s in training]),
        power=np.array([s.power_w for s in training]),
    )


def _cyclic_distance_array(values: np.ndarray, target: float, period: float) -> np.ndarray:
    difference = np.abs(values - target)
    return np.minimum(difference, period - difference)


def _sample_distances(
    arrays: _TrainingArrays, target: WeatherPoint, weights: DistanceWeights
) -> np.ndarray:
    """Gewichtete Distanz zwischen target und jedem Trainingspunkt in
    arrays, fuer alle Trainingspunkte gleichzeitig berechnet (siehe
    DistanceWeights/fit_distance_weights fuer die Herkunft der Gewichte).
    Jeder Term ist auf eine grobe, vergleichbare Groessenordnung skaliert
    (Divisoren 3/45/180/180/140/20), bevor er mit dem gelernten (oder
    Standard-)Gewicht multipliziert wird.
    """
    target_hour = target.timestamp.hour + target.timestamp.minute / 60
    target_day = target.timestamp.timetuple().tm_yday
    hour_distance = _cyclic_distance_array(arrays.hours, target_hour, 24) / 3
    day_distance = _cyclic_distance_array(arrays.days, target_day, 366) / 45
    ghi_distance = np.abs(arrays.ghi - target.shortwave_w_m2) / 180
    direct_distance = np.abs(arrays.direct - target.direct_w_m2) / 180
    diffuse_distance = np.abs(arrays.diffuse - target.diffuse_w_m2) / 140
    temperature_distance = np.abs(arrays.temperature - target.temperature_c) / 20
    return (
        weights.hour * hour_distance
        + weights.day * day_distance
        + weights.ghi * ghi_distance
        + weights.direct * direct_distance
        + weights.diffuse * diffuse_distance
        + weights.temperature * temperature_distance
    )


def predict_power(
    training: list[TrainingPoint],
    target: WeatherPoint,
    weights: DistanceWeights | None = None,
    arrays: _TrainingArrays | None = None,
) -> tuple[float, float, float]:
    """KNN-Prognose mit Strahlungsskalierung und empirischem Streubereich.

    weights bestimmt die relative Wichtigkeit der Merkmalsdimensionen in der
    Distanzmetrik (siehe fit_distance_weights). arrays ist dieselbe
    Trainingshistorie als numpy-Arrays (siehe _prepare_training_arrays).
    Beide sind per Default (None) aus training abgeleitet; wer wiederholt
    fuer denselben Trainingsdatensatz vorhersagt (siehe _summarize(), eine
    Vorhersage je Prognosestunde), sollte sie einmal vorab berechnen und
    explizit uebergeben, statt sie bei jedem Aufruf neu aufzubauen.
    """
    if target.shortwave_w_m2 < 3 or not training:
        return 0.0, 0.0, 0.0
    if weights is None:
        weights = fit_distance_weights(training)
    if arrays is None:
        arrays = _prepare_training_arrays(training)

    distances = _sample_distances(arrays, target, weights)
    k = min(24, len(distances))
    nearest_idx = np.argpartition(distances, k - 1)[:k]

    source_ghi = arrays.ghi[nearest_idx]
    valid = source_ghi >= 3
    if not np.any(valid):
        return 0.0, 0.0, 0.0
    nearest_idx = nearest_idx[valid]
    source_ghi = source_ghi[valid]

    observed_max = arrays.power.max()
    scale = np.clip(target.shortwave_w_m2 / source_ghi, 0.35, 2.75)
    estimates = np.minimum(observed_max * 1.08, arrays.power[nearest_idx] * scale)
    sample_weights = 1.0 / (0.15 + distances[nearest_idx])

    weight_sum = float(sample_weights.sum())
    expected = float((estimates * sample_weights).sum() / weight_sum)
    variance = float((sample_weights * (estimates - expected) ** 2).sum() / weight_sum)
    spread = math.sqrt(max(0.0, variance))
    return expected, max(0.0, expected - 1.28 * spread), expected + 1.28 * spread


def select_model(training: list[TrainingPoint]) -> ModelProfile:
    """Waehlt Standard- oder gelernte Gewichte anhand spaeterer Messwerte.

    Die letzten 20 Prozent der Historie bleiben beim Lernen unangetastet.
    Nur wenn die gelernten Gewichte dort einen kleineren absoluten Fehler
    erzielen, werden sie fuer die echte Zukunftsprognose verwendet.
    """
    fallback = ModelProfile(
        weights=DEFAULT_DISTANCE_WEIGHTS,
        method="standard",
        validation_samples=0,
        validation_error_percent=None,
        interval_error_fraction=None,
    )
    if len(training) < BACKTEST_MIN_SAMPLES:
        return fallback

    ordered = sorted(training, key=lambda sample: sample.weather.timestamp)
    split_at = max(MIN_TRAINING_SAMPLES, int(len(ordered) * 0.8))
    fit_samples = ordered[:split_at]
    validation = [
        sample
        for sample in ordered[split_at:]
        if sample.weather.shortwave_w_m2 >= 20 and sample.power_w >= 0
    ]
    if len(validation) < BACKTEST_MIN_DAYLIGHT_SAMPLES:
        return fallback

    arrays = _prepare_training_arrays(fit_samples)
    learned_weights = fit_distance_weights(fit_samples)

    def evaluate(weights: DistanceWeights) -> tuple[float, list[float], list[float]]:
        predictions = [
            predict_power(fit_samples, sample.weather, weights, arrays)[0]
            for sample in validation
        ]
        errors = [
            abs(predicted - sample.power_w)
            for predicted, sample in zip(predictions, validation)
        ]
        return sum(errors), predictions, errors

    default_error, default_predictions, default_errors = evaluate(
        DEFAULT_DISTANCE_WEIGHTS
    )
    learned_error, learned_predictions, learned_errors = evaluate(learned_weights)
    learned_wins = (
        learned_weights != DEFAULT_DISTANCE_WEIGHTS
        and learned_error < default_error * 0.99
    )
    method = "learned" if learned_wins else "standard"
    predictions = learned_predictions if learned_wins else default_predictions
    errors = learned_errors if learned_wins else default_errors
    actual_total = sum(sample.power_w for sample in validation)
    error_percent = 100 * sum(errors) / actual_total if actual_total > 0 else None
    relative_errors = [
        abs(predicted - sample.power_w) / max(sample.power_w, 100.0)
        for predicted, sample in zip(predictions, validation)
    ]
    interval_fraction = min(1.5, float(np.quantile(relative_errors, 0.8)))
    final_weights = (
        fit_distance_weights(ordered) if learned_wins else DEFAULT_DISTANCE_WEIGHTS
    )
    return ModelProfile(
        weights=final_weights,
        method=method,
        validation_samples=len(validation),
        validation_error_percent=error_percent,
        interval_error_fraction=interval_fraction,
    )


def _predict_with_profile(
    training: list[TrainingPoint],
    target: WeatherPoint,
    profile: ModelProfile,
    arrays: _TrainingArrays,
) -> tuple[float, float, float]:
    expected, local_low, local_high = predict_power(
        training, target, profile.weights, arrays
    )
    if expected <= 0 or profile.interval_error_fraction is None:
        return expected, local_low, local_high
    calibrated_spread = expected * profile.interval_error_fraction
    spread = max(expected - local_low, local_high - expected, calibrated_spread)
    return expected, max(0.0, expected - spread), expected + spread


def _aggregate_bounds(
    hourly_values: list[tuple[float, float, float]]
) -> tuple[float, float, float]:
    """Aggregiert stuendliche (expected, low, high)-Tripel (gleiche Einheit,
    z.B. W oder kW) ueber mehrere Stunden zu einem Gesamtwert (z.B. Tagessumme).

    expected wird schlicht aufsummiert. Fuer low/high wird NICHT die Summe
    der stuendlichen Extremwerte gebildet - das wuerde unterstellen, dass die
    Prognose in JEDER Stunde gleichzeitig maximal daneben liegt (voll
    korrelierter Fehler), was die Unsicherheit ueber laengere Zeitraeume stark
    ueberschaetzt und den Bereich unnoetig aufblaeht. Stattdessen werden die
    stuendlichen Halbbreiten (high - expected) quadratisch addiert - die
    ueblichen Fehlerfortpflanzung fuer die Summe naeherungsweise unabhaengiger
    Fehler (vgl. Zentraler Grenzwertsatz) - sodass sich Ausreisser einzelner
    Stunden im Tagesverlauf teilweise ausgleichen koennen, statt sich
    aufzusummieren."""
    total_expected = 0.0
    variance = 0.0
    for expected, _low, high in hourly_values:
        total_expected += expected
        half_width = high - expected
        variance += half_width * half_width
    spread = math.sqrt(variance)
    return total_expected, max(0.0, total_expected - spread), total_expected + spread


def _empty_result(message: str) -> dict:
    return {
        "available": False,
        "message": message,
        "generated_at": datetime.now(timezone.utc),
        "training_start": None,
        "training_end": None,
        "training_samples": 0,
        "weather_source": "Open-Meteo",
        "models": [],
        "days": [],
        "hours": [],
    }


def _summarize(
    training: dict[str, list[TrainingPoint]],
    forecast_weather: list[WeatherPoint],
    *,
    persist: bool = False,
    generated_at: datetime | None = None,
) -> dict:
    device_names = {device.id: device.name for device in settings.inverters}
    local_tz = ZoneInfo(settings.timezone_name)
    per_device_hour: dict[str, dict[datetime, tuple[float, float, float]]] = {}
    # Nur Geraete mit genuegend Historie fliessen in die Vorhersage UND in die
    # unten berichteten Trainings-Metadaten (training_samples/_start/_end)
    # ein - ein zu junges Geraet soll die gemeldete Datengrundlage nicht
    # verzerren.
    used_samples: dict[str, list[TrainingPoint]] = {}
    profiles: dict[str, ModelProfile] = {}
    for device_id, samples in training.items():
        if len(samples) < MIN_TRAINING_SAMPLES:
            continue
        used_samples[device_id] = samples
        profile = select_model(samples)
        profiles[device_id] = profile
        arrays = _prepare_training_arrays(samples)
        per_device_hour[device_id] = {
            (point.timestamp - timedelta(hours=1)): _predict_with_profile(
                samples, point, profile, arrays
            )
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
        per_device_values = {
            device_id: hours[interval_start]
            for device_id, hours in per_device_hour.items()
        }
        values = list(per_device_values.values())
        combined_hours.append(
            {
                "timestamp": interval_start,
                # Explizite Anlagen-Lokalzeit fuer Frontends: die Zuordnung
                # zu "heute" darf nicht von der Zeitzone des betrachtenden
                # Browsers abhaengen.
                "local_date": interval_start.astimezone(local_tz).date().isoformat(),
                "expected_kw": round(sum(value[0] for value in values) / 1000, 3),
                "low_kw": round(sum(value[1] for value in values) / 1000, 3),
                "high_kw": round(sum(value[2] for value in values) / 1000, 3),
                # Stundenwerte je Geraet - damit sich Diagramm/Kacheln im
                # Frontend auf ein einzelnes Geraet filtern lassen (Klick auf
                # den zugehoerigen Wechselrichter-Tab), statt immer nur die
                # ueber alle Geraete summierte Prognose zu zeigen.
                "devices": [
                    {
                        "device_id": device_id,
                        "device_name": device_names.get(device_id, device_id),
                        "expected_kw": round(value[0] / 1000, 3),
                        "low_kw": round(value[1] / 1000, 3),
                        "high_kw": round(value[2] / 1000, 3),
                    }
                    for device_id, value in per_device_values.items()
                ],
            }
        )

    hours_by_day: dict[str, list[dict]] = defaultdict(list)
    for hour in combined_hours:
        hours_by_day[hour["local_date"]].append(hour)

    days = []
    for date_key, day_hours in hours_by_day.items():
        devices = []
        for device_id, predictions in per_device_hour.items():
            device_hours_for_day = [
                (hour["timestamp"], predictions[hour["timestamp"]])
                for hour in day_hours
                if hour["timestamp"] in predictions
            ]
            device_values = [value for _timestamp, value in device_hours_for_day]
            device_expected, device_low, device_high = _aggregate_bounds(device_values)
            # Produktionsfenster/Spitze je Geraet - dieselbe Logik wie unten
            # fuer den kombinierten Tageswert (Schwelle 0.1 kW), nur auf die
            # Stunden DIESES Geraets beschraenkt statt auf die ueber alle
            # Geraete summierte Leistung.
            device_active = [
                (timestamp, value)
                for timestamp, value in device_hours_for_day
                if value[0] / 1000 >= 0.1
            ]
            device_peak = (
                max(device_hours_for_day, key=lambda item: item[1][0])
                if device_hours_for_day
                else None
            )
            devices.append(
                {
                    "device_id": device_id,
                    "device_name": device_names.get(device_id, device_id),
                    "expected_kwh": round(device_expected / 1000, 2),
                    "low_kwh": round(device_low / 1000, 2),
                    "high_kwh": round(device_high / 1000, 2),
                    "production_start": device_active[0][0] if device_active else None,
                    "production_end": (
                        device_active[-1][0] + timedelta(hours=1) if device_active else None
                    ),
                    "peak_at": (
                        device_peak[0] if device_peak and device_peak[1][0] > 0 else None
                    ),
                    "peak_kw": round(device_peak[1][0] / 1000, 3) if device_peak else 0.0,
                }
            )
        day_expected, day_low, day_high = _aggregate_bounds(
            [(hour["expected_kw"], hour["low_kw"], hour["high_kw"]) for hour in day_hours]
        )
        active = [hour for hour in day_hours if hour["expected_kw"] >= 0.1]
        peak = max(day_hours, key=lambda hour: hour["expected_kw"])
        days.append(
            {
                "date": date_key,
                "expected_kwh": round(day_expected, 2),
                "low_kwh": round(day_low, 2),
                "high_kwh": round(day_high, 2),
                "production_start": active[0]["timestamp"] if active else None,
                "production_end": active[-1]["timestamp"] + timedelta(hours=1) if active else None,
                "peak_at": peak["timestamp"] if peak["expected_kw"] > 0 else None,
                "peak_kw": peak["expected_kw"],
                "devices": devices,
            }
        )

    generated_at = generated_at or datetime.now(timezone.utc)
    if persist:
        from .forecast_evaluation import save_forecast_predictions

        save_forecast_predictions(
            per_device_hour,
            {device_id: profile.method for device_id, profile in profiles.items()},
            generated_at,
        )

    all_samples = [
        sample for samples in used_samples.values() for sample in samples
    ]
    return {
        "available": True,
        "message": "Prognose aus historischen PV- und Wetterdaten.",
        "generated_at": generated_at,
        "training_start": min(sample.weather.timestamp for sample in all_samples),
        "training_end": max(sample.weather.timestamp for sample in all_samples),
        "training_samples": len(all_samples),
        "weather_source": "Open-Meteo",
        "models": [
            {
                "device_id": device_id,
                "device_name": device_names.get(device_id, device_id),
                "method": profile.method,
                "validation_samples": profile.validation_samples,
                "validation_error_percent": (
                    round(profile.validation_error_percent, 1)
                    if profile.validation_error_percent is not None
                    else None
                ),
            }
            for device_id, profile in profiles.items()
        ],
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
        # Einen zusaetzlichen UTC-Tag abrufen, damit der letzte lokale Tag
        # auch in Zeitzonen mit UTC-Versatz vollstaendig vorliegt. Direkt
        # danach wird exakt auf FORECAST_DAYS lokale Kalendertage begrenzt.
        fetch_forecast_weather(latitude, longitude, FORECAST_DAYS + 1),
    )
    forecast_weather = forecast_weather_for_local_days(
        forecast_weather,
        now.astimezone(ZoneInfo(settings.timezone_name)).date(),
        FORECAST_DAYS,
        settings.timezone_name,
    )
    training = build_training_data(history, historical_weather)
    return _summarize(
        training,
        forecast_weather,
        persist=True,
        generated_at=datetime.now(timezone.utc),
    )


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
