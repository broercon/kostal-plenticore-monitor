"""Tests fuer das datengetriebene PV-Prognosemodell."""
from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.aggregation import pure_pv_power_w
from app.energy_forecast import (
    BACKTEST_MIN_SAMPLES,
    DEFAULT_DISTANCE_WEIGHTS,
    DistanceWeights,
    MIN_TRAINING_SAMPLES,
    ModelProfile,
    TrainingPoint,
    _aggregate_bounds,
    _empty_result,
    _predict_with_profile,
    _prepare_training_arrays,
    _summarize,
    build_training_data,
    fit_distance_weights,
    forecast_weather_for_local_days,
    invalidate_hourly_pv_cache,
    load_hourly_pv_history,
    predict_power,
    refresh_forecast_for_new_day,
    select_model,
)
from app.database import SessionLocal
from app.forecast_weather import WeatherPoint
from app.models import HourlyPvCache, Reading

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


def test_forecast_uses_exactly_seven_local_days_without_zero_edge_day():
    start = datetime(2026, 8, 11, tzinfo=timezone.utc)
    weather = [
        WeatherPoint(
            timestamp=start + timedelta(hours=index),
            shortwave_w_m2=500.0,
            direct_w_m2=350.0,
            diffuse_w_m2=150.0,
            temperature_c=20.0,
        )
        for index in range(8 * 24)
    ]

    result = forecast_weather_for_local_days(
        weather, start.date(), 7, "Europe/Berlin"
    )
    local_dates = {
        (point.timestamp - timedelta(hours=1))
        .astimezone(ZoneInfo("Europe/Berlin"))
        .date()
        for point in result
    }
    assert local_dates == {start.date() + timedelta(days=index) for index in range(7)}
    assert len(
        [
            point
            for point in result
            if (point.timestamp - timedelta(hours=1))
            .astimezone(ZoneInfo("Europe/Berlin"))
            .date()
            == start.date() + timedelta(days=6)
        ]
    ) == 24


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


def test_hourly_history_caches_closed_hours_and_ignores_later_raw_changes(client):
    """Eine ABGESCHLOSSENE Stunde (weit in der Vergangenheit) wird beim
    ersten Aufruf in hourly_pv_cache abgelegt (siehe HourlyPvCache) und bei
    jedem weiteren Aufruf aus dem Cache bedient - eine nachtraegliche
    Aenderung an den Rohmesswerten OHNE expliziten invalidate_hourly_pv_cache()
    -Aufruf darf sich deshalb (bewusst) nicht mehr auswirken."""
    start = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        db.add(
            Reading(
                device_id="wr1",
                device_name="WR 1",
                timestamp=start + timedelta(minutes=10),
                pv_power_w=4000.0,
                battery_power_w=0.0,
            )
        )
        db.commit()
    finally:
        db.close()

    target_hour = start + timedelta(hours=1)
    result = load_hourly_pv_history(start, target_hour)
    assert result["wr1"][target_hour] == 4000.0

    db = SessionLocal()
    try:
        cached = db.get(HourlyPvCache, ("wr1", target_hour))
        assert cached is not None
        assert cached.avg_power_w == 4000.0
    finally:
        db.close()

    db = SessionLocal()
    try:
        row = db.query(Reading).filter_by(device_id="wr1").one()
        row.pv_power_w = 9999.0
        db.commit()
    finally:
        db.close()

    result_again = load_hourly_pv_history(start, target_hour)
    assert result_again["wr1"][target_hour] == 4000.0


def test_invalidate_hourly_pv_cache_forces_recompute(client):
    start = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        db.add(
            Reading(
                device_id="wr1",
                device_name="WR 1",
                timestamp=start + timedelta(minutes=10),
                pv_power_w=4000.0,
                battery_power_w=0.0,
            )
        )
        db.commit()
    finally:
        db.close()

    target_hour = start + timedelta(hours=1)
    load_hourly_pv_history(start, target_hour)

    db = SessionLocal()
    try:
        row = db.query(Reading).filter_by(device_id="wr1").one()
        row.pv_power_w = 9999.0
        db.commit()
    finally:
        db.close()

    invalidate_hourly_pv_cache(start.date(), start.date())

    result = load_hourly_pv_history(start, target_hour)
    assert result["wr1"][target_hour] == 9999.0


def test_invalidate_hourly_pv_cache_uses_local_day_boundaries(client):
    """Ein lokaler Importtag beginnt in Europe/Berlin bereits am Vorabend
    in UTC. Auch dessen erste Cache-Stunde muss invalidiert werden."""
    local_day = date(2026, 6, 1)
    first_local_hour_end_utc = datetime(2026, 5, 31, 23, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        db.add(
            HourlyPvCache(
                device_id="wr1",
                hour_timestamp=first_local_hour_end_utc,
                avg_power_w=4000.0,
                computed_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()

    invalidate_hourly_pv_cache(local_day, local_day)

    db = SessionLocal()
    try:
        assert db.get(HourlyPvCache, ("wr1", first_local_hour_end_utc)) is None
    finally:
        db.close()


def test_hourly_history_does_not_cache_the_still_running_hour(client, monkeypatch):
    """Die aktuell noch laufende Stunde (und alles danach) darf NIE in
    hourly_pv_cache landen - ihr Wert kann sich noch aendern, solange
    weitere Messwerte innerhalb dieser Stunde eintreffen."""
    import app.energy_forecast as energy_forecast_module

    frozen_now = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_now.replace(tzinfo=None)
            return frozen_now.astimezone(tz)

    monkeypatch.setattr(energy_forecast_module, "datetime", _FrozenDatetime)

    start = frozen_now.replace(minute=0, second=0, microsecond=0)
    db = SessionLocal()
    try:
        db.add(
            Reading(
                device_id="wr1",
                device_name="WR 1",
                timestamp=start + timedelta(minutes=5),
                pv_power_w=4000.0,
                battery_power_w=0.0,
            )
        )
        db.commit()
    finally:
        db.close()

    result = load_hourly_pv_history(start, start + timedelta(hours=1))
    # Die laufende Stunde (12:00-13:00, Bucket-Label 13:00) hat schon einen
    # Teil-Messwert und wird deshalb ganz normal im Rueckgabewert geliefert -
    # nur eben NICHT dauerhaft gecacht (naechster Test-Teil).
    assert result["wr1"][start + timedelta(hours=1)] == 4000.0

    db = SessionLocal()
    try:
        assert db.get(HourlyPvCache, ("wr1", start + timedelta(hours=1))) is None
    finally:
        db.close()


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


# --- _aggregate_bounds ------------------------------------------------------


def test_aggregate_bounds_combines_independent_spreads_not_naive_sum():
    """Zwei Stunden mit je +/-2 Halbbreite sollen sich teilweise ausgleichen
    (sqrt(2^2+2^2) ~= 2.83), statt sich zur naiven Summe von 4.0 zu addieren -
    genau das ist der Fix fuer den zu breiten Tages-Spannbereich."""
    expected, low, high = _aggregate_bounds([(10.0, 8.0, 12.0), (10.0, 8.0, 12.0)])
    assert expected == 20.0
    combined_half_width = 8 ** 0.5  # sqrt(2^2 + 2^2)
    assert round(low, 4) == round(20.0 - combined_half_width, 4)
    assert round(high, 4) == round(20.0 + combined_half_width, 4)
    assert high < 24.0  # naive Summe der Extremwerte waere 10+12+10+12-20 = 24
    assert low > 16.0


def test_aggregate_bounds_clips_low_at_zero():
    _, low, _ = _aggregate_bounds([(1.0, -5.0, 7.0)])
    assert low == 0.0


def test_summarize_day_bounds_combine_hourly_spreads_instead_of_summing_naively(
    monkeypatch,
):
    """Regression fuer den Mail-Report/Dashboard: der Tages-Spannbereich
    darf nicht mehr die Summe der stuendlichen Extremwerte sein (das
    unterstellt, dass jede Stunde gleichzeitig maximal daneben liegt),
    sondern muss die stuendlichen Halbbreiten quadratisch kombinieren."""
    import app.energy_forecast as module

    class Device:
        def __init__(self, device_id, name):
            self.id = device_id
            self.name = name

    monkeypatch.setattr(module.settings, "inverters", [Device("wr1", "Dach")])
    monkeypatch.setattr(
        module,
        "_predict_with_profile",
        lambda training, target, profile, arrays: (1000.0, 800.0, 1200.0),
    )

    training = {
        "wr1": [
            TrainingPoint(_weather(12, 600, day=(i % 27) + 1), 3000)
            for i in range(MIN_TRAINING_SAMPLES)
        ]
    }
    forecast_weather = [_weather(11, 600), _weather(12, 600)]  # zwei Stunden, ein Tag

    result = module._summarize(training, forecast_weather)
    day = result["days"][0]

    assert day["expected_kwh"] == 2.0  # 1.0 + 1.0 kWh, unveraendert
    naive_sum_high_kwh = 2.4  # 2 * 1.2 kWh - so war es VOR dem Fix
    naive_sum_low_kwh = 1.6  # 2 * 0.8 kWh - so war es VOR dem Fix
    assert day["high_kwh"] < naive_sum_high_kwh
    assert day["low_kwh"] > naive_sum_low_kwh
    assert day["high_kwh"] == 2.28
    assert day["low_kwh"] == 1.72
    # dieselbe Korrektur greift auch fuer die Pro-Geraet-Aufschluesselung
    assert day["devices"][0]["high_kwh"] == 2.28
    assert day["devices"][0]["low_kwh"] == 1.72


def test_summarize_uses_frozen_prediction_instead_of_fresh_live_value(client, monkeypatch):
    """Regression fuer die Prognose-Festschreibung (FORECAST_FREEZE_TIME):
    sobald eine Zielstunde bereits eingefroren ist (siehe
    forecast_evaluation.save_forecast_predictions/load_frozen_predictions),
    muss _summarize() den gespeicherten Wert verwenden - NICHT den frischen,
    live vom Modell berechneten - auch wenn diese Stunde selbst noch nicht
    begonnen hat. Das ist die Grundlage dafuer, dass Mail-Report,
    Dashboard-Tagesuebersicht und Prognosekontrolle fuer einen bereits
    eingefrorenen Tag denselben Wert zeigen."""
    import app.energy_forecast as module
    from app.forecast_evaluation import save_forecast_predictions

    class Device:
        def __init__(self, device_id, name):
            self.id = device_id
            self.name = name

    monkeypatch.setattr(module.settings, "inverters", [Device("wr1", "Dach")])
    # Das Modell wuerde JETZT live einen ANDEREN Wert liefern als das, was
    # "gestern Abend" nach der Einfrier-Grenze bereits gespeichert wurde.
    monkeypatch.setattr(
        module,
        "_predict_with_profile",
        lambda training, target, profile, arrays: (5000.0, 4000.0, 6000.0),
    )

    # Zielstunde 2.6.2026, 12-13 Uhr UTC (14-15 Uhr Berlin). Die
    # Einfrier-Grenze fuer diesen Tag ist der 1.6., 22 Uhr Berlin = 20 Uhr
    # UTC. Der folgende Lauf liegt DANACH und friert die Stunde damit ein.
    target = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    save_forecast_predictions(
        {"wr1": {target: (1234.0, 1000.0, 1500.0)}},
        {"wr1": "standard"},
        datetime(2026, 6, 1, 21, tzinfo=timezone.utc),
    )

    training = {
        "wr1": [
            TrainingPoint(_weather(12, 600, day=(i % 27) + 1), 3000)
            for i in range(MIN_TRAINING_SAMPLES)
        ]
    }
    forecast_weather = [_weather(13, 600, day=2)]  # deckt genau "target" ab

    # Neuer Lauf "heute Morgen", also nach der Einfrier-Grenze, aber noch
    # deutlich vor Beginn der Zielstunde selbst.
    result = module._summarize(
        training,
        forecast_weather,
        persist=True,
        generated_at=datetime(2026, 6, 2, 6, tzinfo=timezone.utc),
    )

    assert result["hours"][0]["expected_kw"] == 1.234
    assert result["hours"][0]["devices"][0]["expected_kw"] == 1.234
    assert result["days"][0]["expected_kwh"] == 1.23

    # Der neue Lauf darf den eingefrorenen Wert in der DB auch nicht
    # ueberschrieben haben.
    from app.database import SessionLocal
    from app.models import ForecastPrediction

    session = SessionLocal()
    try:
        row = session.query(ForecastPrediction).filter_by(device_id="wr1").one()
        assert row.expected_w == 1234.0
    finally:
        session.close()


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
    devices = result["days"][0]["devices"]
    assert [item["device_id"] for item in devices] == ["wr1", "wr2"]

    # Pro-Geraet-Aufschluesselung: wr1 (3000 W) liefert mehr als wr2 (1000 W),
    # beide haben aber dieselbe Prognosestunde (12 Uhr) als Produktionsfenster
    # und Spitze - das gilt jetzt auch je Geraet, nicht nur kombiniert.
    wr1, wr2 = devices
    assert wr1["expected_kwh"] == 3.0
    assert wr2["expected_kwh"] == 1.0
    assert wr1["peak_kw"] == 3.0
    assert wr2["peak_kw"] == 1.0
    assert wr1["production_start"] is not None
    assert wr2["production_start"] is not None

    # Die Stundenwerte (fuer das Diagramm) tragen dieselbe Aufschluesselung -
    # damit sich das Prognose-Diagramm im Frontend auf ein einzelnes Geraet
    # filtern laesst (Klick auf den zugehoerigen Wechselrichter-Tab).
    hour = result["hours"][0]
    assert hour["local_date"] == "2026-06-01"
    # Europe/Berlin ist im Juni CEST (UTC+2); 11:00 UTC (interval_start) wird
    # damit lokal zu 13:00 - dasselbe Bucket-Format wie
    # aggregation.hourly_kwh_per_device, siehe schemas.ForecastHourOut.local_hour.
    assert hour["local_hour"] == "2026-06-01T13:00:00"
    assert {item["device_id"] for item in hour["devices"]} == {"wr1", "wr2"}
    hour_wr1 = next(item for item in hour["devices"] if item["device_id"] == "wr1")
    hour_wr2 = next(item for item in hour["devices"] if item["device_id"] == "wr2")
    assert hour_wr1["expected_kw"] == 3.0
    assert hour_wr2["expected_kw"] == 1.0
    assert round(hour_wr1["expected_kw"] + hour_wr2["expected_kw"], 3) == hour["expected_kw"]


def test_summarize_and_empty_result_report_configured_freeze_time(monkeypatch):
    """Das Frontend zeigt in der "Morgen"-Ansicht einen Hinweis, ab welcher
    Uhrzeit die Prognose fuer den Folgetag feststeht (siehe
    config.FORECAST_FREEZE_TIME) - dafuer muss sowohl der normale Erfolgsfall
    (_summarize) als auch der Fehlerfall ohne Trainingsdaten (_empty_result,
    z.B. direkt nach Inbetriebnahme) die konfigurierte Uhrzeit mitliefern,
    statt sie im Frontend hart zu codieren."""
    import app.energy_forecast as module

    monkeypatch.setattr(module.settings, "forecast_freeze_time", "21:30")

    training = {
        "wr1": [
            TrainingPoint(_weather(12, 600, day=(i % 27) + 1), 3000)
            for i in range(MIN_TRAINING_SAMPLES)
        ],
    }
    result = _summarize(training, [_weather(12, 600)])
    assert result["freeze_time"] == "21:30"

    assert _empty_result("keine Daten")["freeze_time"] == "21:30"


def test_refresh_forecast_for_new_day_forces_recompute_despite_fresh_cache(monkeypatch):
    """Nach einem im Betrieb beobachteten Cache-Haenger (das Dashboard zeigte
    ueber einen Tageswechsel hinweg weiter den Vortag) gibt es in main.py
    einen taeglichen 00:01-Trigger (_refresh_forecast_at_midnight), der
    ueber diese Funktion die Prognose garantiert neu berechnet - anders als
    ein normaler forecast_service.get()-Aufruf, der einen noch frischen
    Cache-Eintrag (siehe CACHE_TTL = 30 Minuten) unveraendert zurueckgeben
    wuerde."""
    import app.energy_forecast as module

    call_count = {"n": 0}

    async def fake_build_forecast():
        call_count["n"] += 1
        return {
            "available": False,
            "message": f"lauf {call_count['n']}",
            "generated_at": datetime.now(timezone.utc),
            "training_start": None,
            "training_end": None,
            "training_samples": 0,
            "weather_source": "Open-Meteo",
            "days": [],
            "hours": [],
            "freeze_time": "22:00",
        }

    monkeypatch.setattr(module, "build_forecast", fake_build_forecast)
    # Einen gerade erst gecachten (also noch frischen) Stand simulieren -
    # ein normaler .get()-Aufruf wuerde diesen fuer die naechsten 30 Minuten
    # unveraendert weiterreichen, siehe ForecastService.get().
    monkeypatch.setattr(
        module.forecast_service, "_cached", {"available": False, "message": "alt"}
    )
    monkeypatch.setattr(module.forecast_service, "_cached_at", datetime.now(timezone.utc))

    unchanged = asyncio.run(module.forecast_service.get())
    assert unchanged["message"] == "alt", "frischer Cache wird normalerweise nicht neu berechnet"
    assert call_count["n"] == 0

    result = asyncio.run(refresh_forecast_for_new_day())
    assert call_count["n"] == 1, "refresh_forecast_for_new_day() muss den Cache-Ablauf erzwingen"
    assert result["message"] == "lauf 1"


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


def test_estimate_cell_temperature_matches_faiman_model():
    """Faiman-Modell: T_zelle = T_luft + G / (U0 + U1 * Wind). Direkter
    Nachrechnung mit den in energy_forecast.py verwendeten Standard-
    koeffizienten (U0=25.0, U1=6.84), um Tippfehler in der Formel selbst
    aufzudecken."""
    import app.energy_forecast as module

    point = WeatherPoint(
        timestamp=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        shortwave_w_m2=800.0,
        direct_w_m2=560.0,
        diffuse_w_m2=240.0,
        temperature_c=25.0,
        wind_speed_ms=2.0,
    )
    expected = 25.0 + 800.0 / (25.0 + 6.84 * 2.0)
    assert module._estimate_cell_temperature_c(point) == pytest.approx(expected)


def test_estimate_cell_temperature_is_cooled_by_wind():
    """Mehr Wind muss (bei sonst gleichen Werten) zu einer niedrigeren
    geschaetzten Zelltemperatur fuehren - der eigentliche physikalische Sinn
    dieses Merkmals (siehe DistanceWeights.cell_temperature)."""
    import app.energy_forecast as module

    calm = WeatherPoint(
        timestamp=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        shortwave_w_m2=800.0,
        direct_w_m2=560.0,
        diffuse_w_m2=240.0,
        temperature_c=25.0,
        wind_speed_ms=0.0,
    )
    windy = WeatherPoint(
        timestamp=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        shortwave_w_m2=800.0,
        direct_w_m2=560.0,
        diffuse_w_m2=240.0,
        temperature_c=25.0,
        wind_speed_ms=8.0,
    )
    assert module._estimate_cell_temperature_c(windy) < module._estimate_cell_temperature_c(calm)


def test_fit_distance_weights_still_learns_when_one_new_feature_is_constant():
    """Praxisfall: an vielen Standorten/in vielen Zeitraeumen gibt es gar
    keinen Schnee - snow_depth_m ist dann ueber die GESAMTE Trainingshistorie
    konstant 0. Das darf (anders als vor der Umstellung auf spaltenweises
    Maskieren) nicht mehr dazu fuehren, dass ueberhaupt nichts gelernt wird -
    die weiterhin streuende Strahlung soll trotzdem klar staerker gewichtet
    werden als Stunde/Tag/Temperatur, genau wie im Test ohne die neuen,
    teils konstanten Merkmale."""
    rng = random.Random(7)
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
            snow_depth_m=0.0,  # ueberall und immer schneefrei
        )
        samples.append(TrainingPoint(weather, radiation * 5.0))

    weights = fit_distance_weights(samples)
    assert weights.snow_depth == 0.0, "konstantes Merkmal darf keine Wichtigkeit lernen"
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


def test_model_selection_uses_learned_weights_only_when_backtest_is_better(
    monkeypatch,
):
    import app.energy_forecast as module

    learned = DistanceWeights(2.0, 1.0, 3.0, 1.0, 1.0, 0.2)
    monkeypatch.setattr(module, "fit_distance_weights", lambda _samples: learned)

    def fake_predict(_training, target, weights=None, arrays=None):
        factor = 5.0 if weights == learned else 3.0
        value = target.shortwave_w_m2 * factor
        return value, value, value

    monkeypatch.setattr(module, "predict_power", fake_predict)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    training = []
    for index in range(BACKTEST_MIN_SAMPLES):
        weather = WeatherPoint(
            timestamp=start + timedelta(hours=index),
            shortwave_w_m2=500.0,
            direct_w_m2=350.0,
            diffuse_w_m2=150.0,
            temperature_c=10.0,
        )
        training.append(TrainingPoint(weather, 2500.0))

    profile = select_model(training)
    assert profile.method == "learned"
    assert profile.weights == learned
    assert profile.validation_samples >= 24
    assert profile.validation_error_percent == 0.0


def test_model_selection_keeps_standard_weights_when_learning_is_worse(monkeypatch):
    import app.energy_forecast as module

    learned = DistanceWeights(2.0, 1.0, 3.0, 1.0, 1.0, 0.2)
    monkeypatch.setattr(module, "fit_distance_weights", lambda _samples: learned)

    def fake_predict(_training, target, weights=None, arrays=None):
        factor = 7.0 if weights == learned else 5.0
        value = target.shortwave_w_m2 * factor
        return value, value, value

    monkeypatch.setattr(module, "predict_power", fake_predict)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    training = [
        TrainingPoint(
            WeatherPoint(
                timestamp=start + timedelta(hours=index),
                shortwave_w_m2=500.0,
                direct_w_m2=350.0,
                diffuse_w_m2=150.0,
                temperature_c=10.0,
            ),
            2500.0,
        )
        for index in range(BACKTEST_MIN_SAMPLES)
    ]

    profile = select_model(training)
    assert profile.method == "standard"
    assert profile.weights == DEFAULT_DISTANCE_WEIGHTS


def test_historical_error_calibrates_forecast_range(monkeypatch):
    import app.energy_forecast as module

    training = [TrainingPoint(_weather(12, 700), 1000.0)]
    arrays = _prepare_training_arrays(training)
    profile = ModelProfile(
        weights=DEFAULT_DISTANCE_WEIGHTS,
        method="standard",
        validation_samples=30,
        validation_error_percent=20.0,
        interval_error_fraction=0.2,
    )
    monkeypatch.setattr(
        module,
        "predict_power",
        lambda *_args, **_kwargs: (1000.0, 950.0, 1050.0),
    )

    expected, low, high = _predict_with_profile(
        training, _weather(12, 700), profile, arrays
    )
    assert expected == 1000.0
    assert low == 800.0
    assert high == 1200.0
