"""Baut alle Datengrundlagen für den täglichen Mail-Report (siehe
daily_report.py) sowie für die zugehörigen API-Endpunkte: Tagessummen je
Wechselrichter (build_daily_summaries), "aktiv/erreichbar"-Status
(device_online_map), Einspeisung je Zeitraum (build_feed_in_summary),
Speicherbilanz je Zeitraum (build_battery_energy_summary),
Hausverbrauch nach Quelle PV/Batterie/Netz je Tag
(build_daily_home_breakdown) sowie aktueller Batterie-Ladestand
(device_battery_snapshot).

Die eigentlichen Berechnungen sind 1:1 aus main.py ausgelagert (keine
FastAPI-/Auth-Abhängigkeiten), damit sie sowohl von den jeweiligen
API-Endpunkten als auch vom täglichen Mail-Report verwendet werden können,
ohne die Logik doppelt zu pflegen.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, cast, delete, func, select

from .aggregation import (
    BATTERY_CHARGE,
    BATTERY_DISCHARGE,
    HISTORY_FIELDS,
    aggregate_per_device,
    combine_devices,
    daily_battery_energy_flows,
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
from .schemas import DailyHomeBreakdownDay, FeedInPeriod, SummaryOut
from .timeutil import local_midnight_utc

# Synthetische device_id für die "Alle (Summe)"-Zeile bei mehreren
# Wechselrichtern (siehe main.COMBINED_DEVICE_ID).
COMBINED_DEVICE_ID = "_all_"

# Spaltenliste fuer Bulk-Zeitraum-Anfragen (select(*cols) statt
# select(Reading.__table__)) - device_id + timestamp + HISTORY_FIELDS deckt
# alle Aufrufer unten ab (integrate_kwh/integrate_pure_pv_kwh,
# aggregate_per_device/combine_devices, daily_kwh_totals,
# daily_pv_yield_totals, daily_battery_energy_flows,
# daily_home_source_breakdown_kwh), ohne die uebrigen ~14 Spalten von
# Reading unnoetig mitzuladen - siehe main._BULK_READING_COLUMNS fuer
# dieselbe Ueberlegung auf main.py-Seite.
_BULK_READING_COLUMNS = [Reading.device_id, Reading.timestamp, *[getattr(Reading, f) for f in HISTORY_FIELDS]]


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


# Kantenlaenge der Zeit-Buckets, in denen die Geraete vor dem Kombinieren
# gemittelt werden (siehe _combined_rows). Muss zwischen der Python- und der
# SQL-Variante identisch sein, sonst entstehen unterschiedliche Buckets.
_COMBINE_BUCKET_SECONDS = 60


def _rows_from_per_device(
    per_device: dict[str, dict[int, dict[str, float | None]]]
) -> list[Reading]:
    """Baut aus gemittelten Geraete-Buckets die hausweit korrigierte
    Energiebilanz (siehe aggregation.combine_devices) als Reading-artige
    Zeitreihe - gemeinsamer Baustein fuer die Funktionen unten, die bei
    >1 Wechselrichter alle auf derselben Logik beruhen wie main.py's
    Endpunkte."""
    combined = combine_devices(
        per_device, _has_grid_meter_map(), _battery_inverted_map(),
        raw_battery_output=True,
    )
    return [
        Reading(
            device_id="_combined_",
            device_name="_combined_",
            timestamp=datetime.fromtimestamp(bk, tz=timezone.utc),
            **values,
        )
        for bk, values in combined.items()
    ]


def _combined_rows(rows: list[Reading]) -> list[Reading]:
    """Wie _rows_from_per_device, aber ausgehend von bereits geladenen
    Rohmesswerten - fuer Aufrufer, die die Zeilen ohnehin schon in der Hand
    haben."""
    return _rows_from_per_device(
        aggregate_per_device(rows, bucket_seconds=_COMBINE_BUCKET_SECONDS)
    )


def _load_per_device_buckets(
    start_date: date, end_date_exclusive: date, *, padding: timedelta = timedelta(0)
) -> dict[str, dict[int, dict[str, float | None]]]:
    """Dasselbe Ergebnis wie aggregate_per_device(_load_readings_range(...),
    60), aber als GROUP BY in der Datenbank statt in Python.

    Der Unterschied ist die Datenmenge, die ueberhaupt aus der Datenbank
    herauskommt: gemessen an 35 Tagen Historie werden aus 395.000
    Rohmesswerten 100.800 Minuten-Buckets - die Verdichtung um den Faktor 4
    passiert so vor dem Netzwerkweg statt danach (gemessen 5.197 ms ->
    1.683 ms). SQLs avg() ignoriert NULL-Werte genau wie die
    Python-Variante, ein Bucket ganz ohne Messwert fuer ein Feld wird also
    auch hier NULL.

    Ein Kreuzvergleich beider Wege gegen dieselben Beispieldaten steht in
    tests/test_combined_rows_sql.py, damit sie nicht unbemerkt
    auseinanderlaufen."""
    tz = ZoneInfo(settings.timezone_name)
    since = datetime.combine(start_date, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    until = datetime.combine(end_date_exclusive, datetime.min.time(), tzinfo=tz).astimezone(
        timezone.utc
    )
    # Derselbe Bucket-Schluessel wie aggregation._bucket_key: auf ganze
    # Vielfache der Bucket-Laenge seit der Epoche abgerundet, also
    # unabhaengig von jeder Zeitzone.
    # Der Cast nach BIGINT ist kein Schoenheitsfehler: extract(epoch ...)
    # liefert in PostgreSQL numeric, und ohne Cast rechnet die Datenbank die
    # ganze Bucket-Bildung in Festkomma-Arithmetik und gibt 100.000 Decimal-
    # Objekte zurueck, die Python einzeln umwandeln muss. Gemessen an 35
    # Tagen Historie: 2.364 ms ohne, 1.683 ms mit Cast.
    bucket = cast(
        func.floor(func.extract("epoch", Reading.timestamp) / _COMBINE_BUCKET_SECONDS)
        * _COMBINE_BUCKET_SECONDS,
        BigInteger,
    ).label("bucket")

    session = SessionLocal()
    try:
        rows = session.execute(
            select(
                Reading.device_id,
                bucket,
                *[func.avg(getattr(Reading, field)).label(field) for field in HISTORY_FIELDS],
            )
            .where(
                Reading.timestamp >= since - padding,
                Reading.timestamp < until + padding,
            )
            .group_by(Reading.device_id, bucket)
        ).all()
    finally:
        session.close()

    result: dict[str, dict[int, dict[str, float | None]]] = {}
    for row in rows:
        result.setdefault(row.device_id, {})[int(row.bucket)] = {
            field: getattr(row, field) for field in HISTORY_FIELDS
        }
    return result


def _load_rows_for_range(
    start_date: date, end_date_exclusive: date, *, padding: timedelta = timedelta(0)
) -> list[Reading]:
    """Messwerte fuer den Zeitraum - bei mehreren Wechselrichtern bereits
    hausweit kombiniert (siehe _rows_from_per_device), bei einem einzelnen
    schlicht die Rohmesswerte.

    Fasst das bisherige Paar aus _load_readings_range() und
    _combined_rows() zusammen, damit bei mehreren Geraeten gar nicht erst
    saemtliche Rohmesswerte nach Python wandern muessen."""
    if len(settings.inverters) > 1:
        return _rows_from_per_device(
            _load_per_device_buckets(start_date, end_date_exclusive, padding=padding)
        )
    return _load_readings_range(start_date, end_date_exclusive, padding=padding)


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
            # select(nur benoetigte Spalten) statt select(Reading): vermeidet
            # sowohl die volle ORM-Objekterzeugung (mit Abstand der teuerste
            # Teil einer solchen Bulk-Anfrage) als auch das Mitladen
            # ungenutzter Spalten (siehe _BULK_READING_COLUMNS oben).
            # integrate_pure_pv_kwh/integrate_kwh greifen nur per getattr()
            # zu, das funktioniert mit Row-Objekten identisch.
            rows = session.execute(
                select(*_BULK_READING_COLUMNS)
                .where(Reading.device_id == cfg.id, Reading.timestamp >= since)
                .order_by(Reading.timestamp)
            ).all()
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
            # select(nur benoetigte Spalten) statt select(Reading), siehe oben.
            rows = session.execute(
                select(*_BULK_READING_COLUMNS).where(Reading.timestamp >= since).order_by(Reading.timestamp)
            ).all()
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


def _load_readings_range(
    start_date: date, end_date_exclusive: date, *, padding: timedelta = timedelta(0)
) -> list[Reading]:
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
        # NUR die tatsaechlich von den Aufrufern (build_feed_in_summary,
        # build_pv_yield_summary, build_battery_energy_summary,
        # build_daily_home_breakdown - direkt oder ueber _combined_rows())
        # benoetigten Felder laden, nicht select(Reading.__table__) mit
        # allen 20+ Spalten (siehe _BULK_READING_COLUMNS oben): bei einem
        # kalten Cache (z.B. "dieses/letztes Jahr" vor der ersten Berechnung,
        # potenziell ein ganzes Jahr an Rohmesswerten) macht allein das
        # ungenutzte Mitladen von device_name, den drei PV-String- und den
        # drei Tageszaehler-Feldern einen spuerbaren Unterschied (gemessen:
        # mehrere Sekunden bis in den zweistelligen Sekundenbereich bei
        # einigen Mio. Zeilen).
        return session.execute(
            select(*_BULK_READING_COLUMNS)
            .where(Reading.timestamp >= since - padding, Reading.timestamp < until + padding)
            .order_by(Reading.timestamp)
        ).all()
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
        rows = _load_rows_for_range(start, end_exclusive)
        return {d["date"]: d["kwh"] for d in daily_kwh_totals(rows, field, settings.timezone_name)}

    per_day = _cached_daily_totals(f"field:{field}", earliest, today, compute)
    return _periods_from_per_day(periods, per_day)


def build_feed_in_summary() -> list[FeedInPeriod]:
    """Einspeisung (kWh) je Zeitraum (integriert)."""
    return build_energy_period_summary("feed_in_power_w")


def _compute_pv_yield_days(start: date, end_exclusive: date) -> dict[str, float | None]:
    """Gemeinsame compute_missing()-Funktion fuer _cached_daily_totals() mit
    dem Cache-Feld "pv_yield" - von build_pv_yield_summary() UND
    build_yearly_comparison() genutzt, damit beide denselben Cache-Inhalt
    teilen (keine doppelte Integration derselben Tage) und die Formel nur an
    einer Stelle gepflegt wird."""
    rows = _load_readings_range(start, end_exclusive)
    return {d["date"]: d["kwh"] for d in daily_pv_yield_totals(rows, settings.timezone_name)}


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

    per_day = _cached_daily_totals("pv_yield", earliest, today, _compute_pv_yield_days)
    return _periods_from_per_day(periods, per_day)


def build_battery_energy_summary() -> dict[str, list[FeedInPeriod]]:
    """Laden und Entladen gemeinsam berechnen und abgeschlossene Tage cachen.

    Ein angefragtes Zeitfenster wird nur einmal geladen und integriert.
    Cache-Version und Konfiguration verhindern die Wiederverwendung alter
    Ergebnisse nach Formel- oder Vorzeichenkorrekturen.
    """
    periods = _energy_period_ranges()
    earliest = min(start for _, start, _ in periods)
    today = datetime.now(ZoneInfo(settings.timezone_name)).date()
    inverted = _battery_inverted_map()
    config_key = hashlib.sha256(
        repr((settings.timezone_name, sorted(inverted.items()))).encode()
    ).hexdigest()[:16]
    computed: dict[tuple[date, date], list[dict]] = {}

    def compute(start: date, end: date) -> list[dict]:
        key = (start, end)
        if key not in computed:
            # Nur ein maximales Integrationsintervall je Seite hinzuladen,
            # damit Messpaare ueber Mitternacht beruecksichtigt werden.
            rows = _load_readings_range(start, end, padding=timedelta(minutes=30))
            computed[key] = daily_battery_energy_flows(rows, settings.timezone_name, inverted)
        return computed[key]

    result = {}
    for direction in (BATTERY_CHARGE, BATTERY_DISCHARGE):
        per_day = _cached_daily_totals(
            f"battery:v2:{config_key}:{direction}", earliest, today,
            lambda start, end: {d["date"]: d[direction] for d in compute(start, end)},
        )
        result[f"{direction}_periods"] = _periods_from_per_day(periods, per_day)
    return result


_YEARLY_COMPARISON_MONTH_LABELS = [
    "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
    "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
]
# Obergrenze statt 52 - manche Jahre haben nach ISO 8601 eine 53. Kalenderwoche
# (siehe date.isocalendar()); ungenutzte Positionen bleiben fuer die
# betroffenen Jahre einfach None (siehe build_yearly_comparison unten).
_YEARLY_COMPARISON_WEEK_COUNT = 53


def build_yearly_comparison(
    granularity: str = "month", years: int | None = None
) -> dict:
    """PV-Ertrag (kWh) je Kalendermonat ODER ISO-Kalenderwoche, gruppiert
    nach Jahr - fuer den Jahresvergleich im "Verlauf"-Tab: jedes Jahr eine
    eigene Kurve auf einer FESTEN Jan-Dez- bzw. KW1-53-Achse, damit sich
    mehrere Jahre direkt uebereinanderlegen lassen (analog zum
    Tagesvergleich, nur auf Jahresebene statt Tagesebene).

    granularity: "month" (Standard, 12 Positionen) oder "week" (53
    Positionen, siehe _YEARLY_COMPARISON_WEEK_COUNT). Bei "week" wird nach
    dem ISO-Kalenderjahr/-woche gruppiert (date.isocalendar()), NICHT nach
    dem Kalenderjahr des Tages selbst - sonst wuerden die letzten Tage im
    Dezember bzw. die ersten Tage im Januar (die laut ISO 8601 oft zur
    Woche des jeweils ANDEREN Jahres gehoeren) dem falschen Jahr/der
    falschen Woche zugeschlagen.

    `years`: bei Angabe werden nur die letzten `years` Kalenderjahre (mit
    Daten) zurueckgegeben - analog zum `years`-Parameter bei
    build_autarky_yearly_comparison(). None (Standard) liefert die
    komplette Historie.

    Anders als build_autarky_monthly_summary() (deren Nachfolger
    build_autarky_yearly_comparison() dasselbe Verhalten uebernimmt, siehe
    dort) behaelt jedes zurueckgegebene Jahr IMMER alle
    12 bzw. 53 Positionen (mit null fuer eine Position ohne Daten) - sonst
    wuerde die feste Achsen-Zuordnung (Position 0 = Januar/KW1 in jedem
    Jahr) durcheinandergeraten."""
    if granularity not in ("month", "week"):
        raise ValueError(f"Unbekannte Granularitaet: {granularity!r}")

    earliest = _earliest_reading_date()
    if earliest is None:
        return {"granularity": granularity, "labels": [], "years": []}
    today = datetime.now(ZoneInfo(settings.timezone_name)).date()

    per_day = _cached_daily_totals("pv_yield", earliest, today, _compute_pv_yield_days)

    if granularity == "week":
        num_positions = _YEARLY_COMPARISON_WEEK_COUNT
        labels = [f"KW {i}" for i in range(1, num_positions + 1)]

        def position_key(day: date) -> tuple[int, int]:
            iso_year, iso_week, _iso_weekday = day.isocalendar()
            return iso_year, iso_week
    else:
        num_positions = 12
        labels = list(_YEARLY_COMPARISON_MONTH_LABELS)

        def position_key(day: date) -> tuple[int, int]:
            return day.year, day.month

    totals: dict[tuple[int, int], float] = {}
    has_data: set[tuple[int, int]] = set()
    day = earliest
    while day <= today:
        value = per_day.get(day.strftime("%Y-%m-%d"))
        if value is not None:
            key = position_key(day)
            totals[key] = totals.get(key, 0.0) + value
            has_data.add(key)
        day += timedelta(days=1)

    all_years = sorted({year for year, _position in has_data})
    if years is not None and years > 0:
        all_years = all_years[-years:]

    result_years = []
    for year in all_years:
        values: list[float | None] = []
        for position in range(1, num_positions + 1):
            key = (year, position)
            values.append(round(totals[key], 3) if key in has_data else None)
        result_years.append({"year": year, "values": values})

    return {"granularity": granularity, "labels": labels, "years": result_years}


def build_daily_home_breakdown(days: int = 30) -> list[DailyHomeBreakdownDay]:
    """Hausverbrauch je Tag, aufgeschlüsselt nach PV-/Batterie-/Netz-Anteil
    (siehe main.get_daily_home_breakdown). Für den Mail-Report wird davon
    nur der letzte (heutige) Eintrag verwendet.

    Läuft über denselben Tages-Cache wie die übrigen Zeitraum-Übersichten
    (siehe _cached_home_source_breakdown). Vorher wurden bei JEDEM Aufruf
    sämtliche Rohmesswerte des angefragten Zeitraums geladen und in Python
    durchgerechnet - bei der Voreinstellung von 30 Tagen rund 330.000
    Zeilen und damit mehrere Sekunden, bei den in der Oberfläche
    wählbaren 365 Tagen entsprechend mehr. Da ein abgeschlossener Tag sich
    nicht mehr ändert (außer durch einen nachträglichen Logdaten-Import,
    der den Cache gezielt verwirft), ist das reine Wiederholungsarbeit.

    Beide Lesarten der Aufteilung werden gebraucht und deshalb gemeinsam
    geholt: die milde für die angezeigten Werte, die strenge für den
    Autarkiegrad (siehe _BREAKDOWN_VARIANT/_AUTARKY_VARIANT).
    """
    today = datetime.now(ZoneInfo(settings.timezone_name)).date()
    earliest_stored = _earliest_reading_date()
    if earliest_stored is None:
        return []
    # Nicht weiter zurück als bis zum ersten Messwert: sonst landeten für
    # jeden Tag davor leere Cache-Zeilen in der Datenbank.
    earliest = max(today - timedelta(days=days - 1), earliest_stored)
    if earliest > today:
        return []

    per_day = _cached_home_source_breakdown(
        earliest, today, (_BREAKDOWN_VARIANT, _AUTARKY_VARIANT)
    )

    # In DailyHomeBreakdownDay-Objekte wandeln (statt roher Dicts), damit
    # sowohl der API-Endpunkt als auch der Mail-Report per Attribut darauf
    # zugreifen koennen (der Report ruft z.B. .pv_kwh direkt auf). Ergaenzt
    # um den Autarkiegrad des jeweiligen Tages (siehe _autarky_percent) -
    # fuer die "Autarkiegrad heute"-Kachel in der Uebersicht sowie als
    # Zusatzinfo im Tagesverbrauch-Diagramm.
    #
    # Tage ganz ohne verwertbare Messwerte werden ausgelassen (alle drei
    # Anteile unbekannt) - so wie vorher, als solche Tage in
    # daily_home_source_breakdown_kwh() gar nicht erst entstanden sind.
    result: list[DailyHomeBreakdownDay] = []
    for date_str, by_variant in sorted(per_day.items()):
        werte = by_variant[_BREAKDOWN_VARIANT.name]
        if all(werte.get(key) is None for key in _BREAKDOWN_OUT_KEYS):
            continue
        autarkie = by_variant[_AUTARKY_VARIANT.name]
        result.append(
            DailyHomeBreakdownDay(
                date=date_str,
                pv_kwh=werte.get("pv_kwh"),
                battery_kwh=werte.get("battery_kwh"),
                grid_kwh=werte.get("grid_kwh"),
                autarky_percent=_autarky_percent(
                    autarkie.get("pv_kwh"),
                    autarkie.get("battery_kwh"),
                    autarkie.get("grid_kwh"),
                ),
            )
        )
    return result


def _earliest_reading_date() -> date | None:
    """Lokales Kalenderdatum des allerersten gespeicherten Messwerts (ueber
    alle Geraete) - Startpunkt fuer den Autarkiegrad-Jahresvergleich UND den
    PV-Ertrag-Jahresvergleich (siehe build_autarky_yearly_comparison/
    build_yearly_comparison), da dort (anders als bei den neun
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


@dataclass(frozen=True)
class _BreakdownVariant:
    """Eine der beiden Lesarten der Hausverbrauchs-Aufteilung.

    Sie unterscheiden sich nur darin, WELCHE Messpunkte eingehen (siehe
    _home_source_breakdown_with_grid), liefern aber dieselben drei Werte.
    Weil beide denselben teuren Rohdaten-Scan brauchen, werden sie
    gemeinsam berechnet und gemeinsam gecacht - unter je eigenen
    Feldnamen in daily_energy_cache, damit sie sich nicht vermischen.
    """

    name: str
    # out_key (pv_kwh/battery_kwh/grid_kwh) -> Feldname in daily_energy_cache
    fields: dict[str, str]
    compute: Callable[[list[Reading]], list[dict]]


def _home_source_breakdown_all_rows(rows: list[Reading]) -> list[dict]:
    """Milde Lesart: ein fehlender Netzwert zaehlt als 0, der Messpunkt
    bleibt erhalten (siehe daily_home_source_breakdown_kwh). So zeigt das
    Tagesverbrauchs-Diagramm auch bei einzelnen Zaehler-Luecken noch eine
    vollstaendige Aufteilung."""
    return daily_home_source_breakdown_kwh(rows, settings.timezone_name)


# Fuer das Tagesverbrauchs-Diagramm (build_daily_home_breakdown).
_BREAKDOWN_VARIANT = _BreakdownVariant(
    name="breakdown",
    fields={
        "pv_kwh": "home_breakdown_pv",
        "battery_kwh": "home_breakdown_battery",
        "grid_kwh": "home_breakdown_grid",
    },
    compute=_home_source_breakdown_all_rows,
)

# Fuer den Autarkiegrad (build_autarky_yearly_comparison sowie die
# Autarkie-Spalte im Tagesverbrauch). Strenge Lesart - siehe
# _home_source_breakdown_with_grid, warum hier nicht 0 angenommen werden
# darf.
_AUTARKY_VARIANT = _BreakdownVariant(
    name="autarky",
    fields={
        "pv_kwh": "home_source_pv",
        "battery_kwh": "home_source_battery",
        "grid_kwh": "home_source_grid",
    },
    compute=_home_source_breakdown_with_grid,
)

_BREAKDOWN_OUT_KEYS = ("pv_kwh", "battery_kwh", "grid_kwh")


def _cached_home_source_breakdown(
    earliest: date, today: date, variants: Sequence[_BreakdownVariant]
) -> dict[str, dict[str, dict[str, float | None]]]:
    """Liefert {date_str: {variante: {"pv_kwh": .., "battery_kwh": ..,
    "grid_kwh": ..}}} fuer [earliest, today] (inklusive), unter Nutzung von
    daily_energy_cache - abgeschlossene Tage werden dauerhaft
    zwischengespeichert, "heute" wird wie bei _cached_daily_totals nie
    gecacht, sondern bei jedem Aufruf frisch berechnet.

    WICHTIG: berechnet ALLE angeforderten Varianten und alle drei Anteile
    je Variante in EINEM Durchlauf ueber die Rohmesswerte einer
    Cache-Luecke, statt _cached_daily_totals mehrfach (einmal je Anteil)
    mit je eigenem compute() aufzurufen. Eine fruehere Version tat genau
    das und nahm den mehrfachen Rohdaten-Scan bei einer Luecke bewusst in
    Kauf ("faellt in der Praxis nicht ins Gewicht, da nur einmalig") - bei
    groesseren Bestaenden (viele Monate an 15s-Messwerten) macht das das
    erste Laden aber spuerbar langsam.
    """
    field_names = [name for variant in variants for name in variant.fields.values()]

    session = SessionLocal()
    try:
        cached_rows = list(
            session.scalars(
                select(DailyEnergyCache).where(
                    DailyEnergyCache.field.in_(field_names),
                    DailyEnergyCache.date >= earliest.strftime("%Y-%m-%d"),
                    DailyEnergyCache.date < today.strftime("%Y-%m-%d"),
                )
            )
        )
    finally:
        session.close()

    # date_str -> Cache-Feldname -> kWh
    cached: dict[str, dict[str, float | None]] = {}
    for row in cached_rows:
        cached.setdefault(row.date, {})[row.field] = row.kwh

    num_closed_days = (today - earliest).days
    all_closed_dates = {
        (earliest + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(num_closed_days)
    }
    # Ein Tag gilt nur als vollstaendig gecacht, wenn ALLE angeforderten
    # Felder vorliegen - fehlt auch nur eines, wird der Tag
    # sicherheitshalber komplett neu berechnet statt mit einer Luecke
    # weiterverwendet zu werden (analog zu _cached_daily_totals).
    missing_closed_dates = sorted(
        d for d in all_closed_dates if not all(name in cached.get(d, {}) for name in field_names)
    )

    if missing_closed_dates:
        # Wie bei _cached_daily_totals: die gesamte Luecke in EINEM Rutsch
        # nachberechnen (ein einziger Rohdaten-Scan fuer alle Varianten
        # zusammen), statt Tag fuer Tag oder Variante fuer Variante.
        gap_start = datetime.strptime(missing_closed_dates[0], "%Y-%m-%d").date()
        gap_end_exclusive = datetime.strptime(
            missing_closed_dates[-1], "%Y-%m-%d"
        ).date() + timedelta(days=1)
        rows = _load_rows_for_range(gap_start, gap_end_exclusive)

        fresh_by_variant = {
            variant.name: {day["date"]: day for day in variant.compute(rows)}
            for variant in variants
        }

        session = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            for date_str in missing_closed_dates:
                for variant in variants:
                    fresh = fresh_by_variant[variant.name].get(date_str, {})
                    for out_key, field_name in variant.fields.items():
                        value = fresh.get(out_key)
                        cached.setdefault(date_str, {})[field_name] = value
                        session.merge(
                            DailyEnergyCache(
                                field=field_name,
                                date=date_str,
                                kwh=value,
                                computed_at=now,
                            )
                        )
            session.commit()
        finally:
            session.close()

    today_rows = _load_rows_for_range(today, today + timedelta(days=1))
    today_str = today.strftime("%Y-%m-%d")
    for variant in variants:
        computed = variant.compute(today_rows)
        fresh = computed[0] if computed else {}
        for out_key, field_name in variant.fields.items():
            cached.setdefault(today_str, {})[field_name] = fresh.get(out_key)

    return {
        date_str: {
            variant.name: {
                out_key: by_field.get(field_name)
                for out_key, field_name in variant.fields.items()
            }
            for variant in variants
        }
        for date_str, by_field in cached.items()
    }


def build_autarky_yearly_comparison(granularity: str = "month", years: int | None = None) -> dict:
    """Autarkiegrad (%) je Kalendermonat ODER ISO-Kalenderwoche, gruppiert
    nach Jahr - wie build_yearly_comparison() fuer den PV-Ertrag, nur fuer
    den Autarkiegrad: jedes Jahr eine eigene Kurve auf einer FESTEN
    Jan-Dez- bzw. KW1-53-Achse, damit sich mehrere Jahre direkt
    uebereinanderlegen lassen (statt einer einzigen durchgehenden Linie
    ueber die gesamte Historie).

    Ein Positionswert ist NICHT der Mittelwert der taeglichen
    Prozentsaetze, sondern wird aus den ueber die Position (Monat/Woche)
    aufsummierten kWh-Anteilen berechnet (siehe _autarky_percent) - sonst
    wuerden Tage mit wenig Hausverbrauch (z.B. Abwesenheit) das Ergebnis
    unverhaeltnismaessig verzerren, obwohl sie kaum zum tatsaechlichen
    Verbrauch beitragen.

    granularity/years: siehe build_yearly_comparison(). Anders als die
    fruehere build_autarky_monthly_summary() (die Monate OHNE jegliche
    Daten komplett ausliess) behaelt jedes zurueckgegebene Jahr IMMER alle
    12 bzw. 53 Positionen (None fuer eine Position ohne Daten) - sonst
    wuerde die feste Achsen-Zuordnung (Position 0 = Januar/KW1 in jedem
    Jahr) durcheinandergeraten."""
    if granularity not in ("month", "week"):
        raise ValueError(f"Unbekannte Granularitaet: {granularity!r}")

    earliest = _earliest_reading_date()
    if earliest is None:
        return {"granularity": granularity, "labels": [], "years": []}
    today = datetime.now(ZoneInfo(settings.timezone_name)).date()

    per_day_variants = _cached_home_source_breakdown(earliest, today, (_AUTARKY_VARIANT,))
    per_day = {
        date_str: by_variant[_AUTARKY_VARIANT.name]
        for date_str, by_variant in per_day_variants.items()
    }

    if granularity == "week":
        num_positions = _YEARLY_COMPARISON_WEEK_COUNT
        labels = [f"KW {i}" for i in range(1, num_positions + 1)]

        def position_key(day: date) -> tuple[int, int]:
            iso_year, iso_week, _iso_weekday = day.isocalendar()
            return iso_year, iso_week
    else:
        num_positions = 12
        labels = list(_YEARLY_COMPARISON_MONTH_LABELS)

        def position_key(day: date) -> tuple[int, int]:
            return day.year, day.month

    sums: dict[tuple[int, int], dict[str, float]] = {}
    has_data: set[tuple[int, int]] = set()
    day = earliest
    while day <= today:
        date_str = day.strftime("%Y-%m-%d")
        key = position_key(day)
        entry = sums.setdefault(key, {"pv_kwh": 0.0, "battery_kwh": 0.0, "grid_kwh": 0.0})
        day_values = per_day.get(date_str, {})
        for out_key in _BREAKDOWN_OUT_KEYS:
            value = day_values.get(out_key)
            if value is not None:
                entry[out_key] += value
                has_data.add(key)
        day += timedelta(days=1)

    all_years = sorted({year for year, _position in has_data})
    if years is not None and years > 0:
        all_years = all_years[-years:]

    result_years = []
    for year in all_years:
        values: list[float | None] = []
        for position in range(1, num_positions + 1):
            key = (year, position)
            if key in has_data:
                entry = sums[key]
                values.append(
                    _autarky_percent(entry["pv_kwh"], entry["battery_kwh"], entry["grid_kwh"])
                )
            else:
                values.append(None)
        result_years.append({"year": year, "values": values})

    return {"granularity": granularity, "labels": labels, "years": result_years}
