"""Kleiner HTTP-Client für den zentralen Mail-Service (siehe
broercon/Mailserver, POST /send) – genutzt vom täglichen Report
(daily_report.py). Bewusst kein eigenes Paket/keine neue Abhängigkeit:
nutzt aiohttp, das über plenticore_client.py ohnehin schon Teil der
Requirements ist.

Nimmt die Konfiguration (URL/Empfänger/API-Key/Absendername) bewusst als
explizites Dict entgegen statt sie selbst aus config.settings zu lesen -
seit der Admin-Oberfläche (siehe daily_report_config.py) ist die
Konfiguration zur Laufzeit änderbar (in der Datenbank, nicht mehr nur beim
Start aus Umgebungsvariablen gelesen), der Aufrufer übergibt daher den
jeweils aktuellen Stand.
"""
from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class ReportMailError(Exception):
    """Wird geworfen, wenn der Report nicht über den Mail-Service verschickt
    werden konnte (fehlende Konfiguration, Verbindungsfehler, Fehlerstatus)."""


async def send_report_mail(subject: str, body: str, *, cfg: dict, html: bool = False) -> None:
    """Schickt subject/body an cfg["recipients"] über die Mailserver-
    REST-API (cfg["mail_service_url"], z.B. "http://mail-api:8080/send").
    Wirft immer ReportMailError bei einem Fehlschlag – der Aufrufer
    (daily_report.py) fängt das ab und merkt sich den Status, statt den
    Scheduler zu beenden."""
    mail_service_url = cfg.get("mail_service_url") or ""
    recipients = cfg.get("recipients") or []

    if not mail_service_url:
        raise ReportMailError("Mail-Service-URL ist nicht konfiguriert.")
    if not recipients:
        raise ReportMailError("Keine Empfänger konfiguriert.")

    payload = {
        "to": list(recipients),
        "subject": subject,
        "body": body,
        "html": html,
        "from_name": cfg.get("mail_service_from_name") or None,
    }
    api_key = cfg.get("mail_service_api_key") or ""
    headers = {"X-API-Key": api_key} if api_key else {}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(mail_service_url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise ReportMailError(f"Mail-Service antwortete mit HTTP {resp.status}: {text}")
    except ReportMailError:
        raise
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        raise ReportMailError(f"Mail-Service nicht erreichbar: {exc}") from exc
