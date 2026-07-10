"""Zeitzonen-Hilfsfunktion fuer die Tagesgrenze der Tagessummen."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings


def local_midnight_utc(now: datetime | None = None) -> datetime:
    """Gibt die letzte lokale Mitternacht (gemaess TIMEZONE) als UTC-Zeitpunkt zurueck."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.timezone_name)
    local_now = now.astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)
