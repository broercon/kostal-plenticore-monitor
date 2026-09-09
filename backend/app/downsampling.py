"""Verdichtet alte Rohmesswerte auf Stundenmittelwerte, um das Wachstum der
readings-Tabelle auf Dauer zu begrenzen (siehe docs/CALCULATIONS.md
"Verdichtung alter Rohdaten"): bei 15s-Polling entstehen pro Wechselrichter
und Tag ueber 5.700 Zeilen - fuer die Anzeige (Diagramme, Zeitraum-
Uebersichten) reicht fuer aeltere Daten eine deutlich groebere Aufloesung,
die Prognose (siehe energy_forecast.py) arbeitet ohnehin ausschliesslich mit
Stundenwerten.

Ab settings.raw_data_retention_days (Standard 60 Tage) werden abgeschlossene
lokale Kalendertage geraeteweise auf einen Messpunkt pro Stunde reduziert:
alle Leistungsfelder werden ueber die Stunde gemittelt (energieerhaltend -
Mittelwert * 1h = Energie dieser Stunde, siehe integrate_kwh), der
Ladezustand (battery_soc_percent, ein Zustand statt einer Fluessgroesse)
wird stattdessen auf den letzten bekannten Wert der Stunde gesetzt. Die
urspruenglichen Einzelmesswerte dieser Stunde werden dabei GELOESCHT und
durch die eine gemittelte Zeile ersetzt (markiert mit is_downsampled=True,
siehe models.Reading).

WICHTIG: das aendert die Aufloesung, mit der integrate_kwh() alte Tage
integrieren wuerde (siehe aggregation.gap_hours_for_day - der normale
1h-Abstand zwischen verdichteten Punkten ist keine Datenluecke). Es aendert
NICHT die bereits berechneten Zeitraum-Uebersichten (daily_energy_cache):
ein abgeschlossener Kalendertag wird dort spaetestens am Folgetag dauerhaft
zwischengespeichert (siehe daily_summary._cached_daily_totals) - lange bevor
er ueberhaupt "alt genug" fuer diese Verdichtung ist. Betroffen sind nur
Faelle, die alte Rohmesswerte NACH der Verdichtung noch einmal lesen:
/api/readings/daily-totals und /api/readings/history fuer sehr weit
zurueckliegende Zeitraeume (dort nur als Verlust an Anzeige-Feinheit, siehe
aggregate_per_device - keine Integration, keine Luecken-Problematik) sowie
ein nachtraeglicher Logdaten-Reimport, der eine bereits verdichtete
Cache-Periode invalidiert (siehe gap_hours_for_day) oder denselben
historischen Zeitraum erneut importiert (siehe import_logdata.import_rows(),
das is_downsampled-markierte Stunden gezielt ueberspringt statt sie mit den
urspruenglichen, feineren Zeitstempeln wieder aufzublaehen).

Bekannter, bewusst in Kauf genommener Randeffekt: der repraesentative
Zeitstempel einer verdichteten Stunde liegt auf der STUNDENMITTE (siehe
_hour_midpoint_utc), die 24 Punkte eines Tages ueberspannen also nur 23h
(00:30 bis 23:30 lokal) statt der vollen 24h. Eine je Kalendertag GRUPPIERTE
Neuberechnung (daily_kwh_totals & Co. integrieren jeden Tag einzeln, nicht
ueber Tagesgrenzen hinweg) unterschaetzt einen bereits verdichteten Tag
dadurch um bis zu 1 Stunde (~4 % bei einer 24h-Anlage) - siehe
test_downsampling.py fuer eine konkrete Gegenueberstellung. Betrifft wie
oben nur eine seltene Neuberechnung, NIE die bereits im daily_energy_cache
abgelegten (und mit den vollaufgeloesten Rohdaten berechneten) Zeitraum-
Summen.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from .config import settings
from .database import SessionLocal
from .models import DownsampleState, Reading

logger = logging.getLogger(__name__)

# Leistungsfelder, die als arithmetisches Mittel ueber die Stunde verdichtet
# werden - siehe Moduldocstring, warum das energieerhaltend ist.
AVERAGED_FIELDS = [
    "home_power_w",
    "grid_power_w",
    "feed_in_power_w",
    "grid_draw_power_w",
    "pv_power_w",
    "ac_power_w",
    "battery_power_w",
    "pv1_power_w",
    "pv2_power_w",
    "pv3_power_w",
]

_STATE_ID = 1


def local_hour_start_utc(ts: datetime, tz: ZoneInfo) -> datetime:
    """Beginn der LOKALEN Kalenderstunde von ts (in tz), als UTC-aware
    datetime - der Bucket-Schluessel fuer die Verdichtung. Wird auch von
    import_logdata.import_rows() genutzt, um zu pruefen, ob eine Stunde
    bereits verdichtet wurde - beide Stellen MUESSEN dieselbe Formel
    verwenden, sonst greift der Abgleich nicht."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(tz)
    local_hour_start = local.replace(minute=0, second=0, microsecond=0)
    return local_hour_start.astimezone(timezone.utc)


def _hour_midpoint_utc(hour_start_utc: datetime) -> datetime:
    """Repraesentativer Zeitstempel einer verdichteten Stunde: die Mitte
    (Beginn + 30 Minuten). Fuer JEDES Geraet identisch (haengt nur von der
    Stunde ab, nicht vom Geraet) - so landen mehrere Geraete derselben
    Stunde beim spaeteren Kombinieren (aggregate_per_device/combine_devices,
    z.B. in daily_summary._combined_rows mit 60s-Buckets) weiterhin im
    selben Bucket, wie es dort fuer eine korrekte hausweite Bilanz
    vorausgesetzt wird."""
    return hour_start_utc + timedelta(minutes=30)


def _average(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _last(values: list[float | None]) -> float | None:
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _downsample_day(session, device_id: str, day_start_utc: datetime, day_end_utc: datetime, tz: ZoneInfo) -> int:
    """Verdichtet einen einzelnen lokalen Kalendertag fuer ein Geraet.
    Gibt die Anzahl der geloeschten (durch je 1 Stundenmittel ersetzten)
    Rohmesswerte zurueck. Committet einmal am Ende (Insert + Delete in
    derselben Transaktion, damit ein Absturz mittendrin nie Daten verliert -
    entweder die Stunde ist noch vollstaendig roh, oder vollstaendig
    verdichtet, nie etwas dazwischen)."""
    rows = list(
        session.scalars(
            select(Reading)
            .where(
                Reading.device_id == device_id,
                Reading.timestamp >= day_start_utc,
                Reading.timestamp < day_end_utc,
            )
            .order_by(Reading.timestamp)
        )
    )
    if not rows:
        return 0

    by_hour: dict[datetime, list[Reading]] = {}
    for row in rows:
        by_hour.setdefault(local_hour_start_utc(row.timestamp, tz), []).append(row)

    # IDs erst sammeln und am Ende in EINEM Rutsch loeschen (statt pro Zeile
    # einzeln per session.delete()) - bei mehreren tausend Rohmesswerten pro
    # Geraet und Tag (15s-Polling) waeren das sonst ebenso viele einzelne
    # DELETE-Statements.
    ids_to_delete: list[int] = []
    new_rows: list[Reading] = []
    for hour_start, hour_rows in by_hour.items():
        # Schon verdichtet (genau 1 Zeile) oder eine natuerlich duennbesetzte
        # Stunde (z.B. kurzer Ausfall) - in beiden Faellen nichts zu tun,
        # das Ergebnis waere identisch zur bestehenden einzelnen Zeile.
        if len(hour_rows) <= 1:
            continue

        averaged = {field: _average([getattr(r, field) for r in hour_rows]) for field in AVERAGED_FIELDS}
        soc = _last([r.battery_soc_percent for r in hour_rows])
        representative = hour_rows[0]

        new_rows.append(
            Reading(
                device_id=device_id,
                device_name=representative.device_name,
                timestamp=_hour_midpoint_utc(hour_start),
                battery_soc_percent=soc,
                is_downsampled=True,
                **averaged,
            )
        )
        ids_to_delete.extend(r.id for r in hour_rows)

    if not ids_to_delete:
        return 0

    session.add_all(new_rows)
    session.execute(delete(Reading).where(Reading.id.in_(ids_to_delete)))
    session.commit()
    return len(ids_to_delete)


def _get_watermark(session) -> date | None:
    state = session.get(DownsampleState, _STATE_ID)
    if state is None or state.downsampled_until_date is None:
        return None
    return datetime.strptime(state.downsampled_until_date, "%Y-%m-%d").date()


def _set_watermark(session, until_date: date) -> None:
    now = datetime.now(timezone.utc)
    state = session.get(DownsampleState, _STATE_ID)
    if state is None:
        state = DownsampleState(id=_STATE_ID, downsampled_until_date=until_date.strftime("%Y-%m-%d"), updated_at=now)
        session.add(state)
    else:
        state.downsampled_until_date = until_date.strftime("%Y-%m-%d")
        state.updated_at = now
    session.commit()


def _earliest_reading_date(session, tz: ZoneInfo) -> date | None:
    first = session.scalar(select(Reading.timestamp).order_by(Reading.timestamp).limit(1))
    if first is None:
        return None
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return first.astimezone(tz).date()


def run_downsample_once(*, now: datetime | None = None) -> dict:
    """Verdichtet alle abgeschlossenen lokalen Kalendertage vom letzten
    Wasserstand (siehe DownsampleState) bis settings.raw_data_retention_days
    vor heute - fuer JEDES in der Datenbank vorkommende Geraet (nicht nur
    aktuell konfigurierte, damit ein zwischenzeitlich entferntes Geraet
    seine Altdaten trotzdem verdichtet bekommt).

    Synchron/blockierend (reine DB-Arbeit, ein Tag nach dem anderen, mit
    Zwischenstand nach jedem Tag - siehe _set_watermark). Der aufrufende
    Hintergrund-Task (main.py) ruft diese Funktion ueber
    asyncio.to_thread() auf, damit ein groesserer Nachholbedarf (z.B.
    direkt nach Einfuehrung dieses Features bei einer bereits lange
    laufenden Anlage) die Web-Oberflaeche nicht blockiert.

    Gibt {"days_processed", "rows_deleted", "up_to_date"} zurueck.
    """
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.timezone_name)
    cutoff = now.astimezone(tz).date() - timedelta(days=settings.raw_data_retention_days)

    session = SessionLocal()
    try:
        watermark = _get_watermark(session)
        if watermark is None:
            watermark = _earliest_reading_date(session, tz)
        if watermark is None or watermark >= cutoff:
            return {"days_processed": 0, "rows_deleted": 0, "up_to_date": True}

        device_ids = [
            row[0] for row in session.execute(select(Reading.device_id).distinct())
        ]
    finally:
        session.close()

    days_processed = 0
    rows_deleted = 0
    day = watermark
    while day < cutoff:
        day_start_utc = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
        day_end_utc = datetime.combine(
            day + timedelta(days=1), datetime.min.time(), tzinfo=tz
        ).astimezone(timezone.utc)

        session = SessionLocal()
        try:
            for device_id in device_ids:
                rows_deleted += _downsample_day(session, device_id, day_start_utc, day_end_utc, tz)
            day += timedelta(days=1)
            _set_watermark(session, day)
        finally:
            session.close()
        days_processed += 1

    logger.info(
        "Verdichtung alter Rohmesswerte: %d Tag(e) verarbeitet, %d Rohmesswerte zusammengefasst.",
        days_processed,
        rows_deleted,
    )
    return {"days_processed": days_processed, "rows_deleted": rows_deleted, "up_to_date": False}
