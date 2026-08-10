"""Tests fuer die im Admin-Bereich editierbare PV-Prognosekonfiguration."""
from __future__ import annotations

from app.config import InverterConfig, PVArrayConfig, settings
from app.forecast_config import get_config, update_config

from .conftest import make_user


def _admin_login(client) -> None:
    make_user("forecast-admin", "forecast-pw", role="admin")
    response = client.post(
        "/api/auth/login",
        json={"username": "forecast-admin", "password": "forecast-pw"},
    )
    assert response.status_code == 200


def _payload() -> dict:
    return {
        "enabled": True,
        "location_name": "Beispielstandort",
        "latitude": 50.000,
        "longitude": 8.000,
        "forecast_days": 7,
        "system_loss_percent": 13.5,
        "arrays": [
            {
                "device_id": "wr1",
                "name": "Dach Sued",
                "module_count": 20,
                "module_power_wp": 430.0,
                "peak_power_kwp": None,
                "tilt_degrees": 35.0,
                "azimuth_degrees": 0.0,
                "inverter_limit_kw": 8.0,
                "enabled": True,
            }
        ],
    }


def test_file_values_are_defaults_until_admin_saves(client, monkeypatch):
    inverter = InverterConfig(
        id="wr-file",
        name="WR aus Datei",
        host="192.0.2.10",
        password="unused",
        latitude=50.1,
        longitude=8.6,
        location_name="Datei-Standort",
        pv_arrays=[
            PVArrayConfig(
                name="Ostdach",
                module_count=10,
                module_power_wp=400,
                tilt_degrees=25,
                azimuth_degrees=-90,
            )
        ],
    )
    monkeypatch.setattr(settings, "inverters", [inverter])

    config = get_config()
    assert config["source"] == "inverters.json"
    assert config["location_name"] == "Datei-Standort"
    assert config["arrays"][0]["device_id"] == "wr-file"
    assert config["arrays"][0]["effective_peak_power_kwp"] == 4.0


def test_saved_config_keeps_arrays_separate_per_inverter(client, monkeypatch):
    monkeypatch.setattr(
        settings,
        "inverters",
        [
            InverterConfig("wr1", "WR 1", "192.0.2.1", "unused"),
            InverterConfig("wr2", "WR 2", "192.0.2.2", "unused"),
        ],
    )
    payload = _payload()
    payload["arrays"].append(
        {
            "device_id": "wr2",
            "name": "Westdach",
            "module_count": None,
            "module_power_wp": None,
            "peak_power_kwp": 5.25,
            "tilt_degrees": 20.0,
            "azimuth_degrees": 90.0,
            "inverter_limit_kw": None,
            "enabled": True,
        }
    )

    saved = update_config(payload)
    assert saved["source"] == "database"
    assert [(a["device_id"], a["effective_peak_power_kwp"]) for a in saved["arrays"]] == [
        ("wr1", 8.6),
        ("wr2", 5.25),
    ]
    assert get_config() == saved


def test_admin_api_requires_admin(client):
    assert client.get("/api/admin/forecast/config").status_code == 401
    make_user("forecast-user", "pw", role="betreiber")
    client.post("/api/auth/login", json={"username": "forecast-user", "password": "pw"})
    assert client.get("/api/admin/forecast/config").status_code == 403


def test_admin_api_saves_config_and_calculates_effective_kwp(client):
    _admin_login(client)
    response = client.put("/api/admin/forecast/config", json=_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "database"
    assert body["arrays"][0]["device_id"] == "wr1"
    assert body["arrays"][0]["effective_peak_power_kwp"] == 8.6


def test_admin_api_rejects_unknown_device(client):
    _admin_login(client)
    payload = _payload()
    payload["arrays"][0]["device_id"] = "does-not-exist"
    response = client.put("/api/admin/forecast/config", json=payload)
    assert response.status_code == 400
    assert "Unbekannter Wechselrichter" in response.json()["detail"]


def test_admin_api_rejects_array_without_power(client):
    _admin_login(client)
    payload = _payload()
    payload["arrays"][0]["module_count"] = None
    payload["arrays"][0]["module_power_wp"] = None
    response = client.put("/api/admin/forecast/config", json=payload)
    assert response.status_code == 400
    assert "kWp oder Modulanzahl" in response.json()["detail"]
