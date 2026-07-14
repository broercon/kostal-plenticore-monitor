"""Täglicher Zusammenfassungs-Report per Mail.

Verschickt einmal am Tag (konfigurierbare Uhrzeit) eine Mail mit einem
Schnappschuss der Anlage – welche Wechselrichter aktiv/erreichbar waren
und wie viel PV-Ertrag sie (einzeln + in Summe) an diesem Tag bereits
erzielt haben – an die konfigurierten Empfänger, verschickt über den
zentralen Mail-Service (siehe broercon/Mailserver).

Die gesamte Konfiguration (aktiv/inaktiv, Uhrzeit, Empfänger, Mail-Service-
URL/API-Key/Absendername) kommt aus daily_report_config.get_config() und
wird bei JEDEM Scheduler-Zyklus frisch gelesen - eine Änderung über die
Admin-Oberfläche (GET/PUT /api/admin/daily-report/config) wirkt also ohne
Container-Neustart, spätestens beim nächsten Zyklus (max. 60s, wenn der
Report gerade deaktiviert ist/unvollständig konfiguriert war).

Lässt sich zusätzlich manuell über POST /api/admin/daily-report/trigger
anstoßen (z.B. um eine gerade gespeicherte Konfiguration sofort zu testen,
unabhängig vom "Aktiv"-Schalter) – GET /api/admin/daily-report/status zeigt
den Stand des letzten Versands/Fehlers.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings
from .daily_report_config import InvalidReportTime, get_config, parse_report_time
from .daily_summary import build_daily_summaries, device_online_map
from .report_mailer import ReportMailError, send_report_mail
from .schemas import SummaryOut

logger = logging.getLogger(__name__)

_state: dict = {
    "last_sent_at": None,
    "last_status": None,  # "ok" | "error" | None (noch nie gelaufen)
    "last_message": None,
}

# Wie oft im "deaktiviert/unvollständig konfiguriert"-Zustand erneut
# geprüft wird, ob sich das über die Admin-Oberfläche geändert hat.
_RECHECK_INTERVAL_SECONDS = 60.0


def get_daily_report_status() -> dict:
    return dict(_state)


def next_run_at(now: datetime, hour: int, minute: int, tz_name: str) -> datetime:
    """Nächster Zeitpunkt (als UTC-datetime), zu dem die konfigurierte
    Uhrzeit (hour:minute, lokale tz_name) erreicht wird – heute, falls diese
    Uhrzeit heute noch nicht erreicht ist, sonst morgen. Reine Funktion
    (keine Seiteneffekte), damit sie sich isoliert testen lässt."""
    tz = ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _format_kwh(value: float | None) -> str:
    return f"{value:.2f} kWh" if value is not None else "keine Daten"


def build_report_text(
    summaries: list[SummaryOut], online_map: dict[str, bool], now: datetime, tz_name: str
) -> tuple[str, str]:
    """Baut (subject, body) für die tägliche Zusammenfassungsmail als
    Klartext. Rein funktional, damit sich das Format unabhängig vom
    eigentlichen Mailversand testen lässt."""
    tz = ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    date_str = local_now.strftime("%d.%m.%Y")

    device_summaries = [s for s in summaries if s.device_id != "_all_"]
    combined = next((s for s in summaries if s.device_id == "_all_"), None)

    lines = [
        f"Tageszusammenfassung {date_str} - Kostal Plenticore Monitor",
        f"(Stand: {local_now.strftime('%H:%M')} Uhr)",
        "",
        "Wechselrichter:",
    ]
    if not device_summaries:
        lines.append("  (keine Wechselrichter konfiguriert)")
    for s in device_summaries:
        status = "aktiv" if online_map.get(s.device_id, False) else "NICHT ERREICHBAR"
        lines.append(
            f"  - {s.device_name}: {status}, PV-Ertrag heute: {_format_kwh(s.yield_day_kwh)}"
        )

    lines.append("")
    if combined is not None:
        lines.append(
            f"Gesamt-PV-Ertrag heute (alle Wechselrichter): {_format_kwh(combined.yield_day_kwh)}"
        )
        lines.append(f"Hausverbrauch heute: {_format_kwh(combined.home_consumption_day_kwh)}")
    elif device_summaries:
        only = device_summaries[0]
        lines.append(f"PV-Ertrag heute: {_format_kwh(only.yield_day_kwh)}")
        lines.append(f"Hausverbrauch heute: {_format_kwh(only.home_consumption_day_kwh)}")

    active_count = sum(1 for s in device_summaries if online_map.get(s.device_id, False))
    lines.append("")
    lines.append(f"{active_count} von {len(device_summaries)} Wechselrichter(n) aktiv.")

    subject = f"Kostal Plenticore Monitor - Tageszusammenfassung {date_str}"
    body = "\n".join(lines)
    return subject, body


async def generate_and_send_daily_report() -> dict:
    """Baut den aktuellen Snapshot und verschickt ihn – gemeinsam genutzt
    vom Scheduler (zur konfigurierten Uhrzeit) und dem manuellen
    Trigger-Endpoint. Liest die Konfiguration frisch (nicht den
    "enabled"-Schalter beachtend - wer das explizit aufruft, will testen/
    verschicken, unabhängig davon, ob der automatische Versand gerade
    aktiviert ist) und fängt JEDEN Fehler ab (Konfigurationsfehler,
    Mail-Service nicht erreichbar o.ä.), statt ihn weiterzureichen – ein
    fehlgeschlagener Report darf den Scheduler-Loop nicht beenden."""
    now = datetime.now(timezone.utc)
    cfg = get_config()
    try:
        summaries = build_daily_summaries()
        online_map = device_online_map(now=now)
        subject, body = build_report_text(summaries, online_map, now, settings.timezone_name)
        await send_report_mail(subject, body, cfg=cfg)
    except ReportMailError as exc:
        message = str(exc)
        logger.warning("Täglicher Mail-Report fehlgeschlagen: %s", message)
        _state.update(last_sent_at=now, last_status="error", last_message=message)
        return {"sent": False, "message": message}
    except Exception as exc:  # noqa: BLE001
        message = f"Unerwarteter Fehler: {exc}"
        logger.exception("Täglicher Mail-Report: unerwarteter Fehler")
        _state.update(last_sent_at=now, last_status="error", last_message=message)
        return {"sent": False, "message": message}

    message = f"Report an {', '.join(cfg['recipients'])} verschickt."
    logger.info(message)
    _state.update(last_sent_at=now, last_status="ok", last_message=message)
    return {"sent": True, "message": message}


class DailyReportScheduler:
    """Hintergrund-Task, der einmal täglich zur konfigurierten Uhrzeit
    generate_and_send_daily_report() anstößt. Analog zu poller.Poller im
    Aufbau (start/stop mit sauberem Herunterfahren über stop_event), damit
    es sich genauso in main.py's lifespan einhängen lässt.

    Anders als der Poller läuft dieser Task IMMER (auch wenn der Report
    gerade deaktiviert/unvollständig konfiguriert ist) - er prüft die
    Konfiguration bei jedem Zyklus neu (siehe _run), damit eine Aktivierung
    über die Admin-Oberfläche ohne Container-Neustart innerhalb kurzer Zeit
    wirksam wird."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Mail-Report-Scheduler gestartet (Konfiguration wird laufend aus "
            "der Admin-Oberfläche bzw. den Umgebungsvariablen gelesen)."
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task

    async def _wait_or_stop(self, seconds: float) -> bool:
        """Wartet bis zu `seconds` Sekunden, bricht aber sofort ab, wenn
        stop() aufgerufen wird. Gibt True zurück, wenn stop() den Abbruch
        ausgelöst hat (Aufrufer soll dann die Schleife sofort verlassen)."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            cfg = get_config()
            if not cfg["enabled"] or not cfg["recipients"] or not cfg["mail_service_url"]:
                if await self._wait_or_stop(_RECHECK_INTERVAL_SECONDS):
                    return
                continue

            try:
                hour, minute = parse_report_time(cfg["report_time"])
            except InvalidReportTime:
                logger.warning(
                    "Ungültige Uhrzeit %r für den Mail-Report - prüfe in %ds erneut.",
                    cfg["report_time"],
                    int(_RECHECK_INTERVAL_SECONDS),
                )
                if await self._wait_or_stop(_RECHECK_INTERVAL_SECONDS):
                    return
                continue

            now = datetime.now(timezone.utc)
            target = next_run_at(now, hour, minute, settings.timezone_name)
            wait_seconds = max(0.0, (target - now).total_seconds())
            if await self._wait_or_stop(wait_seconds):
                return  # stop() wurde während des Wartens aufgerufen

            # Direkt vor dem Versand nochmal prüfen: waehrend des (ggf. bis
            # zu 24h langen) Wartens könnte "enabled" über die
            # Admin-Oberfläche wieder deaktiviert worden sein.
            cfg = get_config()
            if not cfg["enabled"]:
                continue

            try:
                await generate_and_send_daily_report()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # generate_and_send_daily_report() fängt selbst schon alles
                # ab - dieser Block ist nur ein letztes Sicherheitsnetz,
                # analog zum Poller, damit ein wirklich unerwarteter Fehler
                # nicht den ganzen täglichen Report für immer beendet.
                logger.exception("Unerwarteter Fehler im Mail-Report-Scheduler")


daily_report_scheduler = DailyReportScheduler()
