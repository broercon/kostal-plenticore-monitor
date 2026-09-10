"""Compatibility helper for hours compacted by an earlier branch revision.

No new compaction is performed: averaging signed power destroys separate
charge/discharge energy and cannot safely replace raw readings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def local_hour_start_utc(ts: datetime, tz: ZoneInfo) -> datetime:
    """Identify legacy compacted hours during import, preserving DST folds."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(tz)
    return local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
