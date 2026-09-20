"""Datenbank-Anbindung ueber SQLAlchemy (PostgreSQL).

Welche Datenbank verwendet wird, entscheidet settings.database_url
(Umgebungsvariable DATABASE_URL, Pflichtangabe - siehe config.py).
"""
from __future__ import annotations

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_url = make_url(settings.database_url)

if _url.get_backend_name() != "postgresql":
    raise RuntimeError(
        f"DATABASE_URL muss auf eine PostgreSQL-Datenbank zeigen, "
        f"bekommen habe ich '{_url.get_backend_name()}'. Siehe "
        f"docs/INSTALLATION.md, Abschnitt 'Datenbank einrichten'."
    )

# "postgresql://..." waehlt in SQLAlchemy standardmaessig den Treiber
# psycopg2; installiert ist hier aber psycopg (Version 3, siehe
# requirements.txt). Statt jeden Aufrufer zu zwingen, das laengere
# "postgresql+psycopg://" zu schreiben, wird der Treiber hier ergaenzt -
# so laesst sich ein fertiger Connection-String unveraendert uebernehmen.
if _url.get_driver_name() == "psycopg2":
    _url = _url.set(drivername="postgresql+psycopg")

engine = create_engine(
    _url,
    # Die Datenbank liegt in einem anderen Container und damit am Ende einer
    # Netzwerkverbindung, die zwischen zwei Pollings (Standard: 15s)
    # abreissen kann - etwa wenn der Datenbank-Container neu startet.
    # pool_pre_ping verwirft solche Verbindungen beim naechsten Zugriff,
    # statt dem Aufrufer einen Fehler durchzureichen.
    pool_pre_ping=True,
    # Alle Zeitstempel dieser Anwendung sind UTC (siehe models.py). Ohne
    # diese Festlegung liefert PostgreSQL timestamptz-Werte in der Zeitzone
    # des Servers zurueck - rechnerisch zwar derselbe Zeitpunkt, aber jede
    # Stelle, die einen Zeitstempel nur formatiert statt umzurechnen, saehe
    # dann Ortszeit. Auch date_trunc() richtet sich danach (siehe
    # energy_forecast._hour_bucket_expression).
    connect_args={"options": "-c timezone=UTC"},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Legt fehlende Tabellen an.

    Aendert bestehende Tabellen NICHT ab. Kommt mit einem Update ein neues
    Feld zu einem bestehenden Modell hinzu, braucht es dafuer einen
    ausdruecklichen Migrationsschritt - siehe docs/DEVELOPMENT.md, Abschnitt
    "Datenbank-Migrationen".
    """
    from . import models  # noqa: F401  (registriert die Modelle an Base)

    Base.metadata.create_all(bind=engine)
