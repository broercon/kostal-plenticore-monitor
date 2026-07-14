"""Baut alle Datengrundlagen für den täglichen Mail-Report (siehe
daily_report.py) sowie für die zugehörigen API-Endpunkte: Tagessummen je
Wechselrichter (build_daily_summaries), "aktiv/erreichbar"-Status
(device_online_map), Einspeisung je Zeitraum (build_feed_in_summary),
Hausverbrauch nach Quelle PV/Batterie/Netz je Tag
(build_daily_home_breakdown) sowie aktueller Batterie-Ladestand
(device_battery_snapshot).

Die eigentlichen Berechnungen sind 1:1 aus main.py ausgelagert (keine
FastAPI-/Auth-Abhängigkeiten), damit sie sowohl von den jeweiligen
API-Endpunkten als auch vom täglichen Mail-Report verwendet werden können,
ohne die Logik doppelt zu pflegen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .aggregation import (
    aggregate_per_device,
    combine_devices,
    daily_home_source_breakdown_kwh,
    daily_kwh_totals,
    integrate_kwh,
)
from .config import settings
from .database import SessionLocal
from .models import Reading
from .poller import poller
from .schemas import DailyHomeBreakdownDay, FeedInPeriod, SummaryOut
from .timeutil import local_midnight_utc

# Synthetische device_id für die "Alle (Summe)"-Zeile bei mehreren
# Wechselrichtern (siehe main.COMBINED_DEVICE_ID).
COMBINED_DEVICE_ID = "_all_"


def _has_grid_meter_map() -> dict[str, bool]:
    return {cfg.id: cfg.has_grid_meter for cfg in settings.inverters}


def _battery_inverted_map() -> dict[str, bool]:
    return {cfg.id: cfg.battery_power_inverted for cfg in settings.inverters}


def _combined_rows(rows: list[Reading]) -> list[Reading]:
    """Fasst rows (mehrere Geräte) zur hausweit korrigierten Energiebilanz
    zusammen (siehe aggregation.combine_devices) - gemeinsamer Baustein für
    mehrere der Funktionen unten, die bei >1 Wechselrichter alle auf
    derselben Logik beruhen wie main.py's Endpunkte."""
    per_device = aggregate_per_device(rows, bucket_seconds=60)
    combined = combine_devices(per_device, _has_grid_meter_map(), _battery_inverted_map())
    return [
        Reading(
            device_id="_combined_",
            device_name="_combined_",
            timestamp=datetime.fromtimestamp(bk, tz=timezone.utc),
            **values,
        )
        for bk, values in combined.items()
    ]


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
            synthetic_rows = _combined_rows(rows)
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


def device_battery_snapshot() -> list[dict]:
    """Aktueller Batterie-Ladestand je Wechselrichter mit Batterie (letzter
    Poller-Messwert) - für die "Batterie-Ladestand"-Live-Kachel im
    Dashboard bzw. den Mail-Report. Geräte ohne (aktuell bekannten)
    Batterie-Ladestand werden ausgelassen, statt einen irreführenden
    0%-Wert vorzutäuschen."""
    result = []
    for cfg in settings.inverters:
        reading = poller.latest.get(cfg.id)
        soc = reading.get("battery_soc_percent") if reading else None
        if soc is None:
            continue
        result.append({"device_id": cfg.id, "device_name": cfg.name, "battery_soc_percent": soc})
    return result


def build_energy_period_summary(field: str) -> list[FeedInPeriod]:
    """Integrierte Energiemenge (kWh) eines Leistungsfeldes je Zeitraum:
    heute, gestern, vorgestern, diese/letzte Woche (Mo-So), dieser/letzter
    Kalendermonat sowie dieses/letztes Kalenderjahr.

    `field` ist das zu integrierende Reading-Feld, z.B. "feed_in_power_w"
    (Einspeisung) oder "pv_power_w" (PV-Ertrag). Bei mehreren Wechselrichtern
    wird zuvor auf die hausweite, korrigierte Energiebilanz zusammengefasst
    (siehe _combined_rows / combine_devices): PV- und Batterieleistung werden
    dabei ueber alle Geraete summiert, Netzbezug/Einspeisung dagegen aus der
    Bilanz neu berechnet. Ein Zeitraum ganz ohne Daten liefert kwh=None."""
    tz = ZoneInfo(settings.timezone_name)
    today = datetime.now(tz).date()
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)
    this_week_start = today - timedelta(days=today.weekday())  # Montag dieser Woche
    last_week_start = this_week_start - timedelta(days=7)
    last_week_end = this_week_start - timedelta(days=1)
    this_month_start = today.replace(day=1)
    last_month_end = this_month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    this_year_start = today.replace(month=1, day=1)
    last_year_end = this_year_start - timedelta(days=1)
    last_year_start = last_year_end.replace(month=1, day=1)

    periods = [
        ("today", today, today),
        ("yesterday", yesterday, yesterday),
        ("day_before_yesterday", day_before, day_before),
        ("this_week", this_week_start, today),
        ("last_week", last_week_start, last_week_end),
        ("this_month", this_month_start, today),
        ("last_month", last_month_start, last_month_end),
        ("this_year", this_year_start, today),
        ("last_year", last_year_start, last_year_end),
    ]

    earliest = min(start for _, start, _ in periods)
    since = datetime.combine(earliest, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    multi = len(settings.inverters) > 1
    session = SessionLocal()
    try:
        rows = list(
            session.scalars(
                select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)
            )
        )
    finally:
        session.close()

    if multi:
        rows = _combined_rows(rows)

    per_day = {
        d["date"]: d["kwh"]
        for d in daily_kwh_totals(rows, field, settings.timezone_name)
    }

    def sum_range(start, end) -> float | None:
        total = 0.0
        has_data = False
        day = start
        while day <= end:
            value = per_day.get(day.strftime("%Y-%m-%d"))
            if value is not None:
                total += value
                has_data = True
            day += timedelta(days=1)
        return round(total, 3) if has_data else None

    return [
        FeedInPeriod(
            key=key,
            from_date=start.strftime("%Y-%m-%d"),
            to_date=end.strftime("%Y-%m-%d"),
            kwh=sum_range(start, end),
        )
        for key, start, end in periods
    ]


def build_feed_in_summary() -> list[FeedInPeriod]:
    """Einspeisung (kWh) je Zeitraum - fuer den Mail-Report."""
    return build_energy_period_summary("feed_in_power_w")


def build_pv_yield_summary() -> list[FeedInPeriod]:
    """PV-Ertrag (kWh) je Zeitraum - fuer die Dashboard-Uebersicht."""
    return build_energy_period_summary("pv_power_w")


def build_daily_home_breakdown(days: int = 30) -> list[DailyHomeBreakdownDay]:
    """Hausverbrauch je Tag, aufgeschlüsselt nach PV-/Batterie-/Netz-Anteil
    (siehe main.get_daily_home_breakdown). Für den Mail-Report wird davon
    nur der letzte (heutige) Eintrag verwendet."""
    since = local_midnight_utc() - timedelta(days=days - 1)

    session = SessionLocal()
    try:
        rows = list(
            session.scalars(
                select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)
            )
        )
    finally:
        session.close()

    if len(settings.inverters) > 1:
        rows = _combined_rows(rows)

    return daily_home_source_breakdown_kwh(rows, settings.timezone_name)
