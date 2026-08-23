"""Tests fuer Speicherung und Ist-Vergleich von PV-Prognosen."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.forecast_evaluation import (
    _tolerant_hour_error_w,
    get_forecast_accuracy,
    get_yesterday_hourly_comparison,
    save_forecast_predictions,
)
from app.models import ForecastPrediction, Reading

from .conftest import make_user


def test_prediction_is_updated_freely_before_freeze_cutoff(client):
    """Vor der Einfrier-Grenze (siehe FORECAST_FREEZE_TIME, Standard 22 Uhr
    lokal am Vortag des Zieltages) darf ein neuer Modelllauf eine noch nicht
    begonnene Zielstunde beliebig oft aktualisieren - wie schon vor der
    Festschreibungs-Funktion."""
    target = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)  # 14 Uhr Berlin
    first_generated = datetime(2026, 5, 30, 8, tzinfo=timezone.utc)
    save_forecast_predictions(
        {"wr1": {target: (3000.0, 2500.0, 3500.0)}},
        {"wr1": "standard"},
        first_generated,
    )
    # Deutlich vor der Einfrier-Grenze fuer den 2.6. (1.6., 22 Uhr Berlin =
    # 20 Uhr UTC) - darf den Wert noch aendern.
    save_forecast_predictions(
        {"wr1": {target: (3200.0, 2700.0, 3700.0)}},
        {"wr1": "learned"},
        datetime(2026, 5, 31, 8, tzinfo=timezone.utc),
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


def test_prediction_is_locked_once_freeze_cutoff_passes(client):
    """Sobald ein Modelllauf NACH der Einfrier-Grenze stattfindet, wird sein
    Ergebnis fuer die betroffene Zielstunde endgueltig - auch wenn die
    Stunde selbst noch gar nicht begonnen hat. Jeder weitere Lauf danach
    darf den Wert nicht mehr aendern."""
    target = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)  # 14 Uhr Berlin
    # Einfrier-Grenze fuer den 2.6. ist der 1.6., 22 Uhr Berlin = 20 Uhr UTC.
    save_forecast_predictions(
        {"wr1": {target: (3000.0, 2500.0, 3500.0)}},
        {"wr1": "standard"},
        datetime(2026, 5, 31, 8, tzinfo=timezone.utc),
    )
    # Erster Lauf NACH der Einfrier-Grenze - wird noch uebernommen und ist
    # ab jetzt die endgueltige Prognose fuer diese Stunde.
    save_forecast_predictions(
        {"wr1": {target: (3200.0, 2700.0, 3700.0)}},
        {"wr1": "learned"},
        datetime(2026, 6, 1, 21, tzinfo=timezone.utc),
    )
    # Weiterer Lauf, ebenfalls nach der Grenze und immer noch bevor die
    # Zielstunde begonnen hat - darf den bereits eingefrorenen Wert NICHT
    # mehr ueberschreiben.
    save_forecast_predictions(
        {"wr1": {target: (9999.0, 9999.0, 9999.0)}},
        {"wr1": "learned"},
        datetime(2026, 6, 2, 6, tzinfo=timezone.utc),
    )

    session = SessionLocal()
    try:
        rows = session.scalars(select(ForecastPrediction)).all()
        assert len(rows) == 1
        assert rows[0].expected_w == 3200.0
        assert rows[0].model_method == "learned"
    finally:
        session.close()


def test_prediction_is_never_saved_after_target_hour_starts(client):
    """Unveraendert gegenueber vorher: eine bereits begonnene Zielstunde
    wird generell nicht mehr (weder aktualisiert noch neu angelegt)."""
    target = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    save_forecast_predictions(
        {"wr1": {target: (9999.0, 9999.0, 9999.0)}},
        {"wr1": "learned"},
        target,
    )

    session = SessionLocal()
    try:
        rows = session.scalars(select(ForecastPrediction)).all()
        assert rows == []
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
    # accuracy_percent zieht je Stunde zunaechst die Toleranz aus
    # _tolerant_hour_error_w ab (siehe dort), bevor die 1000-W-Fehler in die
    # Genauigkeit einfliessen: Stunde 1 (Ist 4000 W) -> Toleranz 200 W ->
    # effektiver Fehler 800 W; Stunde 2 (Ist 3000 W) -> Toleranz 150 W ->
    # effektiver Fehler 850 W. difference_kwh/difference_percent bleiben
    # davon unberuehrt (oben weiterhin 0.0) - nur die Genauigkeitszahl wird
    # nicht mehr unrealistisch streng bewertet.
    expected_accuracy = round(100 * (1 - (0.8 + 0.85) / 7.0), 1)
    assert day["accuracy_percent"] == expected_accuracy
    assert day["accuracy_percent"] < 100.0
    assert day["devices"][0]["accuracy_percent"] == expected_accuracy
    assert result["overall_accuracy_percent"] == expected_accuracy


def test_tolerant_hour_error_absorbs_small_deviations_fully():
    """Innerhalb der Toleranz (Sockel ODER Prozentsatz, je nachdem was
    groesser ist) zaehlt eine Abweichung gar nicht - eine Wetterprognose
    kann realistischerweise nicht auf das Watt genau treffen."""
    # 80 W Abweichung liegt unter dem festen Sockel (100 W) UND unter 5 %
    # von 1000 W (50 W waere die reine Prozent-Toleranz, hier greift also
    # der Sockel) -> voll toleriert.
    assert _tolerant_hour_error_w(1080.0, 1000.0) == 0.0
    # Bei einer grossen Leistung (10000 W) ist der Prozentsatz (5 % = 500 W)
    # groesser als der Sockel - eine Abweichung von 400 W bleibt darunter.
    assert _tolerant_hour_error_w(10400.0, 10000.0) == 0.0


def test_tolerant_hour_error_counts_only_the_excess_beyond_tolerance():
    """Oberhalb der Toleranz zaehlt nur der ueberschiessende Anteil, nicht
    der komplette Fehler - siehe _tolerant_hour_error_w."""
    # Ist-Wert 1000 W -> Toleranz = max(100, 5% von 1000 = 50) = 100 W.
    assert _tolerant_hour_error_w(1300.0, 1000.0) == 200.0
    # Ist-Wert 10000 W -> Toleranz = max(100, 5% von 10000 = 500) = 500 W.
    assert _tolerant_hour_error_w(11000.0, 10000.0) == 500.0


def test_accuracy_treats_small_deviation_within_tolerance_as_perfect(client):
    """End-to-End: eine Stunde, die nur knapp (innerhalb der Toleranz)
    daneben lag, soll die Genauigkeit nicht mehr verschlechtern - auch wenn
    difference_kwh/difference_percent weiterhin die tatsaechliche,
    ungefilterte Abweichung zeigen."""
    hour = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    generated_at = hour - timedelta(days=1)
    # 80 W Abweichung bei 4000 W Ist-Leistung liegt unter der Toleranz
    # (5 % von 4000 W = 200 W; dieser Wert ist groesser als der
    # 100-W-Sockel und greift deshalb hier).
    save_forecast_predictions(
        {"wr1": {hour: (4080.0, 3000.0, 5000.0)}},
        {"wr1": "standard"},
        generated_at,
    )
    session = SessionLocal()
    try:
        session.add(
            Reading(
                device_id="wr1",
                device_name="WR 1",
                timestamp=hour + timedelta(minutes=30),
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
    day = result["days"][0]
    assert day["accuracy_percent"] == 100.0
    # Die rohe Abweichung bleibt sichtbar, wird also NICHT verschleiert.
    assert day["difference_kwh"] == round((4.0 - 4.08), 2)
    assert day["difference_kwh"] != 0.0


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
