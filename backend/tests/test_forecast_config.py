"""Tests fuer die minimale Standortkonfiguration der PV-Prognose."""
from __future__ import annotations

from app.config import InverterConfig, settings
from app.forecast_config import get_config, update_config

from .conftest import make_user


def _admin_login(client) -> None:
    make_user("forecast-admin", "forecast-password", role="admin")
    response = client.post(
        "/api/auth/login",
        json={"username": "forecast-admin", "password": "forecast-password"},
    )
    assert response.status_code == 200


def test_coordinates_from_inverter_file_are_initial_defaults(client, monkeypatch):
    monkeypatch.setattr(
        settings,
        "inverters",
        [
            InverterConfig(
                id="wr-file",
                name="WR aus Datei",
                host="192.0.2.10",
                password="unused",
                latitude=50.1,
                longitude=8.6,
            )
        ],
    )
    assert get_config() == {
        "enabled": True,
        "latitude": 50.1,
        "longitude": 8.6,
        "source": "inverters.json",
    }


def test_file_defaults_do_not_mix_coordinates_from_different_inverters(
    client, monkeypatch
):
    monkeypatch.setattr(
        settings,
        "inverters",
        [
            InverterConfig(
                id="wr-latitude",
                name="Nur Breitengrad",
                host="192.0.2.10",
                password="unused",
                latitude=50.1,
            ),
            InverterConfig(
                id="wr-longitude",
                name="Nur Laengengrad",
                host="192.0.2.11",
                password="unused",
                longitude=8.6,
            ),
        ],
    )
    assert get_config() == {
        "enabled": False,
        "latitude": None,
        "longitude": None,
        "source": "inverters.json",
    }


def test_saved_coordinates_take_precedence(client):
    saved = update_config({"enabled": True, "latitude": 50.000, "longitude": 8.000})
    assert saved == {
        "enabled": True,
        "latitude": 50.000,
        "longitude": 8.000,
        "source": "database",
    }
    assert get_config() == saved


def test_admin_api_requires_admin(client):
    assert client.get("/api/admin/forecast/config").status_code == 401
    make_user("forecast-user", "valid-password", role="betreiber")
    client.post(
        "/api/auth/login",
        json={"username": "forecast-user", "password": "valid-password"},
    )
    assert client.get("/api/admin/forecast/config").status_code == 403


def test_admin_api_saves_only_coordinates(client):
    _admin_login(client)
    response = client.put(
        "/api/admin/forecast/config",
        json={"enabled": True, "latitude": 50.000, "longitude": 8.000},
    )
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "latitude": 50.000,
        "longitude": 8.000,
        "source": "database",
    }


def test_active_forecast_requires_coordinates(client):
    _admin_login(client)
    response = client.put(
        "/api/admin/forecast/config",
        json={"enabled": True, "latitude": None, "longitude": None},
    )
    assert response.status_code == 400
    assert "Breiten- und Laengengrad" in response.json()["detail"]
