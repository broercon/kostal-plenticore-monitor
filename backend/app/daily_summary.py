"""Baut die Tageszusammenfassung (PV-Ertrag/Verbrauch je Wechselrichter +
hausweite Summe) sowie den "aktiv/erreichbar"-Status je Gerät.

Die eigentliche Summenberechnung ist 1:1 aus main.get_today_summary
ausgelagert (keine FastAPI-/Auth-Abhängigkeiten), damit sie sowohl vom
API-Endpoint GET /api/readings/today-summary als auch vom täglichen
Mail-Report (siehe daily_report.py) verwendet werden kann, ohne die Logik
doppelt zu pflegen.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from .aggregation import aggregate_per_device, combine_devices, integrate_kwh
from .config import settings
from .database import SessionLocal
from .models import Reading
from .poller import poller
from .schemas import SummaryOut
from .timeutil import local_midnight_utc

# Synthetische device_id für die "Alle (Summe)"-Zeile bei mehreren
# Wechselrichtern (siehe main.COMBINED_DEVICE_ID).
COMBINED_DEVICE_ID = "_all_"


def _has_grid_meter_map() -> dict[str, bool]:
    return {cfg.id: cfg.has_grid_meter for cfg in settings.inverters}


def _battery_inverted_map() -> dict[str, bool]:
    return {cfg.id: cfg.battery_power_inverted for cfg in settings.inverters}


def build_daily_summaries() -> list[SummaryOut]:
    """Tagessummen je Wechselrichter (+ "_all_"-Summe bei mehreren Geräten).
    Siehe main.get_today_summary für die ausführliche Erklärung der
    Fallback-Logik (Geräte-Statistikwert vs. Integration seit Mitternacht)."""
    since = local_midnight_utc()
    summaries: list[SummaryOut] = []

    for cfg in settings.inverters:
        reading = poller.latest.get(cfg.id)
        yield_kwh = reading.get("yield_day_kwh") if reading else None
        home_kwh = reading.get("home_consumption_day_kwh") if reading else None
        grid_kwh = reading.get("energy_grid_day_kwh") if reading else None

        if yield_kwh is None or home_kwh is None or grid_kwh is None:
            session = SessionLocal()
            try:
                rows = list(
                    session.scalars(
                        select(Reading)
                        .where(Reading.device_id == cfg.id, Reading.timestamp >= since)
                        .order_by(Reading.timestamp)
                    )
                )
            finally:
                session.close()

            if yield_kwh is None:
                yield_kwh = integrate_kwh(rows, "pv_power_w")
            if home_kwh is None:
                home_kwh = integrate_kwh(rows, "home_power_w")
            if grid_kwh is None:
                grid_kwh = integrate_kwh(rows, "feed_in_power_w")

        summaries.append(
            SummaryOut(
                device_id=cfg.id,
                device_name=cfg.name,
                yield_day_kwh=yield_kwh,
                home_consumption_day_kwh=home_kwh,
                energy_grid_day_kwh=grid_kwh,
                as_of=reading.get("timestamp") if reading else None,
            )
        )

    if len(settings.inverters) > 1:
        session = SessionLocal()
        try:
            rows = list(
                session.scalars(
                    select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)
                )
            )
        finally:
            session.close()

        if rows:
            per_device = aggregate_per_device(rows, bucket_seconds=60)
            combined = combine_devices(per_device, _has_grid_meter_map(), _battery_inverted_map())
            synthetic_rows = [
                Reading(
                    device_id="_combined_",
                    device_name="_combined_",
                    timestamp=datetime.fromtimestamp(bk, tz=timezone.utc),
                    **values,
                )
                for bk, values in combined.items()
            ]
            summaries.append(
                SummaryOut(
                    device_id=COMBINED_DEVICE_ID,
                    device_name="Alle (Summe)",
                    yield_day_kwh=integrate_kwh(synthetic_rows, "pv_power_w"),
                    home_consumption_day_kwh=integrate_kwh(synthetic_rows, "home_power_w"),
                    energy_grid_day_kwh=integrate_kwh(synthetic_rows, "feed_in_power_w"),
                    as_of=max(row.timestamp for row in rows),
                )
            )

    return summaries


def device_online_map(
    *, now: datetime | None = None, stale_after_seconds: float | None = None
) -> dict[str, bool]:
    """Ermittelt je konfiguriertem Wechselrichter, ob er gerade als
    "aktiv/erreichbar" gilt: der Poller hat innerhalb der letzten
    `stale_after_seconds` tatsächlich einen Messwert von ihm erhalten
    (siehe poller.latest). Standard: das 3-fache Poll-Intervall, mindestens
    aber 120s, damit ein einzelner verzögerter/verpasster Zyklus nicht
    sofort als Ausfall gewertet wird. Ein Gerät, das seit Start noch nie
    erfolgreich erreicht wurde, hat keinen Eintrag in poller.latest und
    gilt als nicht aktiv."""
    now = now or datetime.now(timezone.utc)
    if stale_after_seconds is None:
        stale_after_seconds = max(120.0, settings.poll_interval_seconds * 3)

    result: dict[str, bool] = {}
    for cfg in settings.inverters:
        reading = poller.latest.get(cfg.id)
        timestamp = reading.get("timestamp") if reading else None
        if timestamp is None:
            result[cfg.id] = False
            continue
        age_seconds = (now - timestamp).total_seconds()
        result[cfg.id] = age_seconds <= stale_after_seconds
    return result
