"""Einmaliges Werkzeug, um historische Messwerte aus dem internen
Datenlogger eines Plenticore-Wechselrichters in die App-Datenbank zu
importieren ("Migration" alter Daten).

Nutzung (innerhalb des laufenden Containers):

    docker compose exec kostal-monitor python -m app.import_logdata \\
        --host 192.168.1.50 --password DEIN_PASSWORT \\
        --device-id wr1 --begin 2026-06-01 --end 2026-07-10

Standardmaessig ist es ein Dry-Run: es wird nur eine Vorschau angezeigt
(gefundene Spalten, erkannte Zuordnung, Anzahl Datenpunkte, erste/letzte
Werte), nichts wird gespeichert. Erst mit zusaetzlichem Flag --commit
werden die Daten wirklich in die SQLite-Datenbank geschrieben. Ein erneuter
Lauf ueberspringt bereits vorhandene Zeitstempel (kein doppelter Import).

WICHTIGER HINWEIS: Das genaue Spaltenformat des Kostal-Logdaten-Exports ist
nicht offiziell dokumentiert und kann sich je nach Geraet/Firmware
unterscheiden (diese Zuordnung basiert auf Community-Berichten, nicht auf
Kostal-Dokumentation). Bitte IMMER zuerst ohne --commit laufen lassen und
die Vorschau mit den Live-Werten im Dashboard plausibilisieren, bevor du
committest. Falls die PV-Spalten falsch erkannt werden, kannst du sie mit
--pv-columns "DC0/P,DC1/P" manuell vorgeben.

Einspeiseleistung (feed_in_power_w) laesst sich aus dem Log-Format nicht
zuverlaessig auftrennen und bleibt bei importierten Altdaten daher leer -
nur Hausverbrauch, PV-Leistung, Netzbezug und Batterie-Ladezustand werden
befuellt.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
from datetime import datetime, timezone

import aiohttp
from pykoplenti import ApiClient

logger = logging.getLogger("import_logdata")

# Spaltennamen-Kandidaten (normalisiert: klein geschrieben, ohne "/", "_",
# Leerzeichen) pro Zielfeld, basierend auf Community-Berichten zum Kostal
# Plenticore Logdaten-Export. Reihenfolge = Prioritaet.
TIMESTAMP_CANDIDATES = ["time", "timestamp", "zeit", "zeitstempel"]
HOME_POWER_CANDIDATES = ["achome0p", "homep", "achomep"]
HOME_POWER_SUM_PARTS = ["achomebatp", "achomepvp", "achomegridp"]
GRID_DRAW_CANDIDATES = ["achomegridp", "gridp"]
BATTERY_SOC_CANDIDATES = ["batsoc", "batterysoc", "soc"]


def _norm(col: str) -> str:
    return col.strip().lower().replace("/", "").replace("_", "").replace(" ", "")


def _find_column(headers_norm: list[str], candidates: list[str]) -> int | None:
    for cand in candidates:
        if cand in headers_norm:
            return headers_norm.index(cand)
    return None


def _to_float(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_logdata(
    raw: str, pv_columns: list[str] | None = None
) -> tuple[list[dict], dict]:
    """Parst die Tab-separierte Logdatei.

    Gibt (rows, meta) zurueck. rows: Liste von dicts mit timestamp (UTC
    datetime), home_power_w, grid_draw_power_w, pv_power_w,
    battery_soc_percent (jeweils float|None). meta: Infos zur erkannten
    Spalten-Zuordnung, fuer die Vorschau.
    """
    reader = csv.reader(io.StringIO(raw), delimiter="\t")
    try:
        header = next(reader)
    except StopIteration:
        return [], {"error": "Datei ist leer", "columns_found": []}

    headers_norm = [_norm(h) for h in header]

    ts_idx = _find_column(headers_norm, TIMESTAMP_CANDIDATES)
    if ts_idx is None:
        ts_idx = 0  # Fallback: erste Spalte ist ueblicherweise der Zeitstempel

    home_idx = _find_column(headers_norm, HOME_POWER_CANDIDATES)
    home_sum_idxs = [
        headers_norm.index(c) for c in HOME_POWER_SUM_PARTS if c in headers_norm
    ]
    grid_draw_idx = _find_column(headers_norm, GRID_DRAW_CANDIDATES)
    soc_idx = _find_column(headers_norm, BATTERY_SOC_CANDIDATES)

    if pv_columns:
        pv_idxs = [i for i, h in enumerate(header) if h.strip() in pv_columns]
    else:
        pv_idxs = [
            i
            for i, hn in enumerate(headers_norm)
            if hn.startswith("dc") and hn.endswith("p")
        ]

    rows: list[dict] = []
    for line in reader:
        if not line or len(line) <= ts_idx:
            continue
        raw_ts = line[ts_idx].strip()
        if not raw_ts:
            continue
        try:
            ts = datetime.fromtimestamp(int(float(raw_ts)), tz=timezone.utc)
        except (ValueError, OSError):
            continue

        home_power = (
            _to_float(line[home_idx])
            if home_idx is not None and home_idx < len(line)
            else None
        )
        if home_power is None and home_sum_idxs:
            parts = [_to_float(line[i]) for i in home_sum_idxs if i < len(line)]
            parts = [p for p in parts if p is not None]
            home_power = sum(parts) if parts else None

        grid_draw = (
            _to_float(line[grid_draw_idx])
            if grid_draw_idx is not None and grid_draw_idx < len(line)
            else None
        )
        soc = (
            _to_float(line[soc_idx])
            if soc_idx is not None and soc_idx < len(line)
            else None
        )

        pv_parts = [_to_float(line[i]) for i in pv_idxs if i < len(line)]
        pv_parts = [p for p in pv_parts if p is not None]
        pv_power = sum(pv_parts) if pv_parts else None

        rows.append(
            {
                "timestamp": ts,
                "home_power_w": home_power,
                "grid_draw_power_w": grid_draw,
                "feed_in_power_w": None,
                "pv_power_w": pv_power,
                "battery_soc_percent": soc,
                "battery_power_w": None,
            }
        )

    meta = {
        "columns_found": header,
        "timestamp_column": header[ts_idx] if ts_idx < len(header) else None,
        "home_power_column": (
            header[home_idx]
            if home_idx is not None
            else (
                "Summe aus: " + ", ".join(header[i] for i in home_sum_idxs)
                if home_sum_idxs
                else None
            )
        ),
        "grid_draw_column": header[grid_draw_idx] if grid_draw_idx is not None else None,
        "battery_soc_column": header[soc_idx] if soc_idx is not None else None,
        "pv_columns": [header[i] for i in pv_idxs],
        "row_count": len(rows),
    }
    return rows, meta


async def _download(host: str, password: str, port: int, begin: datetime, end: datetime) -> str:
    async with aiohttp.ClientSession() as session:
        client = ApiClient(session, host, port=port)
        await client.login(password)
        buf = io.StringIO()
        await client.download_logdata(buf, begin=begin, end=end)
        return buf.getvalue()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", required=True, help="IP/Hostname des Wechselrichters")
    parser.add_argument("--password", required=True)
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument(
        "--device-id", required=True, help="Muss zu einer ID in config/inverters.json passen"
    )
    parser.add_argument("--device-name", default=None)
    parser.add_argument("--begin", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--pv-columns",
        default=None,
        help="Kommagetrennte Spaltennamen fuer PV-Leistung, falls Auto-Erkennung falsch liegt",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Ohne dieses Flag: nur Vorschau (Dry-Run), nichts wird gespeichert",
    )
    args = parser.parse_args()

    begin = datetime.strptime(args.begin, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    pv_columns = (
        [c.strip() for c in args.pv_columns.split(",")] if args.pv_columns else None
    )

    logger.info("Lade Logdaten von %s (%s bis %s) ...", args.host, args.begin, args.end)
    raw = asyncio.run(_download(args.host, args.password, args.port, begin, end))
    logger.info("Heruntergeladen: %d Zeichen", len(raw))

    rows, meta = parse_logdata(raw, pv_columns=pv_columns)

    logger.info("--- Vorschau ---")
    logger.info("Gefundene Spalten: %s", meta.get("columns_found"))
    logger.info("Zeitstempel-Spalte: %s", meta.get("timestamp_column"))
    logger.info("Hausverbrauch-Spalte(n): %s", meta.get("home_power_column"))
    logger.info("Netzbezug-Spalte: %s", meta.get("grid_draw_column"))
    logger.info("Batterie-SoC-Spalte: %s", meta.get("battery_soc_column"))
    logger.info("PV-Spalten (werden summiert): %s", meta.get("pv_columns"))
    logger.info("Anzahl Datenpunkte: %d", meta.get("row_count", 0))

    if not rows:
        logger.warning("Keine Datenpunkte gefunden - nichts zu importieren.")
        return

    logger.info("Erste Datenpunkte:")
    for r in rows[:3]:
        logger.info("  %s", r)
    logger.info("Letzter Datenpunkt: %s", rows[-1])

    if not args.commit:
        logger.info("")
        logger.info(
            "Dies war nur eine Vorschau (Dry-Run). Wenn die Zahlen plausibel "
            "aussehen (z.B. Vergleich mit dem Live-Dashboard), erneut mit "
            "--commit aufrufen, um wirklich zu speichern."
        )
        return

    # Import in die DB
    from sqlalchemy import select

    from .database import SessionLocal, init_db
    from .models import Reading

    init_db()
    session = SessionLocal()
    try:
        # SQLite gibt DateTime-Werte beim Zurücklesen als "naive" datetime
        # zurueck (ohne tzinfo), auch wenn wir sie tz-aware gespeichert haben.
        # Fuer den Duplikat-Check auf beiden Seiten UTC-aware normalisieren.
        existing = set()
        for ts in session.scalars(
            select(Reading.timestamp).where(Reading.device_id == args.device_id)
        ):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            existing.add(ts)
        device_name = args.device_name or args.device_id
        inserted = 0
        skipped = 0
        for r in rows:
            if r["timestamp"] in existing:
                skipped += 1
                continue
            session.add(
                Reading(
                    device_id=args.device_id,
                    device_name=device_name,
                    timestamp=r["timestamp"],
                    home_power_w=r["home_power_w"],
                    grid_draw_power_w=r["grid_draw_power_w"],
                    feed_in_power_w=r["feed_in_power_w"],
                    pv_power_w=r["pv_power_w"],
                    battery_soc_percent=r["battery_soc_percent"],
                    battery_power_w=r["battery_power_w"],
                )
            )
            inserted += 1
        session.commit()
        logger.info(
            "Import fertig: %d neue Zeilen gespeichert, %d bereits vorhandene uebersprungen.",
            inserted,
            skipped,
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
