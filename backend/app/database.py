"""SQLite-Anbindung ueber SQLAlchemy."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

settings.db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401  (registriert die Modelle an Base)

    # Legt fehlende TABELLEN an - aendert aber KEINE bestehenden Tabellen ab,
    # falls seit der letzten Version neue Spalten zu einem bestehenden Modell
    # (z.B. Reading) hinzugekommen sind. Fuer sowas braeuchte man eigentlich
    # ein Migrationswerkzeug wie Alembic - fuer dieses Projekt lohnt sich der
    # Aufwand dafuer (noch) nicht, deshalb kleine manuelle Migrationen direkt
    # hier (siehe _ensure_ac_power_column).
    Base.metadata.create_all(bind=engine)
    _ensure_ac_power_column()


def _ensure_ac_power_column() -> None:
    """Ergaenzt die Spalte readings.ac_power_w, falls sie noch fehlt (z.B.
    Bestandsdatenbank von vor diesem Update). Bei einer frisch angelegten
    Tabelle (ueber create_all() oben) ist die Spalte bereits vorhanden - dann
    passiert hier nichts. SQLite unterstuetzt ADD COLUMN direkt, ohne die
    Tabelle neu anlegen zu muessen; bestehende Zeilen bekommen NULL fuer die
    neue Spalte (siehe README fuer die Auswirkung auf die Berechnung)."""
    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(readings)")}
        if "ac_power_w" not in columns:
            conn.exec_driver_sql("ALTER TABLE readings ADD COLUMN ac_power_w FLOAT")
            conn.commit()
