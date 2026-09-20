"""Tests rund um den Betrieb auf zwei Datenbanken (SQLite und PostgreSQL).

Zwei Sorten, bewusst in einer Datei:

1. Zusagen, die unter BEIDEN Datenbanken identisch gelten muessen. Sie
   laufen deshalb in jedem Testlauf mit - einmal gegen SQLite, einmal
   gegen PostgreSQL (siehe docs/DEVELOPMENT.md). Genau hier wuerde
   auffallen, wenn die dialektabhaengigen Ausdruecke in
   energy_forecast.py auseinanderlaufen.

2. Zusagen, die NUR PostgreSQL durchsetzt (Marker postgres_only).
   Sie beschreiben die drei Fallstricke, die beim Umstieg von SQLite
   tatsaechlich zugeschlagen haben - VARCHAR-Laengen, Fremdschluessel
   und zonenbehaftete Zeitstempel - damit dieselbe Klasse Fehler nicht
   ein zweites Mal unbemerkt bleibt.
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

from .conftest import make_user, postgres_only


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
# 1. Gilt unter BEIDEN Datenbanken
# ---------------------------------------------------------------------------


def test_hour_bucket_labels_the_end_of_the_utc_hour(client):
    """Die Stunden-Einteilung muss unter beiden Datenbanken exakt dieselben
    Zeitfenster ergeben.

    Hintergrund: SQLite bildet das ueber strftime(..., '+1 hour'),
    PostgreSQL ueber date_trunc() + to_char() - zwei voellig verschiedene
    Ausdruecke, die dieselbe Textdarstellung liefern muessen (siehe
    energy_forecast._hour_bucket_expression). Geprueft wird die
    Konvention, dass eine Messstunde den ENDE-Zeitpunkt als Schluessel
    bekommt (12:00-13:00 -> 13:00), weil Open-Meteo Strahlung ebenso als
    Mittel der vorangegangenen Stunde kennzeichnet.

    Die Messwerte liegen bewusst auch ueber einer Tagesgrenze: ein
    Messwert um 23:30 UTC gehoert in den Eimer 00:00 des FOLGETAGS - ein
    Ausdruck, der in Ortszeit statt UTC rechnet, wuerde hier danebenliegen.
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

    SQLite schreibt das als max(0.0, ...), PostgreSQL als greatest(0.0, ...)
    - dort ist max() ausschliesslich eine Aggregatfunktion (siehe
    energy_forecast._greatest). Wuerde der Ausdruck versehentlich als
    Aggregat ueber die Zeilen hinweg ausgewertet, faenden beide Datenbanken
    einen anderen Mittelwert.

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
    Zeichen lang. Die Spalte war frueher als String(32) deklariert -
    SQLite erzwingt VARCHAR-Laengen nicht und hat das klaglos gespeichert,
    PostgreSQL weist es ab. Der Fehler fiel deshalb erst beim Umstieg auf,
    Jahre nach seiner Entstehung.
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
    zurueckkommen - unabhaengig davon, ob die Datenbank ihn zonenbehaftet
    (PostgreSQL) oder naiv (SQLite) zurueckgibt.

    Das ist die Zusage, auf der saemtliche Auswertungen dieser App
    aufbauen. Der Vergleich laeuft deshalb ueber dieselbe Normalisierung,
    die auch der Anwendungscode verwendet."""
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

    Genau hier lag ein Fehler: ein unbedingtes replace(tzinfo=utc) ist
    unter SQLite richtig (dort kommen Werte naiv zurueck), haette unter
    PostgreSQL aber den Zeitpunkt still um den Zonenversatz verschoben.
    Da die betroffene Tabelle die Trainingsdaten der PV-Prognose enthaelt
    (ein volles Jahr), waere das lange unbemerkt geblieben.

    Dieser Test braucht keine Datenbank - er sichert das Muster selbst ab.
    """
    naiv = datetime(2026, 6, 1, 12, 0)
    assert _utc(naiv) == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    # +02:00 (Berliner Sommerzeit): 14:00 Ortszeit sind 12:00 UTC.
    mit_versatz = datetime(2026, 6, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert _utc(mit_versatz) == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 2. Nur PostgreSQL setzt das durch
# ---------------------------------------------------------------------------


@postgres_only
def test_connection_timezone_is_utc(client):
    """Die Verbindung muss auf UTC stehen (siehe database.py).

    Ohne diese Festlegung liefert PostgreSQL timestamptz-Werte in der
    Zeitzone des Servers - rechnerisch derselbe Zeitpunkt, aber jede
    Stelle, die einen Zeitstempel nur formatiert statt umzurechnen, saehe
    dann Ortszeit. Auch date_trunc() richtet sich danach."""
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SHOW timezone").scalar() == "UTC"


@postgres_only
def test_timestamps_come_back_timezone_aware(client):
    """PostgreSQL gibt DateTime(timezone=True)-Spalten zonenbehaftet
    zurueck - anders als SQLite.

    Dieser Unterschied ist der Grund, warum der Anwendungscode ueberall
    normalisiert, statt sich auf eine der beiden Formen zu verlassen."""
    _add_readings([(datetime(2026, 6, 1, 12, tzinfo=timezone.utc), 500.0, None)])

    db = SessionLocal()
    try:
        stored = db.scalars(select(Reading)).one()
        assert stored.timestamp.tzinfo is not None
    finally:
        db.close()


@postgres_only
def test_foreign_key_from_session_to_user_is_enforced(client):
    """Eine Sitzung ohne zugehoerigen Nutzer darf es nicht geben.

    SQLite setzt Fremdschluessel ohne "PRAGMA foreign_keys=ON" gar nicht
    durch, PostgreSQL immer. Praktisch relevant beim Loeschen: dort muss
    die Reihenfolge stimmen (Sitzungen vor Nutzern) - genau daran ist die
    erste Fassung des Migrationsskripts gescheitert."""
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


@postgres_only
def test_varchar_length_is_actually_enforced(client):
    """PostgreSQL weist zu lange Werte ab, statt sie stillschweigend zu
    speichern.

    Das ist der Mechanismus hinter dem String(32)-Fehler (siehe
    test_longest_real_cache_key_round_trips). Dieser Test haelt fest, dass
    die Grenze real ist - wer die Spalte kuenftig verengt, bekommt hier
    sofort eine Rueckmeldung statt erst im Betrieb."""
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
