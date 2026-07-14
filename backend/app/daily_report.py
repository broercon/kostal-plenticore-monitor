"""Täglicher Zusammenfassungs-Report per Mail.

Verschickt einmal am Tag (konfigurierbare Uhrzeit) eine gestaltete
HTML-Mail mit einem Schnappschuss der Anlage: welche Wechselrichter
aktiv/erreichbar waren, wie viel PV-Ertrag sie (einzeln + in Summe) an
diesem Tag bereits erzielt haben, den PV-Ertrag über mehrere Zeiträume
(wie die "PV-Ertrag"-Tabelle im Dashboard), der heutige Hausverbrauch
aufgeschlüsselt nach PV/Batterie/Netz (wie das "Tagesverbrauch"-Diagramm)
sowie der aktuelle Batterie-Ladestand – verschickt an die konfigurierten
Empfänger über den zentralen Mail-Service (siehe broercon/Mailserver).

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
import html as html_lib
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings
from .daily_report_config import InvalidReportTime, get_config, parse_report_time
from .daily_summary import (
    build_daily_home_breakdown,
    build_daily_summaries,
    build_pv_yield_summary,
    device_battery_snapshot,
    device_online_map,
)
from .report_mailer import ReportMailError, send_report_mail
from .schemas import DailyHomeBreakdownDay, FeedInPeriod, SummaryOut

logger = logging.getLogger(__name__)

_state: dict = {
    "last_sent_at": None,
    "last_status": None,  # "ok" | "error" | None (noch nie gelaufen)
    "last_message": None,
}

# Wie oft im "deaktiviert/unvollständig konfiguriert"-Zustand erneut
# geprüft wird, ob sich das über die Admin-Oberfläche geändert hat.
_RECHECK_INTERVAL_SECONDS = 60.0

# --- Layout-Konstanten für die HTML-Mail (inline Styles, damit sie in
# moeglichst vielen Mail-Clients korrekt ankommen - kein <style>-Block,
# kein Flexbox/Grid, nur Block-/Inline-Block-Elemente mit expliziten
# Breiten). ---
_COLOR_BG = "#f1f5f9"
_COLOR_CARD = "#ffffff"
_COLOR_BORDER = "#e2e8f0"
_COLOR_TEXT = "#0f172a"
_COLOR_MUTED = "#64748b"
_COLOR_HEADER_BG = "#0f172a"
_COLOR_PV = "#16a34a"
_COLOR_BATTERY = "#2563eb"
_COLOR_GRID = "#ea580c"
_COLOR_OK = "#16a34a"
_COLOR_FAIL = "#dc2626"

_PERIOD_LABELS = {
    "today": "Heute",
    "yesterday": "Gestern",
    "day_before_yesterday": "Vorgestern",
    "this_week": "Diese Woche",
    "last_week": "Letzte Woche",
    "this_month": "Dieser Monat",
    "last_month": "Letzter Monat",
    "this_year": "Dieses Jahr",
    "last_year": "Letztes Jahr",
}


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


def _esc(value: object) -> str:
    return html_lib.escape(str(value))


def _fmt_kwh(value: float | None) -> str:
    return f"{value:.2f} kWh" if value is not None else "keine Daten"


def _fmt_percent(value: float | None) -> str:
    return f"{value:.0f} %" if value is not None else "–"


def _status_pill(active: bool) -> str:
    color = _COLOR_OK if active else _COLOR_FAIL
    label = "Aktiv" if active else "Nicht erreichbar"
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
        f'font-size:12px;font-weight:600;color:#ffffff;background:{color};">{label}</span>'
    )


def _stacked_bar(segments: list[tuple[str, float, str]]) -> str:
    """Baut eine gestapelte, proportionale Balken-Visualisierung (Liste von
    (label, value, color)) plus Legende darunter - z.B. für den
    Hausverbrauch nach Quelle. Segmente mit value<=0 werden ausgelassen."""
    total = sum(v for _, v, _ in segments if v and v > 0)
    bar_parts = []
    legend_parts = []
    if total > 0:
        for label, value, color in segments:
            if not value or value <= 0:
                continue
            width_pct = round(value / total * 100, 2)
            bar_parts.append(
                f'<div style="width:{width_pct}%;background:{color};height:100%;'
                f'display:inline-block;"></div>'
            )
            legend_parts.append(
                f'<span style="color:{color};">&#9679;</span> '
                f'<span style="color:{_COLOR_TEXT};">{_esc(label)}: {_fmt_kwh(value)}</span>'
            )
    bar_html = (
        f'<div style="width:100%;height:20px;border-radius:6px;overflow:hidden;'
        f'background:{_COLOR_BORDER};font-size:0;">{"".join(bar_parts)}</div>'
        if bar_parts
        else f'<p style="color:{_COLOR_MUTED};margin:0;">Keine Daten für heute.</p>'
    )
    legend_html = (
        f'<p style="margin:8px 0 0;font-size:13px;">{" &nbsp;&nbsp; ".join(legend_parts)}</p>'
        if legend_parts
        else ""
    )
    return bar_html + legend_html


def _soc_bar(percent: float) -> str:
    percent = max(0.0, min(100.0, percent))
    color = _COLOR_FAIL if percent < 20 else ("#d97706" if percent < 50 else _COLOR_OK)
    return (
        f'<div style="width:100%;height:14px;border-radius:7px;background:{_COLOR_BORDER};'
        f'overflow:hidden;"><div style="width:{percent:.0f}%;height:100%;background:{color};">'
        f"</div></div>"
    )


def _card(label: str, value: str, color: str) -> str:
    return (
        f'<td style="padding:6px;" width="33%">'
        f'<div style="background:{_COLOR_CARD};border:1px solid {_COLOR_BORDER};'
        f'border-radius:10px;padding:14px;text-align:center;">'
        f'<div style="font-size:22px;font-weight:700;color:{color};">{_esc(value)}</div>'
        f'<div style="font-size:12px;color:{_COLOR_MUTED};margin-top:4px;">{_esc(label)}</div>'
        f"</div></td>"
    )


def build_report_html(
    summaries: list[SummaryOut],
    online_map: dict[str, bool],
    pv_yield_periods: list[FeedInPeriod],
    home_breakdown_today: DailyHomeBreakdownDay | None,
    battery_snapshot: list[dict],
    now: datetime,
    tz_name: str,
) -> tuple[str, str]:
    """Baut (subject, html_body) für die tägliche Zusammenfassungsmail.
    Rein funktional (keine Seiteneffekte), damit sich das Layout unabhängig
    vom eigentlichen Mailversand testen lässt. Nutzt bewusst nur
    inline-Styles und Block-/Inline-Block-Elemente (kein Flexbox/Grid,
    kein <style>-Block), damit die Darstellung in möglichst vielen
    Mail-Clients funktioniert."""
    tz = ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    date_str = local_now.strftime("%d.%m.%Y")

    device_summaries = [s for s in summaries if s.device_id != "_all_"]
    combined = next((s for s in summaries if s.device_id == "_all_"), None)
    active_count = sum(1 for s in device_summaries if online_map.get(s.device_id, False))

    total_yield = combined.yield_day_kwh if combined else (
        device_summaries[0].yield_day_kwh if device_summaries else None
    )
    total_home = combined.home_consumption_day_kwh if combined else (
        device_summaries[0].home_consumption_day_kwh if device_summaries else None
    )

    # --- Hero-Kacheln ---
    hero = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        + _card("PV-Ertrag heute", _fmt_kwh(total_yield), _COLOR_PV)
        + _card("Hausverbrauch heute", _fmt_kwh(total_home), _COLOR_BATTERY)
        + _card(
            "Wechselrichter aktiv",
            f"{active_count} / {len(device_summaries)}" if device_summaries else "–",
            _COLOR_OK if device_summaries and active_count == len(device_summaries) else _COLOR_GRID,
        )
        + "</tr></table>"
    )

    # --- Wechselrichter-Tabelle ---
    if device_summaries:
        rows_html = "".join(
            f'<tr><td style="padding:8px 10px;border-bottom:1px solid {_COLOR_BORDER};">'
            f"{_esc(s.device_name)}</td>"
            f'<td style="padding:8px 10px;border-bottom:1px solid {_COLOR_BORDER};">'
            f"{_status_pill(online_map.get(s.device_id, False))}</td>"
            f'<td style="padding:8px 10px;border-bottom:1px solid {_COLOR_BORDER};'
            f'text-align:right;color:{_COLOR_TEXT};">{_fmt_kwh(s.yield_day_kwh)}</td></tr>'
            for s in device_summaries
        )
        devices_table = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;font-size:14px;">'
            f'<tr><th style="text-align:left;padding:6px 10px;color:{_COLOR_MUTED};'
            f'font-size:12px;text-transform:uppercase;">Wechselrichter</th>'
            f'<th style="text-align:left;padding:6px 10px;color:{_COLOR_MUTED};'
            f'font-size:12px;text-transform:uppercase;">Status</th>'
            f'<th style="text-align:right;padding:6px 10px;color:{_COLOR_MUTED};'
            f'font-size:12px;text-transform:uppercase;">PV-Ertrag heute</th></tr>'
            f"{rows_html}</table>"
        )
    else:
        devices_table = f'<p style="color:{_COLOR_MUTED};">Keine Wechselrichter konfiguriert.</p>'

    # --- Hausverbrauch nach Quelle (heute) ---
    if home_breakdown_today is not None:
        breakdown_html = _stacked_bar(
            [
                ("PV", home_breakdown_today.pv_kwh or 0.0, _COLOR_PV),
                ("Batterie", home_breakdown_today.battery_kwh or 0.0, _COLOR_BATTERY),
                ("Netz", home_breakdown_today.grid_kwh or 0.0, _COLOR_GRID),
            ]
        )
    else:
        breakdown_html = f'<p style="color:{_COLOR_MUTED};">Noch keine Daten für heute.</p>'

    # --- PV-Ertrag je Zeitraum ---
    period_rows = "".join(
        f'<tr><td style="padding:6px 10px;border-bottom:1px solid {_COLOR_BORDER};">'
        f'{_esc(_PERIOD_LABELS.get(p.key, p.key))}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid {_COLOR_BORDER};'
        f'text-align:right;">{_fmt_kwh(p.kwh)}</td></tr>'
        for p in pv_yield_periods
    )
    pv_yield_table = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;font-size:14px;">{period_rows}</table>'
        if pv_yield_periods
        else f'<p style="color:{_COLOR_MUTED};">Keine Daten.</p>'
    )

    # --- Batterie-Ladestand ---
    if battery_snapshot:
        battery_html = "".join(
            f'<div style="margin-bottom:10px;">'
            f'<div style="font-size:13px;margin-bottom:4px;">{_esc(b["device_name"])} '
            f'<span style="color:{_COLOR_MUTED};">({_fmt_percent(b["battery_soc_percent"])})</span></div>'
            f'{_soc_bar(b["battery_soc_percent"])}'
            f"</div>"
            for b in battery_snapshot
        )
    else:
        battery_html = f'<p style="color:{_COLOR_MUTED};">Keine Batterie konfiguriert/erkannt.</p>'

    def _section(title: str, content: str) -> str:
        return (
            f'<div style="background:{_COLOR_CARD};border:1px solid {_COLOR_BORDER};'
            f'border-radius:10px;padding:16px;margin-top:16px;">'
            f'<h2 style="margin:0 0 12px;font-size:15px;color:{_COLOR_TEXT};">{_esc(title)}</h2>'
            f"{content}</div>"
        )

    body = f"""\
<div style="background:{_COLOR_BG};padding:24px 12px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
  <div style="max-width:600px;margin:0 auto;">
    <div style="background:{_COLOR_HEADER_BG};border-radius:10px 10px 0 0;padding:20px 24px;">
      <div style="color:#ffffff;font-size:18px;font-weight:700;">☀️ Kostal Plenticore Monitor</div>
      <div style="color:#94a3b8;font-size:13px;margin-top:4px;">Tageszusammenfassung {_esc(date_str)} · Stand {_esc(local_now.strftime('%H:%M'))} Uhr</div>
    </div>
    <div style="background:{_COLOR_BG};padding:16px 0 0;">
      {hero}
      {_section("Wechselrichter", devices_table)}
      {_section("Hausverbrauch heute nach Quelle", breakdown_html)}
      {_section("PV-Ertrag", pv_yield_table)}
      {_section("Batterie-Ladestand (aktuell)", battery_html)}
      <p style="color:{_COLOR_MUTED};font-size:11px;text-align:center;margin-top:20px;">
        Automatisch versendet von Kostal Plenticore Monitor.
      </p>
    </div>
  </div>
</div>
"""

    subject = f"Kostal Plenticore Monitor - Tageszusammenfassung {date_str}"
    return subject, body


async def generate_and_send_daily_report() -> dict:
    """Baut den aktuellen Snapshot und verschickt ihn als HTML-Mail –
    gemeinsam genutzt vom Scheduler (zur konfigurierten Uhrzeit) und dem
    manuellen Trigger-Endpoint. Liest die Konfiguration frisch (ignoriert
    dabei bewusst den "enabled"-Schalter - wer das explizit aufruft, will
    testen/verschicken, unabhängig davon, ob der automatische Versand
    gerade aktiviert ist) und fängt JEDEN Fehler ab (Konfigurationsfehler,
    Mail-Service nicht erreichbar o.ä.), statt ihn weiterzureichen – ein
    fehlgeschlagener Report darf den Scheduler-Loop nicht beenden."""
    now = datetime.now(timezone.utc)
    cfg = get_config()
    try:
        summaries = build_daily_summaries()
        online_map = device_online_map(now=now)
        pv_yield_periods = build_pv_yield_summary()
        home_breakdown_days = build_daily_home_breakdown(days=1)
        home_breakdown_today = home_breakdown_days[-1] if home_breakdown_days else None
        battery_snapshot = device_battery_snapshot()
        subject, body = build_report_html(
            summaries,
            online_map,
            pv_yield_periods,
            home_breakdown_today,
            battery_snapshot,
            now,
            settings.timezone_name,
        )
        await send_report_mail(subject, body, cfg=cfg, html=True)
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
