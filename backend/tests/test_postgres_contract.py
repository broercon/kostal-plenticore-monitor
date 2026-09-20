"""Zusagen, die diese Anwendung an PostgreSQL stellt.

Die Tests hier pruefen nicht Fachlogik, sondern die Schnittstelle zur
Datenbank selbst: dass Zeitstempel als derselbe Zeitpunkt zurueckkommen,
dass die Stunden-Einteilung in UTC rechnet, dass die Kappung der reinen
PV-Leistung je ZEILE greift, und dass PostgreSQL seine Zusagen
(VARCHAR-Laengen, Fremdschluessel) tatsaechlich durchsetzt.

Der Anlass ist konkret: diese Anwendung lief frueher auf SQLite. Beim
Umstieg kamen drei Fehler ans Licht, die SQLite jahrelang verziehen hatte
- eine zu knapp deklarierte VARCHAR-Laenge, eine nicht gepruefte
Loeschreihenfolge bei einem Fremdschluessel und ein Zeitstempel, der neu
etikettiert statt umgerechnet wurde. Alle drei haetten die Tests hier
sofort gefunden. Sie stehen deshalb bewusst zusammen in einer Datei, als
Erinnerung an diese Fehlerklasse.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.database import SessionLocal, engine
from app.energy_forecast import _raw_hourly_pv_average
from app.models import DailyEnergyCache, Reading, Session as SessionModel, User
from app.weather_cache import _utc

from .conftest import make_user


def _add_readings(rows: list[tuple[datetime, float, float | None]]) -> None:
    db = SessionLocal()
    try:
        db.add_all(
            [
                Reading(
                    device_id="wr1",
                    device_name="WR 1",
                    timestamp=ts,
                    pv_power_w=pv,
                    battery_power_w=battery,
                )
                for ts, pv, battery in rows
            ]
        )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Zeit, Zahlen, Rundung
# ---------------------------------------------------------------------------


def test_hour_bucket_labels_the_end_of_the_utc_hour(client):
    """Die Stunden-Einteilung muss in UTC rechnen, nicht in Ortszeit.

    Geprueft wird die Konvention, dass eine Messstunde den ENDE-Zeitpunkt
    als Schluessel bekommt (12:00-13:00 -> 13:00), weil Open-Meteo
    Strahlung ebenso als Mittel der vorangegangenen Stunde kennzeichnet
    (siehe energy_forecast._hour_bucket_expression).

    Die Messwerte liegen bewusst auch ueber einer Tagesgrenze: ein
    Messwert um 23:30 UTC gehoert in den Eimer 00:00 des FOLGETAGS. Wuerde
    date_trunc() sich auf die Zeitzone der Sitzung verlassen statt
    ausdruecklich nach UTC zu drehen, laege dieser Wert bei einem
    Berliner Server im falschen Eimer.
    """
    day = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _add_readings(
        [
            (day.replace(hour=12, minute=0), 1000.0, None),
            (day.replace(hour=12, minute=59), 2000.0, None),
            (day.replace(hour=13, minute=0), 3000.0, None),
            (day.replace(hour=23, minute=30), 4000.0, None),
        ]
    )

    result = _raw_hourly_pv_average(day, day + timedelta(days=1, hours=1))

    assert result["wr1"] == {
        # 12:00 und 12:59 fallen beide in die Messstunde 12:00-13:00.
        day.replace(hour=13): 1500.0,
        day.replace(hour=14): 3000.0,
        # 23:30 gehoert in die Stunde, die um 00:00 des Folgetags endet.
        day + timedelta(days=1): 4000.0,
    }


def test_pure_pv_is_clamped_per_row_not_across_rows(client):
    """Die Untergrenze 0 der reinen PV-Leistung muss JE ZEILE greifen.

    Der SQL-Ausdruck verwendet dafuer greatest() und nicht max() - max()
    ist in PostgreSQL ausschliesslich eine Aggregatfunktion ueber Zeilen
    hinweg. Wuerde die Kappung versehentlich als Aggregat ausgewertet,
    kaeme ein anderer Mittelwert heraus.

    Die zweite Zeile ist der eigentliche Pruefstein: dort ist die
    Batterieleistung groesser als die PV-Leistung, die Differenz also
    negativ und muss auf 0 gekappt werden - nicht etwa den Mittelwert
    nach unten ziehen.
    """
    hour = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    _add_readings(
        [
            (hour, 1000.0, 0.0),        # rein: 1000
            (hour.replace(minute=20), 100.0, 500.0),   # rein: max(0, -400) = 0
            (hour.replace(minute=40), 900.0, 100.0),   # rein: 800
        ]
    )

    result = _raw_hourly_pv_average(hour, hour + timedelta(hours=1))

    # (1000 + 0 + 800) / 3 - mit einer Kappung PRO ZEILE.
    assert result["wr1"][hour + timedelta(hours=1)] == pytest.approx(600.0)


def test_longest_real_cache_key_round_trips(client):
    """Der laengste Schluessel, den die App tatsaechlich in
    daily_energy_cache.field schreibt, muss unveraendert wieder
    herauskommen.

    Dieser Schluessel wird in daily_summary.build_battery_energy_summary()
    aus einem 16-stelligen Hash-Praefix zusammengesetzt und ist damit 37
    Zeichen lang. Die Spalte war frueher als String(32) deklariert - unter
    der alten SQLite-Datenbank fiel das nie auf, weil SQLite
    VARCHAR-Laengen nicht erzwingt. PostgreSQL weist zu lange Werte ab.
    """
    config_key = hashlib.sha256(b"beliebige-konfiguration").hexdigest()[:16]
    field = f"battery:v2:{config_key}:discharge"
    assert len(field) == 37, "Schluesselformat geaendert - Spaltenbreite pruefen"

    db = SessionLocal()
    try:
        db.add(
            DailyEnergyCache(
                field=field,
                date="2026-06-01",
                kwh=12.5,
                computed_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        stored = db.scalars(
            select(DailyEnergyCache).where(DailyEnergyCache.field == field)
        ).one()
        assert stored.field == field
        assert stored.kwh == 12.5
    finally:
        db.close()


def test_reading_timestamp_round_trips_as_the_same_instant(client):
    """Ein gespeicherter Zeitstempel muss als DERSELBE Zeitpunkt
    zurueckkommen.

    Das ist die Zusage, auf der saemtliche Auswertungen dieser App
    aufbauen. Der Vergleich laeuft ueber dieselbe Normalisierung, die auch
    der Anwendungscode verwendet."""
    moment = datetime(2026, 6, 1, 14, 37, 21, 123456, tzinfo=timezone.utc)
    _add_readings([(moment, 1234.0, None)])

    db = SessionLocal()
    try:
        stored = db.scalars(select(Reading)).one()
        assert _utc(stored.timestamp) == moment
    finally:
        db.close()


def test_utc_helper_converts_instead_of_relabelling(client):
    """weather_cache._utc() darf einen bereits zonenbehafteten Zeitstempel
    nur UMRECHNEN, nie neu etikettieren.

    Genau hier lag ein Fehler: ein unbedingtes replace(tzinfo=utc)
    verschiebt einen bereits zonenbehafteten Zeitpunkt still um den
    Zonenversatz. Da die betroffene Tabelle die Trainingsdaten der
    PV-Prognose enthaelt (ein volles Jahr), waere das lange unbemerkt
    geblieben.

    Dieser Test braucht keine Datenbank - er sichert das Muster selbst ab.
    """
    naiv = datetime(2026, 6, 1, 12, 0)
    assert _utc(naiv) == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    # +02:00 (Berliner Sommerzeit): 14:00 Ortszeit sind 12:00 UTC.
    mit_versatz = datetime(2026, 6, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert _utc(mit_versatz) == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Zusagen, die PostgreSQL durchsetzt
# ---------------------------------------------------------------------------


def test_connection_timezone_is_utc(client):
    """Die Verbindung muss auf UTC stehen (siehe database.py).

    Ohne diese Festlegung liefert PostgreSQL timestamptz-Werte in der
    Zeitzone des Servers - rechnerisch derselbe Zeitpunkt, aber jede
    Stelle, die einen Zeitstempel nur formatiert statt umzurechnen, saehe
    dann Ortszeit. Auch date_trunc() richtet sich danach."""
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SHOW timezone").scalar() == "UTC"


def test_timestamps_come_back_timezone_aware(client):
    """PostgreSQL gibt DateTime(timezone=True)-Spalten zonenbehaftet
    zurueck.

    Darauf verlaesst sich der Anwendungscode nicht blind - er
    normalisiert (siehe weather_cache._utc) -, aber die Zusage gehoert
    festgehalten: sie ist der Grund, warum ein unbedingtes
    replace(tzinfo=utc) hier falsch waere."""
    _add_readings([(datetime(2026, 6, 1, 12, tzinfo=timezone.utc), 500.0, None)])

    db = SessionLocal()
    try:
        stored = db.scalars(select(Reading)).one()
        assert stored.timestamp.tzinfo is not None
    finally:
        db.close()


def test_foreign_key_from_session_to_user_is_enforced(client):
    """Eine Sitzung ohne zugehoerigen Nutzer darf es nicht geben.

    Praktisch relevant beim Loeschen: dort muss die Reihenfolge stimmen
    (Sitzungen vor Nutzern). Unter der frueheren SQLite-Datenbank war das
    folgenlos, weil SQLite Fremdschluessel ohne "PRAGMA foreign_keys=ON"
    gar nicht prueft."""
    user = make_user("fk-pruefung", "passwort-fuer-den-test")
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        db.add(
            SessionModel(
                token="token-fuer-die-fk-pruefung",
                user_id=user.id,
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        db.execute(select(User))  # Verbindung aufbauen
        with pytest.raises(IntegrityError):
            db.query(User).filter(User.id == user.id).delete()
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_varchar_length_is_actually_enforced(client):
    """PostgreSQL weist zu lange Werte ab, statt sie stillschweigend zu
    speichern.

    Das ist der Mechanismus hinter dem String(32)-Fehler (siehe
    test_longest_real_cache_key_round_trips). Wer eine Spalte kuenftig zu
    knapp deklariert, bekommt hier sofort eine Rueckmeldung statt erst im
    Betrieb."""
    db = SessionLocal()
    try:
        db.add(
            DailyEnergyCache(
                field="x" * 65,  # Spalte ist String(64)
                date="2026-06-01",
                kwh=1.0,
                computed_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(DBAPIError):
            db.commit()
        db.rollback()
    finally:
        db.close()
