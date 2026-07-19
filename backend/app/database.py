"""SQLite-Anbindung ueber SQLAlchemy."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

settings.db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ARG001
    """WAL-Modus statt SQLites Standard-Rollback-Journal: lesende Zugriffe
    (Dashboard-/API-Abfragen) blockieren dann nicht mehr gegenseitig mit dem
    Poller, der alle paar Sekunden neue Messwerte schreibt (und umgekehrt) -
    relevant, weil diese App staendig gleichzeitig liest und schreibt.
    synchronous=NORMAL ist die fuer WAL uebliche Kombination (etwas
    schwaecheres Crash-Sicherheitsversprechen als FULL, aber weiterhin
    konsistente Daten nach einem Prozess-Absturz, nur nicht zwingend nach
    einem Betriebssystem-/Stromausfall genau im Schreibmoment)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


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
    _ensure_readings_timestamp_index()


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


def _ensure_readings_timestamp_index() -> None:
    """Ergaenzt einen Index rein auf readings.timestamp (ohne device_id),
    falls er noch fehlt - fuer Bestandsdatenbanken von vor dieser Aenderung
    (bei einer frisch angelegten Tabelle ist er bereits ueber
    models.Reading.__table_args__ vorhanden). CREATE INDEX IF NOT EXISTS ist
    in SQLite direkt idempotent, eine eigene Existenzpruefung wie bei
    _ensure_ac_power_column ist hier nicht noetig."""
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_readings_timestamp ON readings (timestamp)"
        )
        conn.commit()
