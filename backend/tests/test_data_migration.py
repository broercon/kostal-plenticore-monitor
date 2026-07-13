"""Test fuer den wichtigen Praxisfall: Bestandsinstallationen haben bereits
Messwerte (readings) in der Datenbank, bevor die Benutzerverwaltung
(users/sessions-Tabellen) hinzugekommen ist. Nach einem Update/Neustart mit
der neuen Version duerfen diese Altdaten weder verloren gehen noch
veraendert werden - init_db() darf nur fehlende Tabellen ERGAENZEN.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import inspect, select

from app import auth
from app.database import Base, SessionLocal, engine, init_db
from app.models import Reading


def test_init_db_only_adds_missing_tables_existing_readings_survive():
    # Ausgangslage nachstellen: nur die (alte) readings-Tabelle existiert,
    # mit einem bereits vorhandenen Messwert - so saehe eine Bestandsdatenbank
    # vor dem Auth-Update aus.
    Base.metadata.drop_all(bind=engine)
    Reading.__table__.create(bind=engine)

    db = SessionLocal()
    try:
        db.add(
            Reading(
                device_id="wr1",
                device_name="Bestands-Wechselrichter",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                home_power_w=1234.5,
                pv_power_w=2000.0,
            )
        )
        db.commit()
    finally:
        db.close()

    # init_db() wie beim Start der neuen Version mit Benutzerverwaltung:
    # muss users/sessions ergaenzen, darf readings aber nicht anfassen.
    init_db()

    tables = set(inspect(engine).get_table_names())
    assert {"readings", "users", "sessions"} <= tables

    db = SessionLocal()
    try:
        rows = list(db.scalars(select(Reading)))
        assert len(rows) == 1
        assert rows[0].device_name == "Bestands-Wechselrichter"
        assert rows[0].home_power_w == 1234.5
        assert rows[0].pv_power_w == 2000.0
    finally:
        db.close()

    # Und das Seeding der Default-Nutzer funktioniert auf einer so
    # "nachgeruesteten" Datenbank genauso wie auf einer komplett neuen.
    auth.seed_default_users()
    assert {u.username for u in auth.list_users()} == {"admin", "betreiber1", "betreiber2"}


def test_init_db_adds_missing_ac_power_column_without_losing_data():
    """Simuliert eine Bestandsdatenbank von VOR dem ac_power_w-Feature: die
    readings-Tabelle existiert bereits, aber ohne diese Spalte (per
    Rohsyntax nachgebaut, da Reading.__table__.create() die Spalte ja
    bereits aus dem aktuellen Modell miterzeugen wuerde). init_db() muss die
    fehlende Spalte ergaenzen (ALTER TABLE), OHNE die vorhandenen Messwerte
    zu verlieren oder zu veraendern."""
    Base.metadata.drop_all(bind=engine)
    with engine.connect() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id VARCHAR(64) NOT NULL,
                device_name VARCHAR(128) NOT NULL,
                timestamp DATETIME NOT NULL,
                home_power_w FLOAT,
                grid_power_w FLOAT,
                feed_in_power_w FLOAT,
                grid_draw_power_w FLOAT,
                pv_power_w FLOAT,
                battery_power_w FLOAT,
                battery_soc_percent FLOAT,
                yield_day_kwh FLOAT,
                home_consumption_day_kwh FLOAT,
                energy_grid_day_kwh FLOAT
            )
            """
        )
        conn.commit()

    columns_before = {row[1] for row in engine.connect().exec_driver_sql("PRAGMA table_info(readings)")}
    assert "ac_power_w" not in columns_before

    # Per Rohsyntax einfuegen (nicht ueber das ORM-Modell Reading()): das
    # aktuelle Modell kennt bereits ac_power_w und wuerde beim INSERT
    # versuchen, diese (in der alten Tabelle noch fehlende) Spalte
    # mitzuschreiben - genau das soll hier ja realistisch nachgestellt
    # werden (Daten, die eine AELTERE App-Version ohne dieses Feld
    # geschrieben hat).
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "INSERT INTO readings (device_id, device_name, timestamp, home_power_w, pv_power_w) "
            "VALUES ('wr1', 'Bestands-Wechselrichter', '2026-01-01 00:00:00', 1234.5, 2000.0)"
        )
        conn.commit()

    init_db()

    columns_after = {row[1] for row in engine.connect().exec_driver_sql("PRAGMA table_info(readings)")}
    assert "ac_power_w" in columns_after

    db = SessionLocal()
    try:
        rows = list(db.scalars(select(Reading)))
        assert len(rows) == 1
        assert rows[0].home_power_w == 1234.5
        assert rows[0].pv_power_w == 2000.0
        # Fuer die alte Zeile ist die neue Spalte NULL, nicht etwa 0 o.ae.
        assert rows[0].ac_power_w is None
    finally:
        db.close()

    # Erneuter Aufruf (z.B. naechster Container-Neustart) darf nicht erneut
    # versuchen, die Spalte hinzuzufuegen (waere ein SQL-Fehler).
    init_db()
