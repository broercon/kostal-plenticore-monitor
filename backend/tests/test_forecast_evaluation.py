"""Tests fuer Speicherung und Ist-Vergleich von PV-Prognosen."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.forecast_evaluation import (
    get_forecast_accuracy,
    get_yesterday_hourly_comparison,
    save_forecast_predictions,
)
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


def test_accuracy_does_not_let_opposite_hourly_errors_cancel_out(client):
    """Regression: ein Tag, an dem die Prognose in einer Stunde zu hoch und
    in einer anderen zu niedrig lag, darf nicht als treffsicher gelten, nur
    weil sich die Fehler beim Aufsummieren der Tagessumme gegenseitig
    aufheben (Netto-Differenz waere 0, obwohl JEDE Stunde daneben lag)."""
    hour1 = datetime(2026, 6, 1, 11, tzinfo=timezone.utc)
    hour2 = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    generated_at = hour1 - timedelta(days=1)
    save_forecast_predictions(
        {
            "wr1": {
                hour1: (3000.0, 2000.0, 4000.0),  # 1000 W zu niedrig prognostiziert
                hour2: (4000.0, 3000.0, 5000.0),  # 1000 W zu hoch prognostiziert
            }
        },
        {"wr1": "standard"},
        generated_at,
    )
    session = SessionLocal()
    try:
        session.add_all(
            [
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=hour1 + timedelta(minutes=30),
                    pv_power_w=4000.0,
                    battery_power_w=0.0,
                ),
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=hour2 + timedelta(minutes=30),
                    pv_power_w=3000.0,
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
    day = result["days"][0]
    # Nettosumme ist perfekt ausgeglichen (7000 W erwartet vs. 7000 W
    # tatsaechlich ueber beide Stunden), trotzdem lag JEDE einzelne Stunde
    # um 1000 W daneben - die Genauigkeit darf das nicht als 100% ausweisen.
    assert day["expected_kwh"] == 7.0
    assert day["actual_kwh"] == 7.0
    assert day["difference_kwh"] == 0.0
    expected_accuracy = round(100 * (1 - 2.0 / 7.0), 1)
    assert day["accuracy_percent"] == expected_accuracy
    assert day["accuracy_percent"] < 100.0
    assert day["devices"][0]["accuracy_percent"] == expected_accuracy
    assert result["overall_accuracy_percent"] == expected_accuracy


def test_accuracy_allows_cancellation_across_devices_within_the_same_hour(client):
    """Anders als bei verschiedenen STUNDEN duerfen sich Fehler verschiedener
    GERAETE INNERHALB DERSELBEN STUNDE ausgleichen: wenn WR1 in einer Stunde
    zu viel und WR2 zu wenig prognostiziert hat, kann die Gesamtanlage in
    dieser Stunde trotzdem treffsicher gewesen sein - das ist eine echte
    physikalische Kombination am selben Hausanschluss, keine kuenstliche
    Mittelung ueber die Zeit wie im Test oben."""
    hour = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    generated_at = hour - timedelta(days=1)
    save_forecast_predictions(
        {
            "wr1": {hour: (3000.0, 2000.0, 4000.0)},  # 1000 W zu niedrig
            "wr2": {hour: (2000.0, 1000.0, 3000.0)},  # 1000 W zu hoch
        },
        {"wr1": "standard", "wr2": "standard"},
        generated_at,
    )
    session = SessionLocal()
    try:
        session.add_all(
            [
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=hour + timedelta(minutes=30),
                    pv_power_w=4000.0,
                    battery_power_w=0.0,
                ),
                Reading(
                    device_id="wr2",
                    device_name="WR 2",
                    timestamp=hour + timedelta(minutes=30),
                    pv_power_w=1000.0,
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
    day = result["days"][0]
    # Kombiniert (Hausanschluss) war die Stunde exakt getroffen: 5000 W
    # erwartet, 5000 W tatsaechlich.
    assert day["expected_kwh"] == 5.0
    assert day["actual_kwh"] == 5.0
    assert day["accuracy_percent"] == 100.0
    # Je Geraet einzeln war die Prognose trotzdem keineswegs perfekt.
    wr1 = next(item for item in day["devices"] if item["device_id"] == "wr1")
    wr2 = next(item for item in day["devices"] if item["device_id"] == "wr2")
    assert wr1["accuracy_percent"] < 100.0
    assert wr2["accuracy_percent"] < 100.0


def test_accuracy_reports_today_so_far_separately_from_completed_days(client):
    """Der laufende Tag darf nicht in "days" (abgeschlossene Tage)
    landen - stattdessen soll er separat in "today_so_far" auftauchen, mit
    derselben stuendlichen Genauigkeitsberechnung wie bei den
    abgeschlossenen Tagen."""
    yesterday_hour = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    today_hour = datetime(2026, 6, 2, 8, tzinfo=timezone.utc)
    generated_at = yesterday_hour - timedelta(days=1)
    save_forecast_predictions(
        {"wr1": {yesterday_hour: (3000.0, 2000.0, 4000.0)}},
        {"wr1": "standard"},
        generated_at,
    )
    save_forecast_predictions(
        {"wr1": {today_hour: (2000.0, 1000.0, 3000.0)}},
        {"wr1": "standard"},
        today_hour - timedelta(hours=2),
    )
    session = SessionLocal()
    try:
        session.add_all(
            [
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=yesterday_hour + timedelta(minutes=30),
                    pv_power_w=4000.0,
                    battery_power_w=0.0,
                ),
                # Heute lief die Prognose staerker daneben (1500 W zu
                # niedrig) als der abgeschlossene Vergangenheitstag (1000 W
                # zu hoch) - genau das soll today_so_far zeigen koennen,
                # ohne die "days"-Statistik zu verfaelschen.
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=today_hour + timedelta(minutes=30),
                    pv_power_w=3500.0,
                    battery_power_w=0.0,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    # "now" liegt am selben Kalendertag (lokal, Europe/Berlin) wie today_hour,
    # aber nach dessen vollstaendigem Ablauf.
    result = get_forecast_accuracy(
        days=2, now=datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    )

    assert result["available"] is True
    # Der laufende Tag (2026-06-02) darf NICHT unter den abgeschlossenen
    # Tagen auftauchen.
    assert all(day["date"] != "2026-06-02" for day in result["days"])
    assert any(day["date"] == "2026-06-01" for day in result["days"])

    assert result["today_so_far"] is not None
    assert result["today_so_far"]["date"] == "2026-06-02"
    assert result["today_so_far"]["expected_kwh"] == 2.0
    assert result["today_so_far"]["actual_kwh"] == 3.5
    assert result["today_so_far"]["devices"][0]["device_id"] == "wr1"

    # overall_accuracy_percent bezieht sich weiterhin nur auf die
    # abgeschlossenen Tage, nicht auf den unvollstaendigen laufenden Tag.
    completed_day = next(d for d in result["days"] if d["date"] == "2026-06-01")
    assert result["overall_accuracy_percent"] == completed_day["accuracy_percent"]


def test_accuracy_today_so_far_is_none_without_matching_hours_today(client):
    """Solange fuer den laufenden Tag noch keine passenden Messwerte
    vorliegen (z.B. ganz am Anfang des Tages), bleibt today_so_far None,
    statt einen leeren/falschen Eintrag vorzutaeuschen."""
    yesterday_hour = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    generated_at = yesterday_hour - timedelta(days=1)
    save_forecast_predictions(
        {"wr1": {yesterday_hour: (3000.0, 2000.0, 4000.0)}},
        {"wr1": "standard"},
        generated_at,
    )
    session = SessionLocal()
    try:
        session.add(
            Reading(
                device_id="wr1",
                device_name="WR 1",
                timestamp=yesterday_hour + timedelta(minutes=30),
                pv_power_w=4000.0,
                battery_power_w=0.0,
            )
        )
        session.commit()
    finally:
        session.close()

    result = get_forecast_accuracy(
        days=2, now=datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    )
    assert result["available"] is True
    assert result["today_so_far"] is None


def test_yesterday_hourly_comparison_combines_devices_and_keeps_hours_separate(client):
    """Gegenstueck zu get_forecast_accuracy(), aber auf Stundenebene fuer den
    kompletten Vortag: jede Stunde bleibt fuer sich (keine Tagesverdichtung),
    die Kombination ueber die Geraete INNERHALB derselben Stunde ist aber
    weiterhin eine simple Summe (physikalisch gueltig)."""
    hour1 = datetime(2026, 6, 1, 11, tzinfo=timezone.utc)
    hour2 = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    generated_at = hour1 - timedelta(days=1)
    save_forecast_predictions(
        {
            "wr1": {
                hour1: (3000.0, 2500.0, 3500.0),
                hour2: (4000.0, 3500.0, 4500.0),
            },
            "wr2": {
                hour1: (1000.0, 800.0, 1200.0),
                hour2: (1500.0, 1300.0, 1700.0),
            },
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
                    timestamp=hour1 + timedelta(minutes=30),
                    pv_power_w=3200.0,
                    battery_power_w=0.0,
                ),
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=hour2 + timedelta(minutes=30),
                    pv_power_w=3900.0,
                    battery_power_w=0.0,
                ),
                Reading(
                    device_id="wr2",
                    device_name="WR 2",
                    timestamp=hour1 + timedelta(minutes=30),
                    pv_power_w=900.0,
                    battery_power_w=0.0,
                ),
                Reading(
                    device_id="wr2",
                    device_name="WR 2",
                    timestamp=hour2 + timedelta(minutes=30),
                    pv_power_w=1600.0,
                    battery_power_w=0.0,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    # "now" liegt lokal (Europe/Berlin) am 2026-06-02 - der Vortag ist damit
    # der 2026-06-01, an dem beide Stunden liegen.
    result = get_yesterday_hourly_comparison(
        now=datetime(2026, 6, 2, 8, tzinfo=timezone.utc)
    )
    assert result["available"] is True
    assert result["date"] == "2026-06-01"
    # Nur die Stunden mit tatsaechlich gespeicherter Prognose - auf
    # ausdruecklichen Wunsch keine Luecken-Stunden fuer den restlichen Tag.
    assert len(result["hours"]) == 2

    hour1_entry, hour2_entry = result["hours"]
    assert hour1_entry["expected_kw"] == 4.0  # 3.0 + 1.0
    assert hour1_entry["actual_kw"] == 4.1  # 3.2 + 0.9
    assert hour1_entry["low_kw"] == 3.3
    assert hour1_entry["high_kw"] == 4.7
    assert {d["device_id"] for d in hour1_entry["devices"]} == {"wr1", "wr2"}
    wr1_hour1 = next(d for d in hour1_entry["devices"] if d["device_id"] == "wr1")
    assert wr1_hour1["expected_kw"] == 3.0
    assert wr1_hour1["actual_kw"] == 3.2

    assert hour2_entry["expected_kw"] == 5.5  # 4.0 + 1.5
    assert hour2_entry["actual_kw"] == 5.5  # 3.9 + 1.6


def test_yesterday_hourly_comparison_unavailable_without_stored_predictions(client):
    result = get_yesterday_hourly_comparison(
        now=datetime(2026, 6, 2, 8, tzinfo=timezone.utc)
    )
    assert result["available"] is False
    assert result["date"] == "2026-06-01"
    assert result["hours"] == []


def test_yesterday_hourly_comparison_keeps_actual_none_without_matching_reading(client):
    """Fehlt fuer eine gespeicherte Prognose-Stunde noch der passende
    Messwert (z.B. Ausfall), bleibt actual_kw None statt faelschlich 0."""
    hour = datetime(2026, 6, 1, 11, tzinfo=timezone.utc)
    save_forecast_predictions(
        {"wr1": {hour: (3000.0, 2500.0, 3500.0)}},
        {"wr1": "standard"},
        hour - timedelta(days=1),
    )
    result = get_yesterday_hourly_comparison(
        now=datetime(2026, 6, 2, 8, tzinfo=timezone.utc)
    )
    assert result["available"] is True
    assert len(result["hours"]) == 1
    assert result["hours"][0]["actual_kw"] is None
    assert result["hours"][0]["devices"][0]["actual_kw"] is None


def test_yesterday_endpoint_requires_login(client):
    assert client.get("/api/forecast/yesterday").status_code == 401
    make_user("yesterday-viewer", "valid-password", role="betreiber")
    client.post(
        "/api/auth/login",
        json={"username": "yesterday-viewer", "password": "valid-password"},
    )
    response = client.get("/api/forecast/yesterday")
    assert response.status_code == 200
    assert response.json()["available"] is False


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
