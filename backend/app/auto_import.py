"""Automatischer Hintergrund-Abgleich mit dem internen Datenlogger der
Wechselrichter beim Start der Anwendung.

Läuft im Hintergrund (blockiert das Bereitstellen der Web-Oberfläche
nicht) und importiert für jeden konfigurierten Wechselrichter die letzten
`AUTO_IMPORT_DAYS` Tage aus dessen internem Datenlogger. Dank der
Dedup-Logik in `import_logdata.py` ist das bei jedem Neustart gefahrlos
wiederholbar (bereits vorhandene Zeitstempel werden übersprungen) - so
werden z.B. Lücken durch Ausfallzeiten des Servers automatisch
nachträglich gefüllt, sobald er wieder läuft.

Für einen initialen Import weiter zurückliegender Daten (mehr als
`AUTO_IMPORT_DAYS` Tage) weiterhin `python -m app.import_logdata`
manuell mit einem größeren Zeitraum verwenden (siehe README).

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


def _parse_and_import(raw: str, device_id: str, device_name: str) -> tuple[int, int]:
    """Laeuft in einem Worker-Thread (siehe unten), da CSV-Parsing und die
    DB-Schreibvorgaenge synchron sind und sonst den Event-Loop blockieren
    wuerden."""
    rows, _meta = parse_logdata(raw)
    if not rows:
        return 0, 0
    return import_rows(device_id, device_name, rows)


async def _import_one_device(cfg: InverterConfig) -> None:
    tz = ZoneInfo(settings.timezone_name)
    end = datetime.now(tz)
    begin = end - timedelta(days=settings.auto_import_days)

    try:
        raw = await _download(cfg.host, cfg.password, cfg.port, begin, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Automatischer Logdaten-Abgleich für %s (%s) fehlgeschlagen: %s",
            cfg.name,
            cfg.host,
            exc,
        )
        return

    try:
        inserted, skipped = await asyncio.to_thread(
            _parse_and_import, raw, cfg.id, cfg.name
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Automatischer Logdaten-Abgleich für %s: Fehler beim Verarbeiten.", cfg.name
        )
        return

    logger.info(
        "Automatischer Logdaten-Abgleich für %s: %d neue Zeilen, %d bereits vorhanden "
        "(Zeitraum %s bis %s).",
        cfg.name,
        inserted,
        skipped,
        begin.date(),
        end.date(),
    )


async def run_auto_import_for_all_devices() -> None:
    if not settings.auto_import_enabled or not settings.inverters:
        return
    logger.info(
        "Starte automatischen Logdaten-Abgleich (letzte %d Tage) im Hintergrund ...",
        settings.auto_import_days,
    )
    for cfg in settings.inverters:
        await _import_one_device(cfg)
