"""Tests fuer den Speicherstand-Verlauf: aggregation.
aggregate_battery_soc_per_device()/build_battery_soc_series() sowie den
Endpoint /api/readings/battery-soc-history.

Zentraler Punkt: ein Ladezustand in Prozent darf beim Kombinieren mehrerer
Geraete ("Alle (Summe)" wie beim Leistungsverlauf) NICHT aufsummiert
werden - anders als Watt-Groessen in HISTORY_FIELDS/combine_devices
bekommt hier deshalb jedes Geraet mit Batterie seine eigene Kurve, und
Geraete ganz ohne SoC-Messwert im Zeitraum tauchen gar nicht erst auf.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.aggregation import aggregate_battery_soc_per_device, build_battery_soc_series
from app.models import Reading

from .conftest import make_user


def _r(device_id, minute, soc, device_name=None):
    return Reading(
        device_id=device_id,
        device_name=device_name or device_id,
        timestamp=datetime(2026, 7, 14, 10, minute, tzinfo=timezone.utc),
        battery_soc_percent=soc,
    )


def test_aggregate_battery_soc_per_device_averages_within_bucket():
    rows = [_r("wr1", 0, 40.0), _r("wr1", 5, 50.0)]
    per_device = aggregate_battery_soc_per_device(rows, bucket_seconds=900)
    bucket_key = int(datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc).timestamp())
    assert per_device["wr1"][bucket_key] == 45.0


def test_aggregate_battery_soc_per_device_ignores_null_values():
    rows = [_r("wr1", 0, None), _r("wr2", 0, 60.0)]
    per_device = aggregate_battery_soc_per_device(rows, bucket_seconds=900)
    assert "wr1" not in per_device
    assert list(per_device["wr2"].values()) == [60.0]


def test_build_battery_soc_series_skips_devices_without_any_soc_value():
    """Ein Wechselrichter ohne eigene Batterie meldet battery_soc_percent
    durchgehend als None - er darf im Ergebnis gar nicht erst als Geraet
    auftauchen (keine leere/flache Phantom-Kurve)."""
    rows = [_r("wr1", 0, 55.0), _r("wr2", 0, None)]
    result = build_battery_soc_series(rows, bucket_seconds=900)
    assert [d["device_id"] for d in result["devices"]] == ["wr1"]
    assert list(result["points"][0]["values"].keys()) == ["wr1"]


def test_build_battery_soc_series_two_devices_get_separate_curves_not_summed():
    """Kernanforderung: zwei Batterien bei 40 % und 30 % duerfen NICHT zu
    "70 %" kombiniert werden - jedes Geraet bleibt eine eigene Kurve."""
    rows = [_r("wr1", 0, 40.0), _r("wr2", 0, 30.0)]
    result = build_battery_soc_series(rows, bucket_seconds=900)
    assert {d["device_id"] for d in result["devices"]} == {"wr1", "wr2"}
    values = result["points"][0]["values"]
    assert values == {"wr1": 40.0, "wr2": 30.0}


def test_build_battery_soc_series_fills_gaps_with_none_per_device():
    """wr1 hat zu beiden Zeitpunkten Daten, wr2 nur beim zweiten - der erste
    Punkt muss fuer wr2 trotzdem einen (None-)Eintrag haben, damit das
    Frontend die Luecke sauber erkennen kann (Chart.js spanGaps)."""
    rows = [
        _r("wr1", 0, 50.0),
        _r("wr1", 20, 52.0),
        _r("wr2", 20, 33.0),
    ]
    result = build_battery_soc_series(rows, bucket_seconds=900)
    assert len(result["points"]) == 2
    first, second = result["points"]
    assert first["values"] == {"wr1": 50.0, "wr2": None}
    assert second["values"] == {"wr1": 52.0, "wr2": 33.0}


def test_build_battery_soc_series_without_any_data_is_empty():
    result = build_battery_soc_series([], bucket_seconds=900)
    assert result == {"devices": [], "points": []}


# --- Endpoint-Tests (ueber den vollen HTTP-Stack, siehe test_yearly_comparison.py) ---


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _seed(device_id: str, ts: datetime, soc: float | None) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.add(Reading(device_id=device_id, device_name=device_id, timestamp=ts, battery_soc_percent=soc))
        db.commit()
    finally:
        db.close()


def test_battery_soc_history_without_data_is_empty(client):
    _login(client)
    res = client.get("/api/readings/battery-soc-history")
    assert res.status_code == 200
    assert res.json() == {"devices": [], "points": []}


def test_battery_soc_history_returns_points_for_today(client, frozen_now):
    _login(client)
    _seed("wr1", frozen_now - timedelta(hours=1), 42.0)

    res = client.get("/api/readings/battery-soc-history")
    assert res.status_code == 200
    body = res.json()
    assert [d["device_id"] for d in body["devices"]] == ["wr1"]
    assert any(p["values"]["wr1"] == 42.0 for p in body["points"])


def test_battery_soc_history_default_hours_uses_local_midnight_not_rolling_window(client, frozen_now):
    """Wie /api/readings/history: bei hours<=24 gilt die feste lokale
    Tagesgrenze (seit Mitternacht), nicht ein rollierendes 24h-Fenster -
    ein Messwert von gestern darf im Standard-Zeitraum nicht auftauchen."""
    _login(client)
    _seed("wr1", frozen_now - timedelta(hours=25), 77.0)

    res = client.get("/api/readings/battery-soc-history")
    assert res.status_code == 200
    assert res.json() == {"devices": [], "points": []}


def test_battery_soc_history_hours_48_includes_yesterday(client, frozen_now):
    _login(client)
    _seed("wr1", frozen_now - timedelta(hours=25), 77.0)

    res = client.get("/api/readings/battery-soc-history?hours=48")
    assert res.status_code == 200
    body = res.json()
    assert body["devices"] == [{"device_id": "wr1", "device_name": "wr1"}]
    assert any(p["values"]["wr1"] == 77.0 for p in body["points"])


def test_battery_soc_history_hours_above_14_days_is_rejected(client):
    _login(client)
    res = client.get("/api/readings/battery-soc-history?hours=337")
    assert res.status_code == 422


def test_battery_soc_history_device_without_battery_is_absent(client, frozen_now):
    _login(client)
    _seed("wr1", frozen_now - timedelta(hours=1), 65.0)
    _seed("wr2", frozen_now - timedelta(hours=1), None)

    res = client.get("/api/readings/battery-soc-history")
    assert res.status_code == 200
    device_ids = {d["device_id"] for d in res.json()["devices"]}
    assert device_ids == {"wr1"}
