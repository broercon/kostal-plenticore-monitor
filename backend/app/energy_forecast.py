"""Datengetriebene PV-Prognose aus Messhistorie und Open-Meteo-Strahlung."""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import case, delete, func, select

from .config import settings
from .database import SessionLocal
from .forecast_config import get_config
from .forecast_weather import WeatherPoint, WeatherServiceError, fetch_forecast_weather
from .models import HourlyPvCache, Reading
from .weather_cache import fetch_historical_weather_cached

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
    # Geschaetzte Modul-/Zelltemperatur (siehe _estimate_cell_temperature_c)
    # statt roher Windgeschwindigkeit - Wind wirkt physikalisch ueber die
    # Kuehlung der Module, nicht als eigenstaendiger Effekt. Defaults
    # entsprechen DEFAULT_DISTANCE_WEIGHTS weiter unten - vor allem, damit
    # bestehender Testcode, der DistanceWeights positionell mit nur den
    # urspruenglichen sechs Werten konstruiert, unveraendert weiterlaeuft.
    cell_temperature: float = 0.5
    # Teilweise redundant zu ghi/direct/diffuse (Bewoelkung ist deren
    # Ursache), kann aber z.B. Dunst von echtem Klarhimmel unterscheiden.
    cloud: float = 0.5
    humidity: float = 0.15
    # Ueberwiegend 0 (kein Schnee) - relevant vor allem als Signal fuer
    # evtl. schneebedeckte Module.
    snow_depth: float = 0.15
    pressure: float = 0.1


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
    hour=1.8,
    day=1.0,
    ghi=2.5,
    direct=1.0,
    diffuse=1.0,
    temperature=0.25,
    # Niedriger als die direkten Strahlungswerte angesetzt: die
    # Zelltemperatur verfeinert die Vorhersage (Temperaturderating), ersetzt
    # aber nicht die Haupttreiber ghi/direct/diffuse.
    cell_temperature=0.5,
    # Trotz Redundanz zu den Strahlungswerten ein moderates Gewicht, kein
    # sehr kleines - siehe DistanceWeights-Kommentar.
    cloud=0.5,
    # Nur feiner Nebeneffekt (Luftfeuchte/Luftdruck haben allenfalls sehr
    # indirekten Einfluss auf den PV-Ertrag).
    humidity=0.15,
    snow_depth=0.15,
    pressure=0.1,
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


def _raw_hourly_pv_average(
    since: datetime, until: datetime
) -> dict[str, dict[datetime, float]]:
    """Mittlere reine PV-Leistung je UTC-Stunde direkt per SQL-GROUP BY -
    der eigentliche (fuer einen groesseren Zeitraum teure) Rechenschritt
    hinter load_hourly_pv_history(), das die abgeschlossenen Stunden davon
    ueber hourly_pv_cache zwischenspeichert (siehe dort)."""
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


def invalidate_hourly_pv_cache(start_date: date, end_date: date) -> None:
    """Loescht gecachte Stundenwerte (siehe hourly_pv_cache/HourlyPvCache) im
    angegebenen Bereich lokaler Kalendertage (inklusive beider Enden) -
    aufgerufen nach einem Logdaten-Import, der rueckwirkend Messwerte fuer
    diese Tage ergaenzt/veraendert haben koennte. Die Grenzen muessen in die
    UTC-Zeitleiste der Cache-Buckets umgerechnet werden: Der Import liefert
    lokale Datumswerte, sodass eine Interpretation als UTC-Tag gerade an der
    ersten Tagesgrenze Stunden uebersehen wuerde. Analog zu
    daily_summary.invalidate_energy_cache, nur fuer die stuendliche PV-
    Historie statt der taeglichen Energie-Zeitraum-Uebersichten."""
    local_tz = ZoneInfo(settings.timezone_name)
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=local_tz).astimezone(
        timezone.utc
    )
    end_exclusive = datetime.combine(
        end_date + timedelta(days=1), datetime.min.time(), tzinfo=local_tz
    ).astimezone(timezone.utc)
    session = SessionLocal()
    try:
        session.execute(
            delete(HourlyPvCache).where(
                HourlyPvCache.hour_timestamp > start,
                HourlyPvCache.hour_timestamp <= end_exclusive,
            )
        )
        session.commit()
    finally:
        session.close()


def load_hourly_pv_history(
    since: datetime, until: datetime
) -> dict[str, dict[datetime, float]]:
    """Mittlere reine PV-Leistung je UTC-Stunde.

    Abgeschlossene (vollstaendig vergangene) Stunden werden ueber
    hourly_pv_cache zwischengespeichert (siehe Modell-Docstring in
    models.py): einmal berechnet, aendert sich der Wert einer
    abgeschlossenen Stunde nicht mehr (ausser ein nachtraeglicher
    Logdaten-Import ruft invalidate_hourly_pv_cache() auf). Nur die aktuell
    noch laufende Stunde sowie alles danach wird bei jedem Aufruf frisch
    berechnet - analog zu daily_summary._cached_daily_totals, nur auf
    Stundenebene statt Kalendertagen, weil /api/forecast/accuracy und
    /api/forecast/yesterday genau diese Granularitaet fuer den
    Prognose-vs-Ist-Vergleich brauchen und sonst bei jedem Dashboard-Reload
    erneut bis zu 30 Tage Rohmesswerte aggregieren wuerden."""
    now = datetime.now(timezone.utc)
    closed_until = min(until, now)
    last_closed_hour = closed_until.replace(minute=0, second=0, microsecond=0)

    result: dict[str, dict[datetime, float]] = defaultdict(dict)

    # Welche Stundenbuckets die Rohdaten-Abfrage im GESCHLOSSENEN Bereich
    # ueberhaupt erzeugen KOENNTE (siehe Kommentar in _raw_hourly_pv_average:
    # Bucket-Label = naechste volle Stunde NACH dem Messwert-Zeitstempel).
    expected_hours: list[datetime] = []
    if since < last_closed_hour:
        hour = since.replace(minute=0, second=0, microsecond=0)
        if hour <= since:
            hour += timedelta(hours=1)
        while hour <= last_closed_hour:
            expected_hours.append(hour)
            hour += timedelta(hours=1)

    cached_by_device: dict[str, dict[datetime, float | None]] = defaultdict(dict)
    if expected_hours:
        session = SessionLocal()
        try:
            rows = session.execute(
                select(
                    HourlyPvCache.device_id,
                    HourlyPvCache.hour_timestamp,
                    HourlyPvCache.avg_power_w,
                ).where(
                    HourlyPvCache.hour_timestamp >= expected_hours[0],
                    HourlyPvCache.hour_timestamp <= expected_hours[-1],
                )
            ).all()
        finally:
            session.close()
        for device_id, hour_timestamp, avg_power_w in rows:
            ts = (
                hour_timestamp
                if hour_timestamp.tzinfo is not None
                else hour_timestamp.replace(tzinfo=timezone.utc)
            )
            cached_by_device[device_id][ts] = avg_power_w

    # Aelteste erwartete Stunde OHNE JEDEN Cache-Eintrag (irgendeines
    # Geraets) - ab dort gilt alles Nachfolgende als "noch nicht gecacht"
    # und wird in einem Rutsch neu berechnet, statt Stunde fuer Stunde
    # einzeln (gleiche Praxis-Abwaegung wie bei _cached_daily_totals: nach
    # dem ersten Aufwaermen betrifft das ueblicherweise nur die juengste
    # neu abgeschlossene Stunde, ausser nach einem rueckwirkenden Import).
    gap_start: datetime | None = None
    for hour in expected_hours:
        if not any(hour in device_hours for device_hours in cached_by_device.values()):
            gap_start = hour - timedelta(hours=1)
            break

    for device_id, device_hours in cached_by_device.items():
        for hour, power_w in device_hours.items():
            if power_w is not None:
                result[device_id][hour] = power_w

    fresh_from: datetime | None = gap_start
    if last_closed_hour < until:
        fresh_from = last_closed_hour if fresh_from is None else min(fresh_from, last_closed_hour)
    if fresh_from is not None:
        fresh_from = max(fresh_from, since)

    if fresh_from is not None:
        fresh = _raw_hourly_pv_average(fresh_from, until)

        hours_to_persist = [h for h in expected_hours if gap_start is not None and h > gap_start]
        if hours_to_persist:
            all_device_ids = set(cached_by_device.keys()) | set(fresh.keys())
            now_write = datetime.now(timezone.utc)
            session = SessionLocal()
            try:
                for device_id in all_device_ids:
                    device_fresh = fresh.get(device_id, {})
                    for hour in hours_to_persist:
                        session.merge(
                            HourlyPvCache(
                                device_id=device_id,
                                hour_timestamp=hour,
                                avg_power_w=device_fresh.get(hour),
                                computed_at=now_write,
                            )
                        )
                session.commit()
            finally:
                session.close()

        for device_id, device_fresh in fresh.items():
            for hour, power_w in device_fresh.items():
                result[device_id][hour] = power_w

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


# Faiman-Modell (siehe z.B. pvlib.temperature.faiman) zur Schaetzung der
# Modul-/Zelltemperatur aus Lufttemperatur, Einstrahlung und Windgeschwin-
# digkeit: T_zelle = T_luft + G / (U0 + U1 * wind). U0/U1 sind die ueblichen
# Standardkoeffizienten fuer freistehend montierte Module (W/(m^2*K) bzw.
# (W/(m^2*K))/(m/s)) - eine Kalibrierung auf die tatsaechliche Montageart
# waere praeziser, ist hier aber bewusst nicht Ziel: es geht nur um ein
# zusaetzliches, physikalisch plausibles Merkmal fuer die Distanzmetrik,
# nicht um eine exakte Temperatursimulation.
_FAIMAN_U0 = 25.0
_FAIMAN_U1 = 6.84


def _estimate_cell_temperature_c(point: WeatherPoint) -> float:
    """Geschaetzte Modultemperatur statt roher Windgeschwindigkeit als
    Merkmal (siehe DistanceWeights.cell_temperature) - Wind wirkt auf den
    PV-Ertrag nicht direkt, sondern nur ueber die Kuehlung der Module, die
    wiederum deren Wirkungsgrad beeinflusst (waermere Zellen -> geringerer
    Wirkungsgrad). Diese Kombination aus Lufttemperatur, Einstrahlung und
    Wind bildet das deutlich praeziser ab als Windgeschwindigkeit oder
    Lufttemperatur je fuer sich alleine."""
    denominator = _FAIMAN_U0 + _FAIMAN_U1 * point.wind_speed_ms
    return point.temperature_c + point.shortwave_w_m2 / denominator


def _forecast_feature_vector(point: WeatherPoint) -> list[float]:
    """13 Merkmale je Messpunkt fuer fit_distance_weights(): Tageszeit und
    Jahrestag als Sinus/Kosinus (damit der Jahres-/Tageswechsel fuer die
    lineare Regression keinen kuenstlichen Sprung erzeugt), Strahlung und
    Temperatur direkt, sowie die zusaetzlichen Wetterwerte (Zelltemperatur,
    Bewoelkung, Luftfeuchte, Schneehoehe, Luftdruck - siehe DistanceWeights).
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
        _estimate_cell_temperature_c(point),
        point.cloud_cover_percent,
        point.humidity_percent,
        point.snow_depth_m,
        point.pressure_hpa,
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
    Reicht die Historie nicht (weniger als MIN_TRAINING_SAMPLES) oder ist das
    Gleichungssystem trotz Regularisierung singulaer, wird auf die
    bisherigen Standardgewichte zurueckgefallen.

    Einzelne (nahezu) konstante Merkmalsspalten - z.B. snow_depth_m bei einem
    Standort/Zeitraum ganz ohne Schnee, oder ein frisch um neue Wetterwerte
    erweiterter Cache mit noch kaum Streuung in humidity/pressure - werden
    NICHT wie frueher zum kompletten Verwerfen des Lernvorgangs fuehren:
    stattdessen fliessen nur die tatsaechlich streuenden Spalten in die
    Regression ein (sonst Division durch Std=0 beim Standardisieren), die
    konstanten Merkmale bekommen Wichtigkeit 0 (siehe Kommentar unten, warum
    das fuer die Distanzberechnung unschaedlich ist).
    """
    if len(training) < MIN_TRAINING_SAMPLES:
        return DEFAULT_DISTANCE_WEIGHTS

    rows = np.array([_forecast_feature_vector(sample.weather) for sample in training])
    targets = np.array([sample.power_w for sample in training])

    stds = rows.std(axis=0)
    variable_mask = stds >= 1e-9
    if not np.any(variable_mask):
        return DEFAULT_DISTANCE_WEIGHTS

    variable_rows = rows[:, variable_mask]
    standardized = (variable_rows - variable_rows.mean(axis=0)) / variable_rows.std(axis=0)

    # Bias-Spalte fuer den Achsenabschnitt; bleibt unten unregularisiert.
    design = np.hstack([standardized, np.ones((len(training), 1))])
    n_variable = int(variable_mask.sum())
    penalty = np.eye(n_variable + 1) * RIDGE_LAMBDA
    penalty[-1, -1] = 0.0

    try:
        solved = np.linalg.solve(design.T @ design + penalty, design.T @ targets)
    except np.linalg.LinAlgError:
        return DEFAULT_DISTANCE_WEIGHTS

    # Rohe (unskalierte) Wichtigkeit je der 13 _forecast_feature_vector()-
    # Spalten, ausserhalb der Regression ausgeschlossene (konstante) Spalten
    # bleiben bei 0.0 - siehe Docstring, warum das fuer eine Spalte ohne
    # jegliche Streuung in der Trainingshistorie die korrekte (weil einzig
    # belegbare) Aussage ist.
    raw_importance = np.zeros(rows.shape[1])
    raw_importance[variable_mask] = np.abs(solved[:-1])

    hour_importance = float(np.hypot(raw_importance[0], raw_importance[1]))
    day_importance = float(np.hypot(raw_importance[2], raw_importance[3]))
    ghi_importance = float(raw_importance[4])
    direct_importance = float(raw_importance[5])
    diffuse_importance = float(raw_importance[6])
    temperature_importance = float(raw_importance[7])
    cell_temperature_importance = float(raw_importance[8])
    cloud_importance = float(raw_importance[9])
    humidity_importance = float(raw_importance[10])
    snow_depth_importance = float(raw_importance[11])
    pressure_importance = float(raw_importance[12])
    total_importance = (
        hour_importance
        + day_importance
        + ghi_importance
        + direct_importance
        + diffuse_importance
        + temperature_importance
        + cell_temperature_importance
        + cloud_importance
        + humidity_importance
        + snow_depth_importance
        + pressure_importance
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
        + DEFAULT_DISTANCE_WEIGHTS.cell_temperature
        + DEFAULT_DISTANCE_WEIGHTS.cloud
        + DEFAULT_DISTANCE_WEIGHTS.humidity
        + DEFAULT_DISTANCE_WEIGHTS.snow_depth
        + DEFAULT_DISTANCE_WEIGHTS.pressure
    )
    scale = default_total / total_importance
    return DistanceWeights(
        hour=hour_importance * scale,
        day=day_importance * scale,
        ghi=ghi_importance * scale,
        direct=direct_importance * scale,
        diffuse=diffuse_importance * scale,
        temperature=temperature_importance * scale,
        cell_temperature=cell_temperature_importance * scale,
        cloud=cloud_importance * scale,
        humidity=humidity_importance * scale,
        snow_depth=snow_depth_importance * scale,
        pressure=pressure_importance * scale,
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
    cell_temperature: np.ndarray
    cloud: np.ndarray
    humidity: np.ndarray
    snow_depth: np.ndarray
    pressure: np.ndarray
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
        cell_temperature=np.array(
            [_estimate_cell_temperature_c(s.weather) for s in training]
        ),
        cloud=np.array([s.weather.cloud_cover_percent for s in training]),
        humidity=np.array([s.weather.humidity_percent for s in training]),
        snow_depth=np.array([s.weather.snow_depth_m for s in training]),
        pressure=np.array([s.weather.pressure_hpa for s in training]),
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
    # Divisoren fuer die fuenf zusaetzlichen Merkmale, nach demselben Prinzip
    # wie oben grob auf eine typische Schwankungsbreite skaliert (kein aus
    # Daten gelernter Wert, nur eine Groessenordnungs-Abschaetzung):
    # Zelltemperatur aehnlich der Lufttemperatur (evtl. etwas hoehere Spanne
    # durch Aufheizung), Bewoelkung/Luftfeuchte in vollen Prozentpunkten,
    # Schneehoehe in Metern (meist 0, schon wenige Zentimeter sind relevant),
    # Luftdruck in hPa (typische Tagesschwankung ca. 10-20 hPa).
    target_cell_temperature = _estimate_cell_temperature_c(target)
    cell_temperature_distance = np.abs(arrays.cell_temperature - target_cell_temperature) / 25
    cloud_distance = np.abs(arrays.cloud - target.cloud_cover_percent) / 40
    humidity_distance = np.abs(arrays.humidity - target.humidity_percent) / 30
    snow_depth_distance = np.abs(arrays.snow_depth - target.snow_depth_m) / 0.1
    pressure_distance = np.abs(arrays.pressure - target.pressure_hpa) / 15
    return (
        weights.hour * hour_distance
        + weights.day * day_distance
        + weights.ghi * ghi_distance
        + weights.direct * direct_distance
        + weights.diffuse * diffuse_distance
        + weights.temperature * temperature_distance
        + weights.cell_temperature * cell_temperature_distance
        + weights.cloud * cloud_distance
        + weights.humidity * humidity_distance
        + weights.snow_depth * snow_depth_distance
        + weights.pressure * pressure_distance
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
        "freeze_time": settings.forecast_freeze_time,
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

    generated_at = generated_at or datetime.now(timezone.utc)
    if persist:
        from .forecast_evaluation import load_frozen_predictions, save_forecast_predictions

        save_forecast_predictions(
            per_device_hour,
            {device_id: profile.method for device_id, profile in profiles.items()},
            generated_at,
        )
        # Fuer bereits eingefrorene Zielstunden (siehe FORECAST_FREEZE_TIME)
        # den gespeicherten (nicht mehr den frischen live berechneten) Wert
        # verwenden, BEVOR daraus combined_hours/days gebaut werden - so
        # spiegeln Dashboard-Tagesuebersicht, Mail-Report und "Stuendliche
        # Prognose heute" fuer einen eingefrorenen Tag exakt denselben Wert
        # wie die Prognosekontrolle (die dieselben gespeicherten Zeilen
        # liest), unabhaengig vom Abrufzeitpunkt.
        frozen = load_frozen_predictions(
            {device_id: set(hours) for device_id, hours in per_device_hour.items()}
        )
        for device_id, overrides in frozen.items():
            per_device_hour[device_id].update(overrides)

    combined_hours = []
    for point in forecast_weather:
        interval_start = point.timestamp - timedelta(hours=1)
        per_device_values = {
            device_id: hours[interval_start]
            for device_id, hours in per_device_hour.items()
        }
        values = list(per_device_values.values())
        local_start = interval_start.astimezone(local_tz)
        combined_hours.append(
            {
                "timestamp": interval_start,
                # Explizite Anlagen-Lokalzeit fuer Frontends: die Zuordnung
                # zu "heute" darf nicht von der Zeitzone des betrachtenden
                # Browsers abhaengen.
                "local_date": local_start.date().isoformat(),
                # Gleiches Bucket-Format wie aggregation.hourly_kwh_per_device,
                # damit das Frontend Prognose- und Ist-Werte je Stunde ohne
                # eigene Zeitzonen-Logik zusammenfuehren kann (siehe
                # Kommentar in schemas.ForecastHourOut.local_hour).
                "local_hour": local_start.replace(
                    minute=0, second=0, microsecond=0
                ).strftime("%Y-%m-%dT%H:%M:%S"),
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
        "freeze_time": settings.forecast_freeze_time,
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
        fetch_historical_weather_cached(
            latitude,
            longitude,
            earliest.date(),
            (until - timedelta(days=1)).date(),
            now=now,
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


async def refresh_forecast_for_new_day() -> dict:
    """Erzwingt eine sofortige Neuberechnung der Prognose, unabhaengig davon,
    wie frisch der aktuelle Cache-Eintrag noch ist (siehe ForecastService.get()/
    CACHE_TTL = 30 Minuten). Fuer main.py's taeglichen Mitternachts-Trigger
    (_refresh_forecast_at_midnight) gedacht: ohne invalidate() wuerde ein
    kurz vor Mitternacht erfolgreich gecachter Stand bis zu 30 Minuten in den
    neuen Tag hinein weiterverwendet, sodass "heute"/"morgen" im Dashboard
    (siehe frontend refreshForecast()) noch den Vortag zeigen wuerden - genau
    dieses Verhalten wurde nach einem Cache-Haenger im Betrieb beobachtet."""
    forecast_service.invalidate()
    return await forecast_service.get()
