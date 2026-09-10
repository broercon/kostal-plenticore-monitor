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
unterscheiden. Die Erkennung unten wurde anhand eines echten Exports
(Plenticore Plus mit Batterie, Format "Zeit\tDC1 U\tDC1 I\tDC1 P\t...")
empirisch gegen die Live-Werte im Dashboard abgeglichen. Bitte trotzdem
IMMER zuerst ohne --commit laufen lassen und die Vorschau plausibilisieren,
bevor du committest. Falls die PV-Spalten falsch erkannt werden, kannst du
sie mit --pv-columns "DC1 P,DC2 P" manuell vorgeben.

Die Datei enthaelt vor der eigentlichen Kopfzeile einen Metadaten-Block
(Titel, Wechselrichter-Nr., Name, aktuelle Zeit, Einheiten-Legende) -
dieser wird automatisch uebersprungen.

Einspeiseleistung UND Netzbezug lassen sich aus diesem Log-Format nicht
rekonstruieren - die Netzmessung kommt bei den meisten Installationen von
einem separaten Smart Energy Meter (KSEM), das nicht in den internen
Logger des Wechselrichters schreibt. Bei importierten Altdaten bleiben
diese beiden Felder daher leer; nur Hausverbrauch, PV-Leistung und
Batterie (Leistung + Ladezustand) werden befuellt.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
from pykoplenti import ApiClient

logger = logging.getLogger("import_logdata")

# Spaltennamen-Kandidaten (normalisiert: klein geschrieben, ohne "/", "_",
# Leerzeichen) pro Zielfeld. "sh1p"/"sc1p"/"hc2p" (SH1 P/SC1 P/HC2 P) und
# "soch" (SOC H) stammen aus einem echten Export und wurden gegen die
# Live-Werte im Dashboard verifiziert; die uebrigen sind Fallback-Kandidaten
# fuer eventuell abweichende Formate/Firmwarestaende.
TIMESTAMP_CANDIDATES = ["time", "timestamp", "zeit", "zeitstempel"]
HOME_POWER_CANDIDATES = ["sh1p", "sc1p", "hc2p", "achome0p", "homep", "achomep"]
HOME_POWER_SUM_PARTS = ["achomebatp", "achomepvp", "achomegridp"]
GRID_DRAW_CANDIDATES = ["achomegridp", "gridp"]
BATTERY_SOC_CANDIDATES = ["soch", "batsoc", "batterysoc", "soc"]
# Spalte, die bei vorhandener Batterie deren Leistung enthaelt (beim
# Plenticore i.d.R. der 3. DC-Eingang "DC3 P" - siehe Docstring oben).
BATTERY_POWER_CANDIDATES = ["dc3p", "batp", "batteryp"]


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

    # Vor der echten Kopfzeile steht bei manchen Geraeten ein Metadaten-Block
    # (Titel, Wechselrichter-Nr., Name, aktuelle Zeit, Einheiten-Legende...).
    # Wir suchen daher gezielt nach der Zeile, deren erste Spalte wie ein
    # Zeitstempel-Feld heisst, statt blind die erste Zeile zu nehmen.
    header: list[str] | None = None
    for candidate_row in reader:
        if not candidate_row:
            continue
        if _norm(candidate_row[0]) in TIMESTAMP_CANDIDATES:
            header = candidate_row
            break

    if header is None:
        return [], {
            "error": (
                "Konnte keine Kopfzeile finden (keine Zeile beginnt mit einem "
                f"der Zeitstempel-Kandidaten {TIMESTAMP_CANDIDATES})."
            ),
            "columns_found": [],
        }

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

    # Batterie nur dann annehmen, wenn ein Ladezustand (SoC) gefunden wurde -
    # sonst koennte ein "DC3 P"-aehnliches Feld auch ein drittes PV-String sein.
    has_battery = soc_idx is not None
    battery_power_idx = (
        _find_column(headers_norm, BATTERY_POWER_CANDIDATES) if has_battery else None
    )

    if pv_columns:
        pv_idxs = [i for i, h in enumerate(header) if h.strip() in pv_columns]
    else:
        pv_idxs = [
            i
            for i, hn in enumerate(headers_norm)
            if hn.startswith("dc") and hn.endswith("p")
        ]
        if battery_power_idx is not None and battery_power_idx in pv_idxs:
            pv_idxs.remove(battery_power_idx)

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
        battery_power = (
            _to_float(line[battery_power_idx])
            if battery_power_idx is not None and battery_power_idx < len(line)
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
                "battery_power_w": battery_power,
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
        "battery_power_column": (
            header[battery_power_idx] if battery_power_idx is not None else None
        ),
        "pv_columns": [header[i] for i in pv_idxs],
        "row_count": len(rows),
    }
    return rows, meta


async def _download(host: str, password: str, port: int, begin: datetime, end: datetime) -> str:
    # aiohttp.ClientSession() nutzt ohne explizite Angabe ein Standard-Timeout
    # von nur 5 Minuten fuer die komplette Anfrage. Bei einem sehr langen
    # Zeitraum (z.B. AUTO_IMPORT_DAYS=unbegrenzt) kann der Wechselrichter
    # (ein leistungsschwaches eingebettetes Geraet) laenger brauchen, um die
    # Logdatei zusammenzustellen - daher hier grosszuegiger.
    timeout = aiohttp.ClientTimeout(total=1800)  # 30 Minuten
    async with aiohttp.ClientSession(timeout=timeout) as session:
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
    parser.add_argument(
        "--raw-lines",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Nur die ersten N rohen Zeilen der heruntergeladenen Datei anzeigen "
            "(mit repr(), damit Tabs/Sonderzeichen sichtbar sind) und beenden - "
            "zum Herausfinden des tatsaechlichen Dateiformats, ohne zu parsen/speichern."
        ),
    )
    parser.add_argument(
        "--raw-tail",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Nur die letzten N rohen Zeilen der heruntergeladenen Datei anzeigen "
            "(die aktuellsten Messpunkte) und beenden - zum Abgleich mit den "
            "Live-Werten im Dashboard zum gleichen Zeitpunkt."
        ),
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

    if args.raw_lines > 0:
        lines = raw.splitlines()
        logger.info("--- Erste %d rohe Zeilen (repr) ---", args.raw_lines)
        for line in lines[: args.raw_lines]:
            logger.info(repr(line))
        return

    if args.raw_tail > 0:
        lines = raw.splitlines()
        logger.info("--- Letzte %d rohe Zeilen (repr) ---", args.raw_tail)
        for line in lines[-args.raw_tail :]:
            logger.info(repr(line))
        return

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

    inserted, updated, skipped = import_rows(
        args.device_id, args.device_name or args.device_id, rows
    )
    if inserted > 0 or updated > 0:
        # Der automatische Import invalidiert beide abgeleiteten Caches in
        # auto_import.py. Der hier dokumentierte manuelle --commit-Pfad muss
        # dasselbe tun, sonst bleiben bereits berechnete Tages- bzw.
        # Stundenwerte trotz der gerade importierten Rohdaten unveraendert.
        from .config import settings
        from .daily_summary import invalidate_energy_cache
        from .energy_forecast import invalidate_hourly_pv_cache

        local_tz = ZoneInfo(settings.timezone_name)
        imported_dates = [row["timestamp"].astimezone(local_tz).date() for row in rows]
        start_date = min(imported_dates)
        end_date = max(imported_dates)
        try:
            invalidate_energy_cache(start_date, end_date)
        except Exception:  # noqa: BLE001
            logger.exception("Konnte Energie-Zeitraum-Cache nach Import nicht invalidieren")
        try:
            invalidate_hourly_pv_cache(start_date, end_date)
        except Exception:  # noqa: BLE001
            logger.exception("Konnte stuendlichen PV-Historie-Cache nach Import nicht invalidieren")
    logger.info(
        "Import fertig: %d neue Zeilen gespeichert, %d bestehende Zeilen "
        "nachtraeglich befuellt, %d unveraendert.",
        inserted,
        updated,
        skipped,
    )


ROW_FIELDS = [
    "home_power_w",
    "grid_draw_power_w",
    "feed_in_power_w",
    "pv_power_w",
    "battery_soc_percent",
    "battery_power_w",
]


def import_rows(device_id: str, device_name: str, rows: list[dict]) -> tuple[int, int, int]:
    """Schreibt geparste Logdaten-Zeilen in die DB. Gibt (inserted, updated,
    skipped) zurueck.

    Fuer bereits vorhandene Zeitstempel werden NUR Felder nachtraeglich
    befuellt, die dort aktuell NULL sind (z.B. weil ein frueherer Import mit
    falscher Spalten-Erkennung lief) - echte/live erfasste Werte werden nie
    ueberschrieben.

    Wird sowohl vom CLI-Tool (main(), s.o.) als auch vom automatischen
    Hintergrund-Abgleich beim Start (app/auto_import.py) genutzt.
    """
    from sqlalchemy import select

    from .database import SessionLocal, init_db
    from .models import Reading

    init_db()
    session = SessionLocal()
    try:
        # SQLite gibt DateTime-Werte beim Zurücklesen als "naive" datetime
        # zurueck (ohne tzinfo), auch wenn wir sie tz-aware gespeichert haben.
        # Fuer den Abgleich auf beiden Seiten UTC-aware normalisieren.
        existing_by_ts: dict[datetime, Reading] = {}
        for reading in session.scalars(
            select(Reading).where(Reading.device_id == device_id)
        ):
            ts = reading.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            existing_by_ts[ts] = reading

        inserted = 0
        updated = 0
        skipped = 0
        for r in rows:
            ts = r["timestamp"]
            existing = existing_by_ts.get(ts)
            if existing is None:
                new_reading = Reading(
                    device_id=device_id,
                    device_name=device_name,
                    timestamp=ts,
                    **{f: r[f] for f in ROW_FIELDS},
                )
                session.add(new_reading)
                existing_by_ts[ts] = new_reading  # Schutz gegen Duplikate in derselben Datei
                inserted += 1
                continue

            changed = False
            for f in ROW_FIELDS:
                if getattr(existing, f) is None and r[f] is not None:
                    setattr(existing, f, r[f])
                    changed = True
            if changed:
                updated += 1
            else:
                skipped += 1

        session.commit()
        return inserted, updated, skipped
    finally:
        session.close()


if __name__ == "__main__":
    main()
