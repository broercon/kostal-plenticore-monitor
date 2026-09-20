"""Uebertraegt den Datenbestand aus einer SQLite-Datei nach PostgreSQL.

Einmaliges Werkzeug fuer den Umstieg von einer SQLite-Bestandsinstallation
(bis September 2026 der Standard, siehe docs/INSTALLATION.md, Abschnitt
"Fruehere Versionen mit SQLite") auf die inzwischen ausschliesslich
unterstuetzte PostgreSQL-Datenbank.

Aufruf im laufenden Container (DATABASE_URL muss wie gewohnt auf das ZIEL
zeigen; SQLITE_SOURCE_PATH auf die alte kostal.db - Standard: /app/data/kostal.db):

    docker compose exec -e DATABASE_URL=postgresql://... kostal-monitor \\
        python -m app.migrate_to_postgres

Das Skript ist bewusst WIEDERHOLBAR: es darf beliebig oft laufen und
liefert jedes Mal denselben Endzustand. Genau das braucht der Umstieg in
zwei Schritten - einmal jetzt, um den Bestand zu uebertragen und die
PostgreSQL-Seite in Ruhe gegenzupruefen, und ein zweites Mal beim
tatsaechlichen Umschalten, um die in der Zwischenzeit weiter erfassten
Messwerte nachzuziehen.

Dabei werden zwei Faelle unterschieden:

  * readings waechst nur hinten an (die id ist eine aufsteigende Sequenz
    und bestehende Zeilen werden nie veraendert). Hier werden ausschliesslich
    Zeilen NEUER als die hoechste bereits uebertragene id kopiert - der
    zweite Lauf dauert deshalb Sekunden statt Minuten.

  * Alle uebrigen Tabellen sind klein (zusammen deutlich unter 30.000
    Zeilen) und koennen sich rueckwirkend aendern (Caches werden nach einem
    Logdaten-Import gezielt geloescht, Einstellungen ueberschrieben). Sie
    werden schlicht geleert und vollstaendig neu uebertragen - das ist
    billig und schliesst jede Frage nach der richtigen Abgleichstrategie aus.

Die Quelle wird ausschliesslich LESEND geoeffnet. Die SQLite-Datei bleibt
unveraendert und damit jederzeit als Rueckfallebene nutzbar.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, create_engine, delete, func, insert, select

from . import models  # noqa: F401  (registriert die Modelle an Base)
from .database import Base, engine, init_db

logger = logging.getLogger(__name__)

# Zeilen je INSERT-Block. Gross genug, damit die 800.000 Messwerte nicht an
# der Zahl der Netzwerk-Umlaeufe scheitern, klein genug, dass immer nur ein
# ueberschaubarer Teil davon gleichzeitig im Speicher liegt.
BATCH_SIZE = 5_000

# Tabelle, die nur hinten anwaechst und deshalb per Delta uebertragen wird
# (siehe Modulkommentar).
APPEND_ONLY_TABLE = "readings"

# Frueherer Standardpfad der SQLite-Datei (siehe DB_PATH aus der Zeit vor
# dem PostgreSQL-Umstieg). Die Anwendung selbst kennt diesen Pfad nicht mehr
# (config.Settings hat kein db_path mehr) - hier bleibt er als Vorgabe fuer
# unveraenderte Bestandsinstallationen erhalten.
DEFAULT_SQLITE_PATH = "/app/data/kostal.db"


def _as_utc(value):
    """Zeitstempel als UTC-aware datetime.

    SQLite liefert DateTime(timezone=True)-Spalten naiv zurueck - der
    Vertrag dieser Anwendung ist aber durchgaengig UTC (siehe models.py).
    Ohne das Anheften wuerde PostgreSQL den naiven Wert in der Zeitzone der
    Sitzung auslegen; die Uebertragung haengt dann an einer Einstellung,
    die mit den Daten selbst nichts zu tun hat.
    """
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _copy_table(table, src_conn, dst_conn, *, since_id: int | None = None) -> int:
    """Kopiert eine Tabelle blockweise. Mit since_id nur die Zeilen darueber."""
    datetime_columns = [c.name for c in table.columns if isinstance(c.type, DateTime)]
    statement = select(table)
    if since_id is not None:
        statement = statement.where(table.c.id > since_id).order_by(table.c.id)

    result = src_conn.execution_options(stream_results=True, yield_per=BATCH_SIZE).execute(
        statement
    )
    copied = 0
    while True:
        chunk = result.fetchmany(BATCH_SIZE)
        if not chunk:
            break
        rows = []
        for row in chunk:
            values = dict(row._mapping)
            for name in datetime_columns:
                values[name] = _as_utc(values[name])
            rows.append(values)
        dst_conn.execute(insert(table), rows)
        copied += len(rows)
    return copied


def _reset_sequences(dst_conn) -> None:
    """Setzt die Sequenzen der SERIAL-Primaerschluessel auf den hoechsten
    uebernommenen Wert.

    Ohne diesen Schritt beginnt PostgreSQL beim naechsten INSERT wieder bei
    1 und laeuft sofort in eine Primaerschluessel-Verletzung, weil die
    Sequenz von den per Hand mitkopierten ids nichts weiss. Das faellt
    nicht beim Umschalten auf, sondern erst beim ersten neu erfassten
    Messwert - deshalb gehoert es fest in die Migration."""
    for table in Base.metadata.sorted_tables:
        if "id" not in table.c:
            continue
        # Liefert NULL, wenn hinter der Spalte gar keine Sequenz steht -
        # dann gibt es auch nichts anzugleichen.
        sequence = dst_conn.execute(
            select(func.pg_get_serial_sequence(table.name, "id"))
        ).scalar()
        if sequence is None:
            continue
        highest = dst_conn.execute(select(func.max(table.c.id))).scalar()
        if highest is None:
            continue
        dst_conn.execute(select(func.setval(sequence, highest)))
        logger.info("  Sequenz %s.id auf %s gesetzt", table.name, highest)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # database.py verlangt seit dem PostgreSQL-Umstieg bereits beim Import
    # ein DATABASE_URL, das auf PostgreSQL zeigt (sonst schlaegt schon der
    # "from .database import ..." oben fehl) - eine gesonderte Pruefung des
    # Ziels ist hier also nicht mehr noetig.
    sqlite_path = Path(os.environ.get("SQLITE_SOURCE_PATH", DEFAULT_SQLITE_PATH))
    if not sqlite_path.is_file():
        logger.error(
            "Quelldatenbank %s nicht gefunden. Pfad ueber SQLITE_SOURCE_PATH setzen.",
            sqlite_path,
        )
        return 1

    logger.info("Quelle: %s", sqlite_path)
    logger.info("Ziel:   %s", engine.url.render_as_string(hide_password=True))

    # Fehlende Tabellen im Ziel anlegen - genau dieselbe Routine wie beim
    # normalen App-Start.
    init_db()

    # Ausdruecklich NUR-LESEND geoeffnet (mode=ro): die Quelldatei ist
    # waehrend der Uebertragung u.U. noch die produktive Datenbank einer
    # laufenden App - dieses Skript darf sie unter keinen Umstaenden
    # veraendern.
    source = create_engine(
        f"sqlite:///file:{sqlite_path}?mode=ro&uri=true",
        connect_args={"check_same_thread": False},
    )

    replaced_tables = [t for t in Base.metadata.sorted_tables if t.name != APPEND_ONLY_TABLE]

    total = 0
    with source.connect() as src_conn, engine.begin() as dst_conn:
        # Erst ALLE zu ersetzenden Tabellen leeren, und zwar in umgekehrter
        # Abhaengigkeitsreihenfolge (sorted_tables ist nach Fremdschluesseln
        # sortiert: uebergeordnete Tabelle zuerst - fuers Loeschen gilt
        # genau die umgekehrte Reihenfolge). Wuerde man je Tabelle einzeln
        # "leeren und fuellen", schlueg das Leeren von users fehl, solange
        # sessions noch darauf verweist. Unter SQLite faellt das nicht auf,
        # weil dort Fremdschluessel ohne "PRAGMA foreign_keys=ON" gar nicht
        # durchgesetzt werden - PostgreSQL prueft sie immer.
        for table in reversed(replaced_tables):
            dst_conn.execute(delete(table))

        for table in Base.metadata.sorted_tables:
            if table.name == APPEND_ONLY_TABLE:
                since_id = dst_conn.execute(select(func.max(table.c.id))).scalar()
                copied = _copy_table(table, src_conn, dst_conn, since_id=since_id or 0)
                logger.info(
                    "%-24s %8d neue Zeilen (ab id > %s)",
                    table.name, copied, since_id or 0,
                )
            else:
                copied = _copy_table(table, src_conn, dst_conn)
                logger.info("%-24s %8d Zeilen (vollstaendig ersetzt)", table.name, copied)
            total += copied

        logger.info("Sequenzen angleichen ...")
        _reset_sequences(dst_conn)

    # Gegenprobe: Zeilenzahlen beider Seiten muessen uebereinstimmen.
    logger.info("\nGegenprobe:")
    mismatch = False
    with source.connect() as src_conn, engine.connect() as dst_conn:
        for table in Base.metadata.sorted_tables:
            a = src_conn.execute(select(func.count()).select_from(table)).scalar()
            b = dst_conn.execute(select(func.count()).select_from(table)).scalar()
            if a != b:
                mismatch = True
            logger.info(
                "  %-24s SQLite %8d | PostgreSQL %8d  %s",
                table.name, a, b, "OK" if a == b else "ABWEICHUNG",
            )

    if mismatch:
        logger.error(
            "\nMindestens eine Tabelle weicht ab. Das ist normal, wenn die App "
            "waehrend der Uebertragung weiter Messwerte geschrieben hat - dann "
            "dieses Skript bei gestoppter App noch einmal laufen lassen."
        )
        return 1

    logger.info("\n%d Zeilen uebertragen, alle Tabellen stimmen ueberein.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
