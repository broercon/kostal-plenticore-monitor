"""Tests fuer das Parsen der Open-Meteo-Antwort (app.forecast_weather),
insbesondere fuer die zusaetzlichen Wetterwerte (Bewoelkung, Wind,
Luftfeuchte, Schneehoehe, Luftdruck), die seit der Erweiterung um
cloud_cover/wind_speed_10m/relative_humidity_2m/snow_depth/surface_pressure
zusaetzlich zu den urspruenglichen Strahlungswerten/Temperatur angefordert
werden (siehe HOURLY_VARIABLES)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.forecast_weather import WeatherServiceError, _parse_points


def _payload(**overrides):
    hourly = {
        "time": ["2026-06-01T12:00"],
        "shortwave_radiation": [500.0],
        "direct_radiation": [350.0],
        "diffuse_radiation": [150.0],
        "temperature_2m": [22.5],
        "cloud_cover": [40.0],
        "wind_speed_10m": [3.5],
        "relative_humidity_2m": [65.0],
        "snow_depth": [0.0],
        "surface_pressure": [1015.3],
    }
    hourly.update(overrides)
    return {"hourly": hourly}


def test_parse_points_reads_all_nine_hourly_variables():
    points = _parse_points(_payload())
    assert len(points) == 1
    point = points[0]
    assert point.timestamp == datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    assert point.shortwave_w_m2 == 500.0
    assert point.direct_w_m2 == 350.0
    assert point.diffuse_w_m2 == 150.0
    assert point.temperature_c == 22.5
    assert point.cloud_cover_percent == 40.0
    assert point.wind_speed_ms == 3.5
    assert point.humidity_percent == 65.0
    assert point.snow_depth_m == 0.0
    assert point.pressure_hpa == 1015.3


def test_parse_points_clamps_cloud_cover_and_humidity_to_0_100():
    # Open-Meteo liefert diese Werte normalerweise schon im 0-100-Bereich,
    # aber Rundungs-/Modellartefakte ausserhalb davon sollen nicht zu einer
    # aus dem Rahmen fallenden Distanzberechnung fuehren (siehe
    # energy_forecast._sample_distances).
    points = _parse_points(_payload(cloud_cover=[104.0], relative_humidity_2m=[-3.0]))
    assert points[0].cloud_cover_percent == 100.0
    assert points[0].humidity_percent == 0.0


def test_parse_points_clamps_wind_and_snow_depth_to_zero_or_more():
    points = _parse_points(_payload(wind_speed_10m=[-1.0], snow_depth=[-0.01]))
    assert points[0].wind_speed_ms == 0.0
    assert points[0].snow_depth_m == 0.0


def test_parse_points_skips_hours_with_any_missing_value():
    points = _parse_points(_payload(cloud_cover=[None]))
    assert points == []


def test_parse_points_raises_on_mismatched_variable_lengths():
    try:
        _parse_points(_payload(surface_pressure=[1015.3, 1016.0]))
    except WeatherServiceError:
        return
    raise AssertionError("WeatherServiceError erwartet bei ungleich langen Variablen")
