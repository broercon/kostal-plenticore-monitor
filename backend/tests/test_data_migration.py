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
