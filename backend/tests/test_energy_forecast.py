"""Tests fuer das datengetriebene PV-Prognosemodell."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.aggregation import pure_pv_power_w
from app.energy_forecast import (
    DEFAULT_DISTANCE_WEIGHTS,
    MIN_TRAINING_SAMPLES,
    TrainingPoint,
    _summarize,
    build_training_data,
    fit_distance_weights,
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


def test_pure_pv_sql_matches_python_helper(client):
    """load_hourly_pv_history() bildet die reine PV-Leistung als SQL-
    Ausdruck; aggregation.pure_pv_power_w() ist dieselbe Formel in Python
    (fuer die Tages-kWh-Integration). Dieser Test stellt sicher, dass beide
    Implementierungen fuer dieselben Rohdaten identische Werte liefern, auch
    wenn sich die Formel irgendwann aendert.
    """
    start = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    samples = [
        (0, 5000.0, 1000.0),
        (15, 3000.0, None),
        (30, 800.0, -200.0),
        (45, 0.0, 0.0),
    ]
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
                for minute, pv, battery in samples
            ]
        )
        db.commit()
    finally:
        db.close()

    sql_result = load_hourly_pv_history(start, start + timedelta(hours=1))
    expected = sum(pure_pv_power_w(pv, battery) for _, pv, battery in samples) / len(
        samples
    )
    assert sql_result["wr1"][start + timedelta(hours=1)] == round(expected, 6)


def test_summary_excludes_immature_devices_from_training_metadata(monkeypatch):
    """Ein Geraet mit zu wenig Historie darf die berichtete Datengrundlage
    (training_samples/_start/_end) nicht verzerren, auch wenn es aus der
    eigentlichen Vorhersage bereits ausgeschlossen ist.
    """
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
    mature_samples = [
        TrainingPoint(_weather(12, 600, day=(i % 27) + 1), 3000)
        for i in range(MIN_TRAINING_SAMPLES)
    ]
    immature_samples = [TrainingPoint(_weather(12, 600, day=1), 1000)]
    training = {"wr1": mature_samples, "wr2": immature_samples}

    result = _summarize(training, [_weather(12, 600)])

    assert result["available"] is True
    assert [item["device_id"] for item in result["days"][0]["devices"]] == ["wr1"]
    assert result["training_samples"] == len(mature_samples)


def test_fit_distance_weights_falls_back_for_too_few_samples():
    too_few = [TrainingPoint(_weather(12, 500), 3000.0) for _ in range(5)]
    assert fit_distance_weights(too_few) == DEFAULT_DISTANCE_WEIGHTS


def test_fit_distance_weights_falls_back_for_constant_feature():
    # _weather() nutzt fuer alle Samples dieselbe Stunde (12) -> die
    # Stunden-Merkmale (sin/cos) haben keine Streuung, Standardisierung waere
    # instabil - erwarteter Fallback auf die Standardgewichte.
    samples = [
        TrainingPoint(_weather(12, 500, day=(i % 27) + 1), 3000.0)
        for i in range(MIN_TRAINING_SAMPLES)
    ]
    assert fit_distance_weights(samples) == DEFAULT_DISTANCE_WEIGHTS


def test_fit_distance_weights_prioritizes_the_feature_that_predicts_power():
    """Leistung haengt in diesen synthetischen Daten NUR von der Strahlung
    ab; Stunde, Jahrestag und Temperatur variieren unabhaengig davon. Die
    gelernten Gewichte sollen das widerspiegeln: die drei Strahlungs-Merkmale
    (GHI/Direkt/Diffus) sollen zusammen deutlich staerker gewichtet werden
    als Stunde, Tag oder Temperatur einzeln.
    """
    rng = random.Random(42)
    samples = []
    for _ in range(80):
        radiation = rng.uniform(50, 900)
        hour = rng.uniform(5, 19)
        day_offset = rng.randint(0, 300)
        temperature = rng.uniform(-10, 35)
        weather = WeatherPoint(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
            + timedelta(days=day_offset, hours=hour),
            shortwave_w_m2=radiation,
            direct_w_m2=radiation * 0.7 + rng.uniform(-5, 5),
            diffuse_w_m2=radiation * 0.3 + rng.uniform(-5, 5),
            temperature_c=temperature,
        )
        samples.append(TrainingPoint(weather, radiation * 5.0))

    weights = fit_distance_weights(samples)
    radiation_weight = weights.ghi + weights.direct + weights.diffuse
    assert radiation_weight > weights.hour * 3
    assert radiation_weight > weights.day * 3
    assert radiation_weight > weights.temperature * 3


def test_summarize_uses_learned_weights_without_crashing():
    """End-to-End-Check: _summarize() mit realistisch variierenden Daten
    (nicht die entarteten Konstanten aus den anderen Tests) laeuft ueber den
    Ridge-Pfad, ohne abzustuerzen, und liefert weiterhin plausible Werte.
    """
    rng = random.Random(7)
    samples = []
    for _ in range(80):
        radiation = rng.uniform(50, 900)
        hour = rng.uniform(5, 19)
        day_offset = rng.randint(0, 300)
        weather = WeatherPoint(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
            + timedelta(days=day_offset, hours=hour),
            shortwave_w_m2=radiation,
            direct_w_m2=radiation * 0.7 + rng.uniform(-5, 5),
            diffuse_w_m2=radiation * 0.3 + rng.uniform(-5, 5),
            temperature_c=rng.uniform(-10, 35),
        )
        samples.append(TrainingPoint(weather, max(0.0, radiation * 5.0 + rng.uniform(-200, 200))))

    forecast_point = _weather(12, 700)
    result = _summarize({"wr1": samples}, [forecast_point])
    assert result["available"] is True
    assert result["days"][0]["expected_kwh"] > 0
