"""Tests fuer das datengetriebene PV-Prognosemodell."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.energy_forecast import (
    MIN_TRAINING_SAMPLES,
    TrainingPoint,
    _summarize,
    build_training_data,
    load_hourly_pv_history,
    predict_power,
)
from app.database import SessionLocal
from app.forecast_weather import WeatherPoint
from app.models import Reading

from .conftest import make_user


def _weather(hour: int, radiation: float, *, day: int = 1) -> WeatherPoint:
    return WeatherPoint(
        timestamp=datetime(2026, 6, day, hour, tzinfo=timezone.utc),
        shortwave_w_m2=radiation,
        direct_w_m2=radiation * 0.7,
        diffuse_w_m2=radiation * 0.3,
        temperature_c=20.0,
    )


def test_training_data_joins_weather_and_pv_by_utc_hour():
    point = _weather(12, 700)
    history = {"wr1": {point.timestamp + timedelta(minutes=10): 4200.0}}
    result = build_training_data(history, [point])
    assert len(result["wr1"]) == 1
    assert result["wr1"][0].power_w == 4200.0


def test_hourly_history_uses_pure_pv_per_inverter(client):
    start = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        db.add_all(
            [
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=start + timedelta(minutes=minute),
                    pv_power_w=pv,
                    battery_power_w=battery,
                )
                for minute, pv, battery in [(0, 5000.0, 1000.0), (30, 3000.0, 0.0)]
            ]
        )
        db.commit()
    finally:
        db.close()

    result = load_hourly_pv_history(start, start + timedelta(hours=1))
    assert result["wr1"][start + timedelta(hours=1)] == 3500.0


def test_prediction_learns_output_without_technical_plant_data():
    training = [
        TrainingPoint(_weather(12, radiation, day=(index % 27) + 1), radiation * 6)
        for index, radiation in enumerate(range(300, 901, 20))
    ]
    expected, low, high = predict_power(training, _weather(12, 750))
    assert 4000 < expected < 5000
    assert 0 <= low <= expected <= high


def test_prediction_is_zero_at_night():
    training = [TrainingPoint(_weather(12, 700), 4000.0)]
    assert predict_power(training, _weather(1, 0)) == (0.0, 0.0, 0.0)


def test_summary_keeps_devices_separate_and_adds_total(monkeypatch):
    import app.energy_forecast as module

    class Device:
        def __init__(self, device_id, name):
            self.id = device_id
            self.name = name

    monkeypatch.setattr(
        module.settings,
        "inverters",
        [Device("wr1", "Dach"), Device("wr2", "Garage")],
    )
    training = {
        "wr1": [
            TrainingPoint(_weather(12, 600, day=(i % 27) + 1), 3000)
            for i in range(MIN_TRAINING_SAMPLES)
        ],
        "wr2": [
            TrainingPoint(_weather(12, 600, day=(i % 27) + 1), 1000)
            for i in range(MIN_TRAINING_SAMPLES)
        ],
    }
    result = _summarize(training, [_weather(12, 600)])
    assert result["available"] is True
    assert result["days"][0]["expected_kwh"] == 4.0
    assert [item["device_id"] for item in result["days"][0]["devices"]] == ["wr1", "wr2"]


def test_forecast_endpoint_requires_login_and_returns_service_result(client, monkeypatch):
    import app.main as main_module

    async def fake_get():
        return {
            "available": False,
            "message": "Test",
            "generated_at": datetime.now(timezone.utc),
            "training_start": None,
            "training_end": None,
            "training_samples": 0,
            "weather_source": "Open-Meteo",
            "days": [],
            "hours": [],
        }

    monkeypatch.setattr(main_module.forecast_service, "get", fake_get)
    assert client.get("/api/forecast").status_code == 401
    make_user("forecast-viewer", "valid-password", role="betreiber")
    client.post(
        "/api/auth/login",
        json={"username": "forecast-viewer", "password": "valid-password"},
    )
    response = client.get("/api/forecast")
    assert response.status_code == 200
    assert response.json()["message"] == "Test"
