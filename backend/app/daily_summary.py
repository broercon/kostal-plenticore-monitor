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

from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from .aggregation import (
    aggregate_per_device,
    combine_devices,
    daily_home_source_breakdown_kwh,
    daily_kwh_totals,
    daily_pv_yield_totals,
    integrate_kwh,
    integrate_pure_pv_kwh,
)
from .config import settings
from .database import SessionLocal
from .models import DailyEnergyCache, Reading
from .poller import poller
from .schemas import AutarkyMonthOut, DailyHomeBreakdownDay, FeedInPeriod, SummaryOut
from .timeutil import local_midnight_utc

# Synthetische device_id für die "Alle (Summe)"-Zeile bei mehreren
# Wechselrichtern (siehe main.COMBINED_DEVICE_ID).
COMBINED_DEVICE_ID = "_all_"


def _has_grid_meter_map() -> dict[str, bool]:
    return {cfg.id: cfg.has_grid_meter for cfg in settings.inverters}


def _battery_inverted_map() -> dict[str, bool]:
    return {cfg.id: cfg.battery_power_inverted for cfg in settings.inverters}


def _autarky_percent(
    pv_kwh: float | None, battery_kwh: float | None, grid_kwh: float | None
) -> float | None:
    """Autarkiegrad in Prozent: welcher Anteil des Hausverbrauchs (PV +
    Speicher + Netz) aus eigener Erzeugung/Speicher statt aus dem Netz kam.

    None, wenn einer der drei Anteile unbekannt ist (siehe
    daily_home_source_breakdown_kwh - z.B. weil fuer den betrachteten
    Zeitraum keine Haus-/PV-Messwerte vorliegen) oder der Hausverbrauch
    insgesamt 0 war (dann ist "Autarkiegrad" nicht sinnvoll definiert)."""
    if pv_kwh is None or battery_kwh is None or grid_kwh is None:
        return None
    home_kwh = pv_kwh + battery_kwh + grid_kwh
    if home_kwh <= 0:
        return None
    return round(100 * (pv_kwh + battery_kwh) / home_kwh, 1)


def _home_source_breakdown_with_grid(rows: list[Reading]) -> list[dict]:
    """Hausverbrauchs-Aufteilung nur aus Messpunkten mit echtem Netzwert.

    ``daily_home_source_breakdown_kwh`` nimmt einen fehlenden Netzbezug
    bewusst als 0 an, damit das bestehende Tagesverbrauchsdiagramm auch bei
    einzelnen Messluecken eine Aufteilung anzeigen kann. Fuer den
    Autarkiegrad waere dieselbe Annahme jedoch irrefuehrend: Historische
    Importdaten ganz ohne Netzmessung wuerden sonst als 100 % autark gelten.
    Deshalb werden fuer Autarkie nur Messpunkte verwendet, an denen ein
    Netzbezugswert tatsaechlich vorhanden ist. Reichen diese Punkte nicht
    fuer eine Integration, bleibt der Wert automatisch unbekannt.
    """
    return daily_home_source_breakdown_kwh(
        [row for row in rows if row.grid_draw_power_w is not None],
        settings.timezone_name,
    )


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
        home_kwh = reading.get("home_consumption_day_kwh") if reading else None
        grid_kwh = reading.get("energy_grid_day_kwh") if reading else None

        # Messwerte des Geraets seit Mitternacht laden - fuer den PV-Ertrag
        # IMMER noetig (reine PV wird integriert, siehe unten) und als
        # Rueckfall fuer Haus/Netz, falls das Geraet keine eigenen Tages-
        # Statistikwerte liefert.
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

        # PV-Ertrag = reine PV-Erzeugung (pv1+pv2), aus der Leistung integriert.
        # Bewusst NICHT der Geraete-Zaehler Statistic:Yield:Day (yield_day_kwh):
        # der zaehlt beim Hybrid den Wechselrichter-Ausgang inkl. Batterie mit.
        # integrate_pure_pv_kwh rechnet die am PV3-String haengende Batterie
        # heraus (pv_power_w - battery_power_w), sodass nachts 0 herauskommt.
        yield_kwh = integrate_pure_pv_kwh(rows)
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
            # PV-Ertrag ist additiv: der Gesamtwert ist die Summe der je Geraet
            # ermittelten reinen PV-Tageswerte (integrate_pure_pv_kwh). Damit
            # stimmt "Alle (Summe)" exakt mit der Summe der einzelnen
            # Wechselrichter ueberein. Hausverbrauch/Netz lassen sich dagegen
            # NICHT naiv summieren und werden aus der korrigierten Hausbilanz
            # integriert.
            device_yields = [s.yield_day_kwh for s in summaries if s.yield_day_kwh is not None]
            combined_yield = round(sum(device_yields), 3) if device_yields else None
            summaries.append(
                SummaryOut(
                    device_id=COMBINED_DEVICE_ID,
                    device_name="Alle (Summe)",
                    yield_day_kwh=combined_yield,
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


def _energy_period_ranges() -> list[tuple[str, date, date]]:
    """Die neun Zeitraeume (key, from_date, to_date) fuer die Energie-
    Uebersichten: heute, gestern, vorgestern, diese/letzte Woche (Mo-So),
    dieser/letzter Kalendermonat sowie dieses/letztes Kalenderjahr."""
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
    return [
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


def _load_readings_range(start_date: date, end_date_exclusive: date) -> list[Reading]:
    """Laedt Messwerte fuer [start_date, end_date_exclusive) - anders als
    frueher (_load_readings_since bis "jetzt") ein SCHMALES Zeitfenster,
    passend zu _cached_daily_totals: fuer bereits gecachte Tage wird diese
    Funktion gar nicht erst aufgerufen, fuer die verbleibenden (neuen/
    fehlenden) Tage nur fuer genau deren Zeitfenster, nicht fuer den
    gesamten angefragten Zeitraum (der bei "dieses/letztes Jahr" mehrere
    Millionen Zeilen umfassen kann)."""
    tz = ZoneInfo(settings.timezone_name)
    since = datetime.combine(start_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    until = datetime.combine(end_date_exclusive, datetime.min.time(), tzinfo=tz).astimezone(
        timezone.utc
    )
    session = SessionLocal()
    try:
        return list(
            session.scalars(
                select(Reading)
                .where(Reading.timestamp >= since, Reading.timestamp < until)
                .order_by(Reading.timestamp)
            )
        )
    finally:
        session.close()


def invalidate_energy_cache(start_date: date, end_date: date) -> None:
    """Löscht gecachte Tageswerte (siehe _cached_daily_totals) im
    angegebenen Datumsbereich (inklusive beider Enden) - aufgerufen nach
    einem Logdaten-Import (auto_import.py), der rückwirkend Messwerte für
    diese Tage ergänzt/verändert haben könnte. Ohne das würde die nächste
    Anfrage den alten (evtl. unvollständigen) Cache-Wert weiterverwenden,
    statt ihn aus den jetzt vollständigeren Rohmesswerten neu zu berechnen."""
    session = SessionLocal()
    try:
        session.execute(
            delete(DailyEnergyCache).where(
                DailyEnergyCache.date >= start_date.strftime("%Y-%m-%d"),
                DailyEnergyCache.date <= end_date.strftime("%Y-%m-%d"),
            )
        )
        session.commit()
    finally:
        session.close()


def _cached_daily_totals(
    field_key: str,
    earliest: date,
    today: date,
    compute_missing: Callable[[date, date], dict[str, float | None]],
) -> dict[str, float | None]:
    """Liefert {date_str: kwh} für [earliest, today] unter Ausnutzung von
    daily_energy_cache: ABGESCHLOSSENE Tage (< today) werden nur EINMAL über
    compute_missing(start, end_exclusive) berechnet und danach dauerhaft im
    Cache abgelegt - jeder weitere Aufruf (z.B. alle 5 Minuten durchs
    Dashboard) liest sie nur noch aus der (kleinen, indizierten)
    Cache-Tabelle, statt erneut sämtliche Rohmesswerte seit `earliest` zu
    laden und zu integrieren. "Heute" ist noch nicht abgeschlossen (der Wert
    wächst über den Tag) und wird deshalb NIE gecacht, sondern bei jedem
    Aufruf frisch berechnet - aber nur für diesen einen Tag, nicht den
    gesamten Zeitraum."""
    session = SessionLocal()
    try:
        cached_rows = list(
            session.scalars(
                select(DailyEnergyCache).where(
                    DailyEnergyCache.field == field_key,
                    DailyEnergyCache.date >= earliest.strftime("%Y-%m-%d"),
                    DailyEnergyCache.date < today.strftime("%Y-%m-%d"),
                )
            )
        )
        result: dict[str, float | None] = {row.date: row.kwh for row in cached_rows}
    finally:
        session.close()

    num_closed_days = (today - earliest).days
    all_closed_dates = {
        (earliest + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(num_closed_days)
    }
    missing_closed_dates = sorted(all_closed_dates - result.keys())

    if missing_closed_dates:
        # In EINEM Rutsch nachberechnen (ein Aufruf von compute_missing über
        # die gesamte Lücke), statt Tag für Tag einzeln - in der Praxis nur
        # beim allerersten Aufruf nach dieser Änderung ein größerer Bereich,
        # danach höchstens noch ein einzelner neuer Tag (der gestrige,
        # sobald er "abgeschlossen" ist).
        gap_start = datetime.strptime(missing_closed_dates[0], "%Y-%m-%d").date()
        gap_end_exclusive = datetime.strptime(missing_closed_dates[-1], "%Y-%m-%d").date() + timedelta(
            days=1
        )
        fresh = compute_missing(gap_start, gap_end_exclusive)

        session = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            for date_str in missing_closed_dates:
                session.merge(
                    DailyEnergyCache(
                        field=field_key, date=date_str, kwh=fresh.get(date_str), computed_at=now
                    )
                )
            session.commit()
        finally:
            session.close()
        result.update({d: fresh.get(d) for d in missing_closed_dates})

    today_str = today.strftime("%Y-%m-%d")
    result[today_str] = compute_missing(today, today + timedelta(days=1)).get(today_str)
    return result


def _periods_from_per_day(periods, per_day: dict[str, float | None]) -> list[FeedInPeriod]:
    """Aus Tageswerten {date: kwh} die Summe je Zeitraum bilden. Ein Zeitraum
    ganz ohne Tageswerte liefert kwh=None (statt 0)."""
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


def build_energy_period_summary(field: str) -> list[FeedInPeriod]:
    """Integrierte Energiemenge (kWh) eines Leistungsfeldes je Zeitraum.

    `field` ist das zu integrierende Reading-Feld, z.B. "feed_in_power_w"
    (Einspeisung). Bei mehreren Wechselrichtern wird zuvor auf die hausweite,
    korrigierte Energiebilanz zusammengefasst (siehe _combined_rows). Fuer den
    PV-Ertrag NICHT verwenden - dafuer build_pv_yield_summary().

    Abgeschlossene Tage werden über _cached_daily_totals zwischengespeichert
    (siehe dort) - ohne das würde jede Anfrage (Dashboard alle 5 Minuten)
    sämtliche Rohmesswerte seit Anfang des Vorjahres neu integrieren."""
    periods = _energy_period_ranges()
    earliest = min(start for _, start, _ in periods)
    today = datetime.now(ZoneInfo(settings.timezone_name)).date()

    def compute(start: date, end_exclusive: date) -> dict[str, float | None]:
        rows = _load_readings_range(start, end_exclusive)
        if len(settings.inverters) > 1:
            rows = _combined_rows(rows)
        return {d["date"]: d["kwh"] for d in daily_kwh_totals(rows, field, settings.timezone_name)}

    per_day = _cached_daily_totals(f"field:{field}", earliest, today, compute)
    return _periods_from_per_day(periods, per_day)


def build_feed_in_summary() -> list[FeedInPeriod]:
    """Einspeisung (kWh) je Zeitraum (integriert)."""
    return build_energy_period_summary("feed_in_power_w")


def build_pv_yield_summary() -> list[FeedInPeriod]:
    """PV-Ertrag (kWh) je Zeitraum - fuer Dashboard-Leiste und Mail-Report.

    PV-Ertrag = reine PV-Erzeugung, aus der Leistung integriert (siehe
    aggregation.daily_pv_yield_totals/integrate_pure_pv_kwh) - bewusst NICHT
    der geräteeigene Tageszähler Statistic:Yield:Day, der beim Hybrid den
    Wechselrichter-Ausgang inkl. Batterieentladung mitzählt.

    Abgeschlossene Tage werden über _cached_daily_totals zwischengespeichert
    (siehe dort) - ohne das würde jede Anfrage (Dashboard alle 5 Minuten)
    sämtliche Rohmesswerte seit Anfang des Vorjahres neu integrieren."""
    periods = _energy_period_ranges()
    earliest = min(start for _, start, _ in periods)
    today = datetime.now(ZoneInfo(settings.timezone_name)).date()

    def compute(start: date, end_exclusive: date) -> dict[str, float | None]:
        rows = _load_readings_range(start, end_exclusive)
        return {d["date"]: d["kwh"] for d in daily_pv_yield_totals(rows, settings.timezone_name)}

    per_day = _cached_daily_totals("pv_yield", earliest, today, compute)
    return _periods_from_per_day(periods, per_day)


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

    breakdown = daily_home_source_breakdown_kwh(rows, settings.timezone_name)
    autarky_by_date = {
        day["date"]: _autarky_percent(
            day.get("pv_kwh"), day.get("battery_kwh"), day.get("grid_kwh")
        )
        for day in _home_source_breakdown_with_grid(rows)
    }

    # In DailyHomeBreakdownDay-Objekte wandeln (statt roher Dicts), damit
    # sowohl der API-Endpunkt als auch der Mail-Report per Attribut darauf
    # zugreifen koennen (der Report ruft z.B. .pv_kwh direkt auf). Ergaenzt
    # um den Autarkiegrad des jeweiligen Tages (siehe _autarky_percent) -
    # fuer die "Autarkiegrad heute"-Kachel in der Uebersicht sowie als
    # Zusatzinfo im Tagesverbrauch-Diagramm.
    return [
        DailyHomeBreakdownDay(
            **day,
            autarky_percent=autarky_by_date.get(day["date"]),
        )
        for day in breakdown
    ]


def _earliest_reading_date() -> date | None:
    """Lokales Kalenderdatum des allerersten gespeicherten Messwerts (ueber
    alle Geraete) - Startpunkt fuer die monatliche Autarkiegrad-Uebersicht
    (siehe build_autarky_monthly_summary), da dort (anders als bei den neun
    Zeitraeumen in _energy_period_ranges) die GESAMTE Historie seit
    Inbetriebnahme gezeigt werden soll, nicht nur bis "letztes Jahr"."""
    session = SessionLocal()
    try:
        earliest_ts = session.scalar(select(func.min(Reading.timestamp)))
    finally:
        session.close()
    if earliest_ts is None:
        return None
    if earliest_ts.tzinfo is None:
        earliest_ts = earliest_ts.replace(tzinfo=timezone.utc)
    return earliest_ts.astimezone(ZoneInfo(settings.timezone_name)).date()


# Mapping von daily_home_source_breakdown_kwh()-Schluessel auf den
# daily_energy_cache-Feldnamen, unter dem der jeweilige Anteil
# zwischengespeichert wird (siehe _cached_home_source_field).
_HOME_SOURCE_CACHE_FIELDS = {
    "pv_kwh": "home_source_pv",
    "battery_kwh": "home_source_battery",
    "grid_kwh": "home_source_grid",
}


def _cached_home_source_field(
    out_key: str, earliest: date, today: date
) -> dict[str, float | None]:
    """Wie build_energy_period_summary()/build_pv_yield_summary(): liefert
    {date_str: kwh} fuer einen einzelnen Hausverbrauchs-Anteil (PV/Speicher/
    Netz, siehe daily_home_source_breakdown_kwh) unter Nutzung von
    _cached_daily_totals - abgeschlossene Tage werden also genauso im
    daily_energy_cache zwischengespeichert wie die bestehenden PV-Ertrags-/
    Einspeisungs-Uebersichten, nur unter einem eigenen Feldnamen je Anteil.

    Wird fuer die drei Anteile separat aufgerufen (siehe
    build_autarky_monthly_summary) - bei einer Cache-Luecke werden die
    Rohmesswerte dadurch bis zu dreimal geladen statt einmal fuer alle drei
    Anteile gemeinsam. Das faellt in der Praxis nicht ins Gewicht: eine
    Luecke tritt nur einmalig (erster Aufruf nach diesem Feature) oder fuer
    einen einzelnen neuen Tag auf (der gestrige, sobald "abgeschlossen") -
    siehe _cached_daily_totals fuer die Begruendung dieses Musters."""

    def compute(start: date, end_exclusive: date) -> dict[str, float | None]:
        rows = _load_readings_range(start, end_exclusive)
        if len(settings.inverters) > 1:
            rows = _combined_rows(rows)
        return {
            d["date"]: d[out_key]
            for d in _home_source_breakdown_with_grid(rows)
        }

    return _cached_daily_totals(_HOME_SOURCE_CACHE_FIELDS[out_key], earliest, today, compute)


def build_autarky_monthly_summary(months: int | None = None) -> list[AutarkyMonthOut]:
    """Autarkiegrad je Kalendermonat, seit dem allerersten gespeicherten
    Messwert (siehe _earliest_reading_date).

    Ein Monatswert ist NICHT der Mittelwert der taeglichen Prozentsaetze,
    sondern wird aus den ueber den Monat aufsummierten kWh-Anteilen
    berechnet (siehe _autarky_percent) - sonst wuerden Tage mit wenig
    Hausverbrauch (z.B. Abwesenheit) das Monatsergebnis unverhaeltnismaessig
    verzerren, obwohl sie kaum zum tatsaechlichen Monatsverbrauch beitragen.

    `months`: bei Angabe werden nur die letzten `months` Kalendermonate
    (mit Daten) zurueckgegeben - analog zum `days`-Parameter bei
    /api/readings/daily-home-breakdown. None (Standard) liefert die
    komplette Historie.

    Monate ganz ohne Messwerte (z.B. eine Luecke vor der Inbetriebnahme
    aller Geraete) fehlen in der Rueckgabe, statt mit 0 kWh/undefiniertem
    Autarkiegrad aufzutauchen."""
    earliest = _earliest_reading_date()
    if earliest is None:
        return []
    today = datetime.now(ZoneInfo(settings.timezone_name)).date()

    per_day = {
        out_key: _cached_home_source_field(out_key, earliest, today)
        for out_key in _HOME_SOURCE_CACHE_FIELDS
    }

    per_month: dict[str, dict[str, float]] = {}
    months_with_data: set[str] = set()
    day = earliest
    while day <= today:
        date_str = day.strftime("%Y-%m-%d")
        month_key = day.strftime("%Y-%m")
        entry = per_month.setdefault(month_key, {"pv_kwh": 0.0, "battery_kwh": 0.0, "grid_kwh": 0.0})
        for out_key in _HOME_SOURCE_CACHE_FIELDS:
            value = per_day[out_key].get(date_str)
            if value is not None:
                entry[out_key] += value
                months_with_data.add(month_key)
        day += timedelta(days=1)

    result = []
    for month_key in sorted(months_with_data):
        entry = per_month[month_key]
        home_kwh = entry["pv_kwh"] + entry["battery_kwh"] + entry["grid_kwh"]
        result.append(
            AutarkyMonthOut(
                month=month_key,
                pv_kwh=round(entry["pv_kwh"], 3),
                battery_kwh=round(entry["battery_kwh"], 3),
                grid_kwh=round(entry["grid_kwh"], 3),
                home_kwh=round(home_kwh, 3),
                autarky_percent=_autarky_percent(
                    entry["pv_kwh"], entry["battery_kwh"], entry["grid_kwh"]
                ),
            )
        )

    if months is not None and months > 0:
        result = result[-months:]
    return result
