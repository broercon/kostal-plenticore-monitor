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

Laesst sich zusaetzlich manuell ueber POST /api/admin/import-history
anstossen (siehe trigger_manual_import), z.B. um nach einer
Konfigurationsaenderung nicht extra den ganzen Container neu starten zu
muessen. GET /api/admin/import-history/status zeigt den aktuellen
Stand/das letzte Ergebnis (get_import_status).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import InverterConfig, settings
from .daily_summary import invalidate_energy_cache
from .energy_forecast import invalidate_hourly_pv_cache
from .import_logdata import _download, import_rows, parse_logdata

logger = logging.getLogger(__name__)

# Als "unbegrenzt" interpretierte Rueckschau, falls AUTO_IMPORT_DAYS=unbegrenzt
# gesetzt ist. 10 Jahre sind praktisch immer laenger als die Speichertiefe
# des internen Loggers - der Wechselrichter liefert dann einfach so viel
# zurueck, wie er tatsaechlich noch gespeichert hat.
UNLIMITED_LOOKBACK_DAYS = 3650

# Haelt Status/letztes Ergebnis des Hintergrund-Abgleichs vor, damit das
# Frontend (oder ein manueller API-Aufruf) sehen kann, ob gerade ein Lauf
# aktiv ist und wie der letzte ausgegangen ist - ohne dafuer die
# Container-Logs durchsuchen zu muessen.
_state: dict = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "results": [],  # Liste von {"device_name", "status", "message", "inserted", "updated", "skipped"}
}


def get_import_status() -> dict:
    return {**_state, "results": list(_state["results"])}


def _try_acquire_run() -> bool:
    """Prueft synchron (ohne await dazwischen, also race-frei innerhalb des
    Event-Loops) und setzt bei Erfolg sofort running=True - so kann sich
    kein zweiter, gleichzeitig gestarteter Lauf (z.B. zwei schnell
    hintereinander gedrueckte Klicks) dazwischenschieben, bevor der erste
    Lauf tatsaechlich zu arbeiten beginnt."""
    if _state["running"]:
        return False
    _state["running"] = True
    _state["last_started_at"] = datetime.now(timezone.utc)
    return True


def trigger_manual_import() -> bool:
    """Stoesst einen Abgleich sofort an (statt nur beim Container-Start) -
    unabhaengig von AUTO_IMPORT_HISTORY (das steuert nur den automatischen
    Lauf beim Start, nicht diesen expliziten manuellen Anstoss). Gibt False
    zurueck, wenn bereits ein Lauf aktiv ist (kein zweiter, parallel
    laufender Import - sonst koennten zwei Laeufe gleichzeitig in die
    SQLite-Datenbank schreiben)."""
    if not settings.inverters:
        return False
    if not _try_acquire_run():
        return False
    asyncio.create_task(_run_import_body())
    return True


def _parse_and_import(raw: str, device_id: str, device_name: str) -> tuple[int, int, int]:
    """Laeuft in einem Worker-Thread (siehe unten), da CSV-Parsing und die
    DB-Schreibvorgaenge synchron sind und sonst den Event-Loop blockieren
    wuerden."""
    rows, _meta = parse_logdata(raw)
    if not rows:
        return 0, 0, 0
    return import_rows(device_id, device_name, rows)


async def _import_one_device(cfg: InverterConfig) -> dict:
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
    result = {
        "device_id": cfg.id,
        "device_name": cfg.name,
        "range_begin": begin.date().isoformat(),
        "range_end": end.date().isoformat(),
    }
    try:
        raw = await _download(cfg.host, cfg.password, cfg.port, begin, end)
    except (TimeoutError, asyncio.TimeoutError):
        message = (
            "Zeitüberschreitung beim Herunterladen (auch nach 30 Minuten). Bei sehr "
            "langer Historie ggf. AUTO_IMPORT_DAYS auf einen kleineren Wert statt "
            "'unbegrenzt' setzen."
        )
        logger.warning(
            "Automatischer Logdaten-Abgleich für %s (%s) abgebrochen: %s",
            cfg.name,
            cfg.host,
            message,
        )
        return {**result, "status": "timeout", "message": message}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Automatischer Logdaten-Abgleich für %s (%s) fehlgeschlagen: %s",
            cfg.name,
            cfg.host,
            exc,
        )
        return {**result, "status": "error", "message": str(exc)}

    try:
        inserted, updated, skipped = await asyncio.to_thread(
            _parse_and_import, raw, cfg.id, cfg.name
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Automatischer Logdaten-Abgleich für %s: Fehler beim Verarbeiten.", cfg.name
        )
        return {**result, "status": "error", "message": f"Fehler beim Verarbeiten: {exc}"}

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
    return {
        **result,
        "status": "ok",
        "message": None,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


async def _run_import_body() -> None:
    """Gemeinsamer Ablauf fuer den automatischen Start-Import UND den
    manuellen Trigger. Erwartet, dass running bereits (synchron, ueber
    _try_acquire_run) auf True gesetzt wurde."""
    if settings.auto_import_days is None:
        logger.info(
            "Starte Logdaten-Abgleich (unbegrenzt zurueck, max. %d Tage) im Hintergrund ...",
            UNLIMITED_LOOKBACK_DAYS,
        )
    else:
        logger.info(
            "Starte Logdaten-Abgleich (letzte %d Tage) im Hintergrund ...",
            settings.auto_import_days,
        )
    try:
        results = []
        for cfg in settings.inverters:
            result = await _import_one_device(cfg)
            results.append(result)
            # Nur bei tatsaechlich neuen/nachtraeglich befuellten Zeilen die
            # betroffenen Tage aus dem Energie-Zeitraum-Cache werfen (siehe
            # daily_summary._cached_daily_totals) - ein Lauf, der wegen der
            # Dedup-Logik in import_logdata.py nichts Neues findet (der
            # Normalfall bei jedem Start), soll den Cache NICHT unnoetig
            # verwerfen.
            if result.get("status") == "ok" and (
                (result.get("inserted") or 0) > 0 or (result.get("updated") or 0) > 0
            ):
                try:
                    invalidate_energy_cache(
                        date.fromisoformat(result["range_begin"]),
                        date.fromisoformat(result["range_end"]),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Konnte Energie-Zeitraum-Cache nach Import fuer %s nicht invalidieren",
                        cfg.name,
                    )
                try:
                    invalidate_hourly_pv_cache(
                        date.fromisoformat(result["range_begin"]),
                        date.fromisoformat(result["range_end"]),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Konnte stuendlichen PV-Historie-Cache nach Import fuer %s nicht invalidieren",
                        cfg.name,
                    )
        _state["results"] = results
    finally:
        _state["running"] = False
        _state["last_finished_at"] = datetime.now(timezone.utc)


async def run_auto_import_for_all_devices() -> None:
    """Wird beim Container-Start aufgerufen (siehe main.py lifespan).
    Respektiert AUTO_IMPORT_HISTORY=false - der manuelle Trigger
    (trigger_manual_import) tut das bewusst NICHT, siehe dort."""
    if not settings.auto_import_enabled or not settings.inverters:
        return
    if not _try_acquire_run():
        logger.info("Logdaten-Abgleich uebersprungen: es laeuft bereits ein anderer Lauf.")
        return
    await _run_import_body()
