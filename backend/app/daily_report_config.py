"""Über die Admin-Oberfläche editierbare Konfiguration des täglichen
Mail-Reports.

Ergänzt/überschreibt die Umgebungsvariablen aus config.py: beim
allerersten Aufruf (noch keine Zeile in der Datenbank) werden deren Werte
als Startwerte übernommen (siehe _env_defaults) - sobald einmal über die
Admin-Oberfläche gespeichert wurde, ist die Datenbank die Quelle der
Wahrheit. get_config() liest bewusst bei JEDEM Aufruf frisch aus der
Datenbank (kein In-Memory-Cache), damit eine Änderung über die Admin-Seite
sofort wirkt - der Scheduler in daily_report.py prüft die Konfiguration
deshalb bei jedem Zyklus neu, statt sie nur einmal beim Start zu lesen.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .config import settings
from .database import SessionLocal
from .models import DailyReportSettings

_SINGLETON_ID = 1


class InvalidReportTime(ValueError):
    """Ungültiges Uhrzeit-Format (erwartet HH:MM)."""


def parse_report_time(raw: str) -> tuple[int, int]:
    """Zerlegt "HH:MM" in (hour, minute) - wirft InvalidReportTime bei
    falschem Format/Wertebereich, statt eine kryptische ValueError/
    AttributeError durchzureichen."""
    try:
        hour_str, minute_str = raw.strip().split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except (ValueError, AttributeError) as exc:
        raise InvalidReportTime(f"Ungültige Uhrzeit {raw!r}, erwartet HH:MM") from exc


def _env_defaults() -> dict:
    return {
        "enabled": settings.daily_report_enabled,
        "report_time": settings.daily_report_time,
        "recipients": list(settings.daily_report_recipients),
        "mail_service_url": settings.mail_service_url,
        "mail_service_api_key": settings.mail_service_api_key,
        "mail_service_from_name": settings.mail_service_from_name,
    }


def _row_to_dict(row: DailyReportSettings) -> dict:
    return {
        "enabled": row.enabled,
        "report_time": row.report_time,
        "recipients": [r.strip() for r in row.recipients.split(",") if r.strip()],
        "mail_service_url": row.mail_service_url,
        "mail_service_api_key": row.mail_service_api_key,
        "mail_service_from_name": row.mail_service_from_name,
    }


def get_config() -> dict:
    """Aktuelle Konfiguration - aus der Datenbank, falls dort schon einmal
    über die Admin-Oberfläche gespeichert wurde, sonst als Fallback aus den
    Umgebungsvariablen."""
    session = SessionLocal()
    try:
        row = session.get(DailyReportSettings, _SINGLETON_ID)
        if row is None:
            return _env_defaults()
        return _row_to_dict(row)
    finally:
        session.close()


def update_config(
    *,
    enabled: bool,
    report_time: str,
    recipients: list[str],
    mail_service_url: str,
    mail_service_api_key: str | None,
    mail_service_from_name: str,
) -> dict:
    """Speichert die vollständige Konfiguration (legt die Zeile beim ersten
    Speichern an, vorbefüllt aus den bisherigen Umgebungsvariablen als
    Ausgangspunkt).

    mail_service_api_key: None oder leerer String bedeutet "vorhandenen
    Wert beibehalten" - das Feld wird nie im Klartext ans Frontend
    zurückgegeben (siehe schemas.DailyReportConfigOut.mail_service_api_key_set),
    aus Nutzersicht gibt es also keinen Unterschied zwischen "noch nie
    gesetzt" und "bewusst leer gelassen"; ein bereits gesetzter Key lässt
    sich nur durch Eingabe eines neuen Werts ersetzen.

    Wirft InvalidReportTime bei ungültigem report_time - der Aufrufer
    (main.py) wandelt das in eine 400-Antwort um."""
    parse_report_time(report_time)

    session = SessionLocal()
    try:
        row = session.get(DailyReportSettings, _SINGLETON_ID)
        if row is None:
            defaults = _env_defaults()
            row = DailyReportSettings(
                id=_SINGLETON_ID,
                enabled=defaults["enabled"],
                report_time=defaults["report_time"],
                recipients=", ".join(defaults["recipients"]),
                mail_service_url=defaults["mail_service_url"],
                mail_service_api_key=defaults["mail_service_api_key"],
                mail_service_from_name=defaults["mail_service_from_name"],
                updated_at=datetime.now(timezone.utc),
            )
            session.add(row)

        row.enabled = enabled
        row.report_time = report_time
        row.recipients = ", ".join(r.strip() for r in recipients if r.strip())
        row.mail_service_url = mail_service_url.strip()
        if mail_service_api_key:
            row.mail_service_api_key = mail_service_api_key.strip()
        row.mail_service_from_name = mail_service_from_name.strip()
        row.updated_at = datetime.now(timezone.utc)

        session.commit()
        session.refresh(row)
        result = _row_to_dict(row)
    finally:
        session.close()
    return result
