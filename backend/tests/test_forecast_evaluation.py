"""Tests fuer Speicherung und Ist-Vergleich von PV-Prognosen."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.forecast_evaluation import get_forecast_accuracy, save_forecast_predictions
from app.models import ForecastPrediction, Reading

from .conftest import make_user


def test_prediction_is_updated_only_until_target_hour_starts(client):
    target = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    first_generated = target - timedelta(hours=2)
    save_forecast_predictions(
        {"wr1": {target: (3000.0, 2500.0, 3500.0)}},
        {"wr1": "standard"},
        first_generated,
    )
    save_forecast_predictions(
        {"wr1": {target: (3200.0, 2700.0, 3700.0)}},
        {"wr1": "learned"},
        target - timedelta(minutes=30),
    )
    save_forecast_predictions(
        {"wr1": {target: (9999.0, 9999.0, 9999.0)}},
        {"wr1": "learned"},
        target,
    )

    session = SessionLocal()
    try:
        rows = session.scalars(select(ForecastPrediction)).all()
        assert len(rows) == 1
        assert rows[0].expected_w == 3200.0
        assert rows[0].model_method == "learned"
        assert rows[0].first_generated_at == first_generated.replace(tzinfo=None)
    finally:
        session.close()


def test_accuracy_compares_each_inverter_and_total(client):
    target = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    generated_at = target - timedelta(days=1)
    save_forecast_predictions(
        {
            "wr1": {target: (3000.0, 2000.0, 4000.0)},
            "wr2": {target: (1000.0, 500.0, 1500.0)},
        },
        {"wr1": "standard", "wr2": "learned"},
        generated_at,
    )
    session = SessionLocal()
    try:
        session.add_all(
            [
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=target + timedelta(minutes=30),
                    pv_power_w=4000.0,
                    battery_power_w=0.0,
                ),
                Reading(
                    device_id="wr2",
                    device_name="WR 2",
                    timestamp=target + timedelta(minutes=30),
                    pv_power_w=1500.0,
                    battery_power_w=0.0,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    result = get_forecast_accuracy(
        days=2, now=datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    )
    assert result["available"] is True
    assert result["days"][0]["expected_kwh"] == 4.0
    assert result["days"][0]["actual_kwh"] == 5.5
    assert result["days"][0]["difference_kwh"] == 1.5
    assert {item["device_id"] for item in result["days"][0]["devices"]} == {
        "wr1",
        "wr2",
    }


def test_accuracy_endpoint_requires_login(client):
    assert client.get("/api/forecast/accuracy").status_code == 401
    make_user("accuracy-viewer", "valid-password", role="betreiber")
    client.post(
        "/api/auth/login",
        json={"username": "accuracy-viewer", "password": "valid-password"},
    )
    response = client.get("/api/forecast/accuracy")
    assert response.status_code == 200
    assert response.json()["available"] is False
