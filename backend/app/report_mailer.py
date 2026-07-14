"""Kleiner HTTP-Client für den zentralen Mail-Service (siehe
broercon/Mailserver, POST /send) – genutzt vom täglichen Report
(daily_report.py). Bewusst kein eigenes Paket/keine neue Abhängigkeit:
nutzt aiohttp, das über plenticore_client.py ohnehin schon Teil der
Requirements ist.
"""
from __future__ import annotations

import logging

import aiohttp

from .config import settings

logger = logging.getLogger(__name__)


class ReportMailError(Exception):
    """Wird geworfen, wenn der Report nicht über den Mail-Service verschickt
    werden konnte (fehlende Konfiguration, Verbindungsfehler, Fehlerstatus)."""


async def send_report_mail(subject: str, body: str, *, html: bool = False) -> None:
    """Schickt subject/body an settings.daily_report_recipients über die
    Mailserver-REST-API (settings.mail_service_url, z.B.
    "http://mail-api:8080/send"). Wirft immer ReportMailError bei einem
    Fehlschlag – der Aufrufer (daily_report.py) fängt das ab und merkt sich
    den Status, statt den Scheduler zu beenden."""
    if not settings.mail_service_url:
        raise ReportMailError("MAIL_SERVICE_URL ist nicht konfiguriert.")
    if not settings.daily_report_recipients:
        raise ReportMailError("DAILY_REPORT_RECIPIENTS ist leer.")

    payload = {
        "to": list(settings.daily_report_recipients),
        "subject": subject,
        "body": body,
        "html": html,
        "from_name": settings.mail_service_from_name or None,
    }
    headers = {"X-API-Key": settings.mail_service_api_key} if settings.mail_service_api_key else {}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(
                settings.mail_service_url, json=payload, headers=headers
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise ReportMailError(f"Mail-Service antwortete mit HTTP {resp.status}: {text}")
    except ReportMailError:
        raise
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        raise ReportMailError(f"Mail-Service nicht erreichbar: {exc}") from exc
