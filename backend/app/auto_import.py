"""Automatischer Hintergrund-Abgleich mit dem internen Datenlogger der
Wechselrichter beim Start der Anwendung.

Läuft im Hintergrund (blockiert das Bereitstellen der Web-Oberfläche
nicht) und importiert für jeden konfigurierten Wechselrichter die letzten
`AUTO_IMPORT_DAYS` Tage aus dessen internem Datenlogger. Dank der
Dedup-/Selbstheilungs-Logik in `import_logdata.py` ist das bei jedem
Neustart gefahrlos wiederholbar (bereits vollstaendig befuellte
Zeitstempel werden uebersprungen) - so werden z.B. Luecken durch
Ausfallzeiten des Servers automatisch nachtraeglich gefuellt, sobald er
wieder laeuft.

Mit `AUTO_IMPORT_DAYS=unbegrenzt` (oder "0"/"all") wird stattdessen so
weit wie moeglich zurueck abgeglichen (siehe UNLIMITED_LOOKBACK_DAYS
unten) - der Wechselrichter liefert dann ohnehin nur so viel Historie, wie
sein interner Logger tatsaechlich noch vorhaelt, ein zu weit reichendes
Anfrage-Datum ist also unproblematisch.

Kann über die Umgebungsvariable AUTO_IMPORT_HISTORY=false komplett
deaktiviert werden.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import InverterConfig, settings
from .import_logdata import _download, import_rows, parse_logdata

logger = logging.getLogger(__name__)

# Als "unbegrenzt" interpretierte Rueckschau, falls AUTO_IMPORT_DAYS=unbegrenzt
# gesetzt ist. 10 Jahre sind praktisch immer laenger als die Speichertiefe
# des internen Loggers - der Wechselrichter liefert dann einfach so viel
# zurueck, wie er tatsaechlich noch gespeichert hat.
UNLIMITED_LOOKBACK_DAYS = 3650


def _parse_and_import(raw: str, device_id: str, device_name: str) -> tuple[int, int, int]:
    """Laeuft in einem Worker-Thread (siehe unten), da CSV-Parsing und die
    DB-Schreibvorgaenge synchron sind und sonst den Event-Loop blockieren
    wuerden."""
    rows, _meta = parse_logdata(raw)
    if not rows:
        return 0, 0, 0
    return import_rows(device_id, device_name, rows)


async def _import_one_device(cfg: InverterConfig) -> None:
    tz = ZoneInfo(settings.timezone_name)
    end = datetime.now(tz)
    lookback_days = (
        UNLIMITED_LOOKBACK_DAYS if settings.auto_import_days is None else settings.auto_import_days
    )
    begin = end - timedelta(days=lookback_days)

    logger.info(
        "Lade Logdaten fuer %s (%s bis %s) - bei sehr langer Historie (z.B. "
        "AUTO_IMPORT_DAYS=unbegrenzt) kann das je nach Wechselrichter mehrere "
        "Minuten dauern ...",
        cfg.name,
        begin.date(),
        end.date(),
    )
    try:
        raw = await _download(cfg.host, cfg.password, cfg.port, begin, end)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "Automatischer Logdaten-Abgleich für %s (%s) abgebrochen: Zeitüberschreitung "
            "beim Herunterladen (auch nach 30 Minuten). Bei sehr langer Historie ggf. "
            "AUTO_IMPORT_DAYS auf einen kleineren Wert statt 'unbegrenzt' setzen.",
            cfg.name,
            cfg.host,
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Automatischer Logdaten-Abgleich für %s (%s) fehlgeschlagen: %s",
            cfg.name,
            cfg.host,
            exc,
        )
        return

    try:
        inserted, updated, skipped = await asyncio.to_thread(
            _parse_and_import, raw, cfg.id, cfg.name
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Automatischer Logdaten-Abgleich für %s: Fehler beim Verarbeiten.", cfg.name
        )
        return

    logger.info(
        "Automatischer Logdaten-Abgleich für %s: %d neue Zeilen, %d nachtraeglich "
        "befuellt, %d unveraendert (Zeitraum %s bis %s).",
        cfg.name,
        inserted,
        updated,
        skipped,
        begin.date(),
        end.date(),
    )


async def run_auto_import_for_all_devices() -> None:
    if not settings.auto_import_enabled or not settings.inverters:
        return
    if settings.auto_import_days is None:
        logger.info(
            "Starte automatischen Logdaten-Abgleich (unbegrenzt zurueck, max. %d Tage) "
            "im Hintergrund ...",
            UNLIMITED_LOOKBACK_DAYS,
        )
    else:
        logger.info(
            "Starte automatischen Logdaten-Abgleich (letzte %d Tage) im Hintergrund ...",
            settings.auto_import_days,
        )
    for cfg in settings.inverters:
        await _import_one_device(cfg)
