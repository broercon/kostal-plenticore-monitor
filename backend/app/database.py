"""Datenbank-Anbindung ueber SQLAlchemy - SQLite (Standard) oder PostgreSQL.

Welche der beiden verwendet wird, entscheidet allein settings.database_url
(Umgebungsvariable DATABASE_URL). Ohne diese Variable bleibt es bei der
SQLite-Datei unter DB_PATH, eine bestehende Installation aendert sich also
nicht. Die wenigen Stellen im Code, die sich zwischen den Dialekten
tatsaechlich unterscheiden (skalares GREATEST, Stunden-Trunkierung,
Upsert), fragen IS_SQLITE ab - siehe energy_forecast.py und
forecast_evaluation.py.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, make_url
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_url = make_url(settings.database_url)

# "postgresql://..." waehlt in SQLAlchemy standardmaessig den Treiber
# psycopg2; installiert ist hier aber psycopg (Version 3, siehe
# requirements.txt). Statt jeden Aufrufer zu zwingen, das laengere
# "postgresql+psycopg://" zu schreiben, wird der Treiber hier ergaenzt -
# so laesst sich der fertige Connection-String aus der Datenbank-
# Infrastruktur unveraendert uebernehmen.
if _url.get_backend_name() == "postgresql" and _url.get_driver_name() == "psycopg2":
    _url = _url.set(drivername="postgresql+psycopg")

IS_SQLITE = _url.get_backend_name() == "sqlite"

if IS_SQLITE:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        """WAL-Modus statt SQLites Standard-Rollback-Journal: lesende Zugriffe
        (Dashboard-/API-Abfragen) blockieren dann nicht mehr gegenseitig mit dem
        Poller, der alle paar Sekunden neue Messwerte schreibt (und umgekehrt) -
        relevant, weil diese App staendig gleichzeitig liest und schreibt.
        synchronous=NORMAL ist die fuer WAL uebliche Kombination (etwas
        schwaecheres Crash-Sicherheitsversprechen als FULL, aber weiterhin
        konsistente Daten nach einem Prozess-Absturz, nur nicht zwingend nach
        einem Betriebssystem-/Stromausfall genau im Schreibmoment).

        Bei PostgreSQL entfaellt das ersatzlos: dessen MVCC laesst Leser und
        Schreiber ohnehin nicht aufeinander warten."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

else:
    engine = create_engine(
        _url,
        # Die Datenbank liegt bei PostgreSQL in einem anderen Container und
        # damit am Ende einer Netzwerkverbindung, die zwischen zwei Pollings
        # (Standard: 15s) abreissen kann - etwa wenn der Datenbank-Container
        # neu startet. pool_pre_ping verwirft solche Verbindungen beim
        # naechsten Zugriff, statt dem Aufrufer einen Fehler durchzureichen.
        pool_pre_ping=True,
        # Alle Zeitstempel dieser Anwendung sind UTC (siehe models.py). Ohne
        # diese Festlegung liefert PostgreSQL timestamptz-Werte in der
        # Zeitzone des Servers zurueck (hier Europe/Berlin) - rechnerisch
        # zwar derselbe Zeitpunkt, aber jede Stelle, die einen Zeitstempel
        # nur formatiert statt umzurechnen, saehe dann Ortszeit.
        connect_args={"options": "-c timezone=UTC"},
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def _table_columns(conn: Connection, table_name: str) -> set[str]:
    """Spaltennamen einer Tabelle - dialektunabhaengig ueber SQLAlchemys
    eigene Inspector-API statt des SQLite-spezifischen "PRAGMA
    table_info(...)". Funktioniert unveraendert unter SQLite, PostgreSQL,
    SQL Server etc., falls die App irgendwann auf eine andere Datenbank
    umzieht - dann muessten nur noch die CREATE/ALTER-Statements selbst
    dialektspezifisch angepasst werden, nicht mehr die Existenzpruefungen.
    Liefert ein leeres Set, wenn die Tabelle noch gar nicht existiert."""
    inspector = inspect(conn)
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_exists(conn: Connection, table_name: str, index_name: str) -> bool:
    """Dialektunabhaengige Alternative zu "CREATE INDEX IF NOT EXISTS":
    SQLite und PostgreSQL kennen dieses IF-NOT-EXISTS-Suffix zwar beide,
    SQL Server (T-SQL) jedoch nicht - dort muesste man den Index ueber
    sys.indexes abfragen. inspect(conn).get_indexes(...) funktioniert
    ueberall gleich."""
    if not inspect(conn).has_table(table_name):
        return False
    existing = {index["name"] for index in inspect(conn).get_indexes(table_name)}
    return index_name in existing


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
    _ensure_weather_hourly_extra_columns()
    _ensure_pv_string_columns()


# Aufraeumregel fuer die Funktionen unten: eine Migration ist hier nur so
# lange noetig, wie es realistischerweise noch eine Bestandsdatenbank ohne
# das jeweilige Schema-Merkmal geben kann. Bei dieser Einzelplatz-App reicht
# dafuer die eigene Update-Historie als Anhaltspunkt - etwa 6 Monate nach
# Einfuehrung (also lange nach dem naechsten "docker compose up -d --build")
# kann die zugehoerige Migration entfernt werden. Siehe docs/DEVELOPMENT.md
# fuer die ausfuehrliche Begruendung. Einfuehrungsdatum je Migration steht
# im jeweiligen Docstring.


def _ensure_ac_power_column() -> None:
    """Eingefuehrt: 2026-07-13. Ergaenzt die Spalte readings.ac_power_w, falls sie noch fehlt (z.B.
    Bestandsdatenbank von vor diesem Update). Bei einer frisch angelegten
    Tabelle (ueber create_all() oben) ist die Spalte bereits vorhanden - dann
    passiert hier nichts. SQLite unterstuetzt ADD COLUMN direkt, ohne die
    Tabelle neu anlegen zu muessen; bestehende Zeilen bekommen NULL fuer die
    neue Spalte (siehe README fuer die Auswirkung auf die Berechnung)."""
    with engine.connect() as conn:
        columns = _table_columns(conn, "readings")
        if "ac_power_w" not in columns:
            conn.exec_driver_sql("ALTER TABLE readings ADD COLUMN ac_power_w FLOAT")
            conn.commit()


def _ensure_readings_timestamp_index() -> None:
    """Eingefuehrt: 2026-07-19. Ergaenzt einen Index rein auf readings.timestamp (ohne device_id),
    falls er noch fehlt - fuer Bestandsdatenbanken von vor dieser Aenderung
    (bei einer frisch angelegten Tabelle ist er bereits ueber
    models.Reading.__table_args__ vorhanden)."""
    with engine.connect() as conn:
        if not _index_exists(conn, "readings", "ix_readings_timestamp"):
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_readings_timestamp ON readings (timestamp)"
            )
            conn.commit()


def _ensure_weather_hourly_extra_columns() -> None:
    """Eingefuehrt: 2026-08-19. Ergaenzt die Spalten fuer die zusaetzlichen Prognose-Wetterwerte
    (Bewoelkungsgrad, Wind, Luftfeuchtigkeit, Schneehoehe, Luftdruck - siehe
    forecast_weather.WeatherPoint) in bestehenden Datenbanken.

    Anders als bei _ensure_ac_power_column() werden hier zusaetzlich ALLE
    bereits gecachten Zeilen geloescht, statt sie mit NULL fuer die neuen
    Spalten stehen zu lassen: models.WeatherHourly-Zeilen gelten ab einem
    bestimmten Alter als "ausgereift" und werden NIE wieder ueberschrieben
    (siehe dortiger Docstring) - wuerden alte Zeilen dauerhaft ohne diese
    Werte im Trainingsfenster (TRAINING_DAYS = 365 Tage) verbleiben, wuerde
    das die gelernten Distanzgewichte (fit_distance_weights()) fuer die
    neuen Merkmale noch ueber ein volles Jahr hinweg verfaelschen. Die
    geloeschten Stunden werden beim naechsten Prognoselauf einfach mit
    vollstaendigen Werten neu von Open-Meteo geholt (siehe weather_cache.py)
    - einmaliger Mehraufwand, der die Korrektheit des Trainings sicherstellt."""
    with engine.connect() as conn:
        columns = _table_columns(conn, "weather_hourly")
        new_columns = {
            "cloud_cover_percent": "FLOAT",
            "wind_speed_ms": "FLOAT",
            "humidity_percent": "FLOAT",
            "snow_depth_m": "FLOAT",
            "pressure_hpa": "FLOAT",
        }
        missing = [name for name in new_columns if name not in columns]
        # Nur handeln, wenn die Tabelle schon (mit altem Schema) existierte -
        # bei einer frisch angelegten Tabelle (ueber create_all() oben) sind
        # die Spalten bereits vorhanden, "columns" waere dann nicht leer und
        # "missing" leer.
        if not columns or not missing:
            return
        for name, sql_type in new_columns.items():
            if name in missing:
                conn.exec_driver_sql(f"ALTER TABLE weather_hourly ADD COLUMN {name} {sql_type}")
        conn.exec_driver_sql("DELETE FROM weather_hourly")
        conn.commit()


def _ensure_pv_string_columns() -> None:
    """Eingefuehrt: 2026-09-08. Ergaenzt die Spalten readings.pv1_power_w/pv2_power_w/pv3_power_w
    (Leistung je einzelnem PV-String, siehe models.Reading), falls sie noch
    fehlen. Wie bei _ensure_ac_power_column() bekommen bestehende Zeilen
    NULL fuer die neuen Spalten, statt geloescht/neu berechnet zu werden -
    die Werte lassen sich aus vorhandenen Daten ohnehin nicht rueckwirkend
    rekonstruieren (frueher wurden die Strings gar nicht abgefragt)."""
    with engine.connect() as conn:
        columns = _table_columns(conn, "readings")
        for name in ("pv1_power_w", "pv2_power_w", "pv3_power_w"):
            if name not in columns:
                conn.exec_driver_sql(f"ALTER TABLE readings ADD COLUMN {name} FLOAT")
        conn.commit()
