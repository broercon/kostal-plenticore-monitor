"""Speicher-Erweiterung: lohnt sich ein größerer Speicher?

Simuliert aus den gemessenen Daten, wie viel Netzbezug ein um ein oder zwei
Module erweiterter Speicher (BYD HVM: 11,04 -> 13,8 / 16,56 kWh) vermieden
hätte. Ablauf:

1. Je abgeschlossenem Kalendertag werden die Messwerte zu 5-Minuten-Schritten
   verdichtet (reine PV aller Wechselrichter, Hausverbrauch aus der
   korrigierten Energiebilanz, gemessener Netzbezug/Einspeisung, gemessene
   Batterieleistung und Ladestand) und in battery_sizing_days gecacht.
2. Aus der gemessenen Batterieleistung und dem Ladestand werden nutzbare
   Kapazität, Wirkungsgrade, Standby, Mindestladung und Leistungsgrenzen des
   vorhandenen Speichers kalibriert (Einstellungen im Admin-Bereich haben
   Vorrang).
3. Drei Speicher mit identischer Logik werden fortlaufend über alle Tage
   simuliert - nur aus PV-Überschuss geladen, bis zur Mindestladung entladen.
   Das 11,04-kWh-Modell wird gegen Messung validiert.
4. Nutzen = simulierter Netzbezug (11,04) - simulierter Netzbezug (Variante),
   je Tag; Monate und Gesamtwerte sind Summen dieser Tageswerte.

Bewusst wird hier nichts hochgerechnet: Monate ohne Messdaten bleiben leer.
Die Aussage wird mit jedem weiteren Tag vollständiger.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import BigInteger, cast, delete, func, select

from .aggregation import AGGREGATED_FIELDS, HISTORY_FIELDS, PV_PURE_FIELD, combine_devices, pure_pv_sql_expression
from .battery_sizing_config import get_config
from .config import settings
from .database import SessionLocal
from .models import BatterySizingDay, Reading

logger = logging.getLogger(__name__)

SLOT_SECONDS = 300
SLOT_HOURS = SLOT_SECONDS / 3600
# Erhöhen, wenn sich Inhalt oder Format der gecachten Tagesreihen ändert -
# Tage mit anderer Version werden dann neu aus den Rohmesswerten berechnet.
CACHE_VERSION = 1
# Ein Tag gilt als vollständig, wenn mindestens dieser Anteil seiner
# 5-Minuten-Schritte PV und Hausverbrauch enthält.
COMPLETE_DAY_SHARE = 0.9
# Nicht mehr als so viele Tage auf einmal aus den Rohmesswerten verdichten,
# damit ein kalter Cache (erster Aufruf nach Einführung) nicht ein ganzes
# Jahr Rohdaten in einem Rutsch in den Speicher holt.
_COMPUTE_CHUNK_DAYS = 31

# Fallbacks, wenn die Messdaten für eine Kalibrierung nicht ausreichen.
DEFAULT_EFFICIENCY = 0.95
DEFAULT_MIN_SOC_PERCENT = 5.0
DEFAULT_STANDBY_W = 10.0
DEFAULT_MAX_POWER_W = 5000.0

# Kategorien der Nutzungshäufigkeit je Kalendertag (Anteil der zusätzlich
# entnommenen Energie an der nutzbaren Zusatzkapazität).
USAGE_UNUSED = "unused"
USAGE_PARTIAL = "partial"
USAGE_FULL = "full"
USAGE_MULTI = "multi"
USAGE_CATEGORIES = (USAGE_UNUSED, USAGE_PARTIAL, USAGE_FULL, USAGE_MULTI)
USAGE_UNUSED_BELOW = 0.05
USAGE_FULL_FROM = 0.95

# Schwellen für das Erkennen von Netzladung und Abregelung in den Messwerten.
GRID_CHARGE_MIN_W = 100.0
CURTAILMENT_SHARE = 0.97

# Index der Werte innerhalb eines gecachten Schritts.
I_TS, I_PV, I_HOME, I_GRID, I_FEED, I_BAT, I_SOC = range(7)


# ---------------------------------------------------------------------------
# Datenbasis
# ---------------------------------------------------------------------------

def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)


def expected_slots(day: date, timezone_name: str) -> int:
    """Anzahl 5-Minuten-Schritte eines lokalen Kalendertags (an den
    Zeitumstellungstagen 276 bzw. 300 statt 288)."""
    tz = ZoneInfo(timezone_name)
    seconds = (_local_midnight(day + timedelta(days=1), tz) - _local_midnight(day, tz)).total_seconds()
    return int(seconds // SLOT_SECONDS)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _load_slot_rows(start: date, end_exclusive: date) -> dict[str, list[list]]:
    """5-Minuten-Schritte je lokalem Kalendertag für [start, end_exclusive).

    Je Gerät wird in der Datenbank gemittelt (wie
    daily_summary._load_per_device_buckets, nur mit 5 statt 1 Minute und
    zusätzlich dem Ladestand), danach hausweit über combine_devices()
    zusammengeführt - damit gelten dieselbe Mehrgeräte-Logik und dieselbe
    reine PV wie überall sonst in der Anwendung.

    Rückgabe: {"YYYY-MM-DD": [[epoch, pv, home, grid, feed, bat, soc], ...]},
    Schritte aufsteigend. Batterieleistung positiv = Entladen, negativ =
    Laden (bereits um battery_power_inverted korrigiert).
    """
    tz = ZoneInfo(settings.timezone_name)
    since = _local_midnight(start, tz)
    until = _local_midnight(end_exclusive, tz)
    bucket = cast(
        func.floor(func.extract("epoch", Reading.timestamp) / SLOT_SECONDS) * SLOT_SECONDS,
        BigInteger,
    ).label("bucket")

    session = SessionLocal()
    try:
        rows = session.execute(
            select(
                Reading.device_id,
                bucket,
                *[func.avg(getattr(Reading, f)).label(f) for f in HISTORY_FIELDS],
                func.avg(pure_pv_sql_expression()).label(PV_PURE_FIELD),
                func.avg(Reading.battery_soc_percent).label("soc"),
            )
            .where(Reading.timestamp >= since, Reading.timestamp < until)
            .group_by(Reading.device_id, bucket)
        ).all()
    finally:
        session.close()

    per_device: dict[str, dict[int, dict[str, float | None]]] = {}
    soc_values: dict[int, list[float]] = {}
    for row in rows:
        bk = int(row.bucket)
        per_device.setdefault(row.device_id, {})[bk] = {
            f: getattr(row, f) for f in AGGREGATED_FIELDS
        }
        if row.soc is not None:
            soc_values.setdefault(bk, []).append(float(row.soc))

    combined = combine_devices(
        per_device,
        {cfg.id: cfg.has_grid_meter for cfg in settings.inverters},
        {cfg.id: cfg.battery_power_inverted for cfg in settings.inverters},
    )

    result: dict[str, list[list]] = {}
    for bk in sorted(combined):
        values = combined[bk]
        socs = soc_values.get(bk)
        day = datetime.fromtimestamp(bk, tz=timezone.utc).astimezone(tz).date().isoformat()
        result.setdefault(day, []).append([
            bk,
            _round(values.get(PV_PURE_FIELD)),
            _round(values.get("home_power_w")),
            _round(values.get("grid_draw_power_w")),
            _round(values.get("feed_in_power_w")),
            _round(values.get("battery_power_w")),
            _round(sum(socs) / len(socs)) if socs else None,
        ])
    return result


_memo_lock = threading.Lock()
_memo: dict[str, object] = {}


def invalidate_battery_sizing_cache(start_date: date, end_date: date) -> None:
    """Verwirft gecachte Tagesreihen im Bereich (inklusive beider Enden) -
    nach einem Logdaten-Import, der diese Tage nachträglich ergänzt haben
    kann (siehe auto_import.py). Da die Simulation den Ladestand über
    Tagesgrenzen fortführt, wird ohnehin bei jedem Aufruf die ganze Reihe neu
    simuliert; hier genügt es also, die Eingangsdaten der betroffenen Tage
    und das zwischengespeicherte Ergebnis zu verwerfen."""
    session = SessionLocal()
    try:
        session.execute(
            delete(BatterySizingDay).where(
                BatterySizingDay.date >= start_date.isoformat(),
                BatterySizingDay.date <= end_date.isoformat(),
            )
        )
        session.commit()
    finally:
        session.close()
    clear_memo()


def clear_memo() -> None:
    with _memo_lock:
        _memo.clear()


def _earliest_reading_date() -> date | None:
    session = SessionLocal()
    try:
        earliest = session.scalar(select(func.min(Reading.timestamp)))
    finally:
        session.close()
    if earliest is None:
        return None
    return earliest.astimezone(ZoneInfo(settings.timezone_name)).date()


def load_day_inputs(earliest: date, last_closed: date) -> dict[str, list[list]]:
    """Tagesreihen für [earliest, last_closed] - abgeschlossene Tage werden
    einmal verdichtet und danach nur noch aus battery_sizing_days gelesen.
    Tage ohne Messwerte werden mit leerer Liste gecacht, damit sie nicht bei
    jedem Aufruf erneut gesucht werden."""
    if last_closed < earliest:
        return {}
    session = SessionLocal()
    try:
        cached = {
            row.date: json.loads(row.payload)
            for row in session.scalars(
                select(BatterySizingDay).where(
                    BatterySizingDay.version == CACHE_VERSION,
                    BatterySizingDay.date >= earliest.isoformat(),
                    BatterySizingDay.date <= last_closed.isoformat(),
                )
            )
        }
    finally:
        session.close()

    all_days = [earliest + timedelta(days=i) for i in range((last_closed - earliest).days + 1)]
    missing = [d for d in all_days if d.isoformat() not in cached]
    # Fehlende Tage zu zusammenhängenden Blöcken (max. _COMPUTE_CHUNK_DAYS)
    # zusammenfassen, damit jeder Block nur eine Datenbankabfrage braucht.
    chunks: list[tuple[date, date]] = []
    for day in missing:
        if chunks and chunks[-1][1] == day and (day - chunks[-1][0]).days < _COMPUTE_CHUNK_DAYS:
            chunks[-1] = (chunks[-1][0], day + timedelta(days=1))
        else:
            chunks.append((day, day + timedelta(days=1)))

    for chunk_start, chunk_end in chunks:
        fresh = _load_slot_rows(chunk_start, chunk_end)
        now = datetime.now(timezone.utc)
        session = SessionLocal()
        try:
            day = chunk_start
            while day < chunk_end:
                key = day.isoformat()
                slots = fresh.get(key, [])
                session.merge(
                    BatterySizingDay(
                        date=key, version=CACHE_VERSION,
                        payload=json.dumps(slots, separators=(",", ":")), computed_at=now,
                    )
                )
                cached[key] = slots
                day += timedelta(days=1)
            session.commit()
        finally:
            session.close()

    return {d: cached[d] for d in sorted(cached)}


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StorageParams:
    """Ein simulierter Speicher. Kapazität bezieht sich auf 0-100 % Ladestand
    (also inkl. Alterung), Leistungen sind DC-Batterieleistungen in W.

    Die vier conversion-Werte bilden ab, dass Überschuss und Bedarf auf der
    AC-Seite (Hausverbrauch) entstehen, die Batterie aber DC-seitig misst:
    beim Laden kommt nur charge_factor · Überschuss + charge_offset_w an,
    beim Entladen muss die Batterie discharge_factor · Bedarf +
    discharge_offset_w liefern (Wandlungsverluste und Eigenverbrauch des
    Wechselrichters, siehe calibrate_conversion)."""

    capacity_kwh: float
    min_soc: float  # Anteil 0..1
    max_charge_w: float
    max_discharge_w: float
    eta_charge: float
    eta_discharge: float
    standby_w: float
    charge_factor: float = 1.0
    charge_offset_w: float = 0.0
    discharge_factor: float = 1.0
    discharge_offset_w: float = 0.0


@dataclass
class DayResult:
    grid_kwh: float = 0.0
    feed_kwh: float = 0.0
    charge_kwh: float = 0.0  # aus PV-Überschuss in den Speicher geflossen (vor Verlusten)
    discharge_kwh: float = 0.0  # an das Haus abgegeben (nach Verlusten)
    slots: int = 0


@dataclass
class SimulationResult:
    days: dict[str, DayResult]
    # Simulierter Ladestand (%) je Schritt-Zeitstempel - nur wenn angefordert.
    soc_trace: dict[int, float] = field(default_factory=dict)
    end_energy_kwh: float = 0.0


def simulate(
    day_inputs: dict[str, list[list]],
    params: StorageParams,
    start_energy_kwh: float | None = None,
    record_soc: bool = False,
) -> SimulationResult:
    """Fortlaufende Simulation über alle Tage (Ladestand wird über
    Tagesgrenzen mitgeführt; beliebig viele Zyklen pro Tag).

    Je 5-Minuten-Schritt: Überschuss = PV - Hausverbrauch. Positiver
    Überschuss lädt (begrenzt durch Ladeleistung und freien Platz, mit
    Ladewirkungsgrad), der Rest wird eingespeist. Negativer Überschuss wird
    aus dem Speicher gedeckt (begrenzt durch Entladeleistung und Energie
    oberhalb der Mindestladung, mit Entladewirkungsgrad), der Rest kommt aus
    dem Netz. Weil die Schritte strikt zeitlich abgearbeitet werden, kann nur
    Überschuss genutzt werden, der VOR dem Bedarf anfällt. Netzladung gibt es
    im Modell nicht.

    Zwischen AC-Seite und Batterie liegt die Wandlungsstufe aus
    StorageParams. Ist der Speicher leer, deckt das Netz nur den Hausbedarf
    selbst - den Eigenverbrauch des Wechselrichters im Entladebetrieb nicht,
    der entfällt dann. Das ist für die größeren Varianten eher vorsichtig
    gerechnet: sie müssen diesen Eigenverbrauch länger aus dem Speicher
    bezahlen.

    Schritte ohne PV oder Hausverbrauch (Datenlücken) werden übersprungen,
    der Ladestand bleibt dann unverändert."""
    capacity_wh = params.capacity_kwh * 1000
    min_wh = capacity_wh * params.min_soc
    if start_energy_kwh is None:
        energy_wh = min_wh
    else:
        energy_wh = min(capacity_wh, max(min_wh, start_energy_kwh * 1000))
    standby_wh = params.standby_w * SLOT_HOURS
    max_charge_wh = params.max_charge_w * SLOT_HOURS
    max_discharge_wh = params.max_discharge_w * SLOT_HOURS

    result = SimulationResult(days={})
    for day, slots in day_inputs.items():
        totals = DayResult()
        for slot in slots:
            pv, home = slot[I_PV], slot[I_HOME]
            if pv is None or home is None:
                continue
            totals.slots += 1
            net_wh = (pv - home) * SLOT_HOURS
            grid_wh = 0.0
            if net_wh > 0:
                # DC-Leistung, die aus dem Überschuss an der Batterie ankäme.
                dc_wh = max(0.0, params.charge_factor * net_wh + params.charge_offset_w * SLOT_HOURS)
                dc_wh = min(dc_wh, max_charge_wh, (capacity_wh - energy_wh) / params.eta_charge)
                energy_wh += dc_wh * params.eta_charge
                # Davon belegter Überschuss (AC); der Rest wird eingespeist.
                used_wh = 0.0
                if dc_wh > 0:
                    used_wh = min(net_wh, (dc_wh - params.charge_offset_w * SLOT_HOURS) / params.charge_factor)
                totals.charge_kwh += used_wh / 1000
                totals.feed_kwh += (net_wh - used_wh) / 1000
            elif net_wh < 0:
                demand = -net_wh
                needed_dc = params.discharge_factor * demand + params.discharge_offset_w * SLOT_HOURS
                dc_wh = min(needed_dc, max_discharge_wh, max(0.0, energy_wh - min_wh) * params.eta_discharge)
                energy_wh -= dc_wh / params.eta_discharge
                delivered = 0.0
                if dc_wh > 0:
                    delivered = min(
                        demand,
                        max(0.0, (dc_wh - params.discharge_offset_w * SLOT_HOURS) / params.discharge_factor),
                    )
                totals.discharge_kwh += delivered / 1000
                grid_wh += demand - delivered
            # Eigenverbrauch des Batteriesystems: aus dem Speicher, solange er
            # über der Mindestladung ist, sonst aus dem Netz.
            if energy_wh - standby_wh >= min_wh:
                energy_wh -= standby_wh
            else:
                grid_wh += standby_wh
            totals.grid_kwh += grid_wh / 1000
            if record_soc:
                result.soc_trace[slot[I_TS]] = 100 * energy_wh / capacity_wh
        result.days[day] = totals
    result.end_energy_kwh = energy_wh / 1000
    return result


# ---------------------------------------------------------------------------
# Kalibrierung
# ---------------------------------------------------------------------------

@dataclass
class Calibration:
    params: StorageParams
    nominal_kwh: float
    module_kwh: float
    base_modules: int
    inverter_limit_w: float | None
    # Herkunft je Kenngröße: "config" (Admin), "measured" (aus den Daten),
    # "default" (Annahme, weil die Daten nicht reichen).
    sources: dict[str, str]
    regression_windows: int = 0


def _start_soc(day_inputs: dict[str, list[list]]) -> float | None:
    """Gemessener Ladestand im ersten Schritt, den die Simulation tatsächlich
    rechnet (mit PV und Hausverbrauch) - nicht im allerersten Schritt
    überhaupt: davor liegende Tage ohne Verbrauchswerte (z.B. importierte
    Altdaten) würden den Startwert sonst über Tage hinweg verfälschen."""
    for slots in day_inputs.values():
        for slot in slots:
            if slot[I_PV] is not None and slot[I_HOME] is not None and slot[I_SOC] is not None:
                return slot[I_SOC]
    return None


def _measured_min_soc(day_inputs: dict[str, list[list]]) -> float | None:
    """Ladestand, bei dem der Speicher trotz Bedarf nicht mehr entlädt.

    Schritte mit mindestens 300 W Defizit (Verbrauch > PV), in denen die
    Batterie praktisch nichts abgibt, bei niedrigem Ladestand: dort steht der
    Speicher an seiner Entladegrenze. Der Median dieser Ladestände ist die
    eingestellte Mindestladung (bzw. die vom Wechselrichter dynamisch
    gehaltene Reserve)."""
    socs = [
        slot[I_SOC]
        for slots in day_inputs.values()
        for slot in slots
        if slot[I_SOC] is not None and slot[I_PV] is not None and slot[I_HOME] is not None
        and slot[I_BAT] is not None
        and slot[I_HOME] - slot[I_PV] > 300 and abs(slot[I_BAT]) < 50 and slot[I_SOC] < 40
    ]
    if len(socs) < 12:
        return None
    return float(np.median(socs))


def _measured_max_power(day_inputs: dict[str, list[list]]) -> tuple[float | None, float | None]:
    """Höchste Lade- bzw. Entladeleistung in den 5-Minuten-Mitteln. Wurde die
    Grenze im Messzeitraum nie erreicht, unterschätzt das die tatsächliche
    Grenze - für die Simulation ist das unkritisch, solange der Hausverbrauch
    ebenfalls selten darüber liegt."""
    bats = [slot[I_BAT] for slots in day_inputs.values() for slot in slots if slot[I_BAT] is not None]
    if not bats:
        return None, None
    charge = max((-b for b in bats if b < 0), default=0.0)
    discharge = max((b for b in bats if b > 0), default=0.0)
    return (charge or None, discharge or None)


def calibrate_conversion(day_inputs: dict[str, list[list]], min_soc_percent: float, max_charge_w: float, max_discharge_w: float):
    """Wandlungsstufe zwischen AC-Überschuss/-Bedarf und DC-Batterieleistung
    als Gerade je Richtung: Batterie = Faktor · AC + Versatz.

    Verwendet nur Schritte, in denen der Speicher frei arbeiten konnte (nicht
    voll, nicht an der Mindestladung, unter 90 % der Leistungsgrenze, beim
    Laden ohne nennenswerte Einspeisung) - sonst bestimmt die Grenze, nicht
    der Bedarf, die Batterieleistung. Unplausible Ergebnisse (oder zu wenige
    Schritte) liefern None für die betroffene Richtung."""
    charge_points, discharge_points = [], []
    for slots in day_inputs.values():
        for s in slots:
            pv, home, feed, bat, soc = s[I_PV], s[I_HOME], s[I_FEED], s[I_BAT], s[I_SOC]
            if None in (pv, home, bat, soc) or not (min_soc_percent + 5 < soc < 95):
                continue
            net = pv - home
            if net < 0 and 20 < bat < 0.9 * max_discharge_w:
                discharge_points.append((-net, bat))
            elif net > 0 and -0.9 * max_charge_w < bat < -20 and (feed or 0) < 100:
                charge_points.append((net, -bat))

    def fit(points):
        if len(points) < 100:
            return None
        # Eine Gerade braucht unterschiedliche Leistungen - bei (fast)
        # konstantem Bedarf waere die Steigung beliebig.
        if np.ptp([p[0] for p in points]) < 200:
            return None
        arr = np.array(points)
        factor, offset = (float(v) for v in np.polyfit(arr[:, 0], arr[:, 1], 1))
        if not (0.8 <= factor <= 1.25 and -400 <= offset <= 400):
            return None
        return factor, offset

    return fit(charge_points), fit(discharge_points)


def _regression_windows(day_inputs: dict[str, list[list]], min_soc_percent: float):
    """Einstündige Fenster mit gemessenem Ladestand an beiden Enden: Änderung
    des Ladestands, geladene und entnommene Energie, Dauer.

    Fenster mit Ladestand nahe 100 % oder an der Mindestladung werden
    verworfen - dort zeigt der Ladestand nicht mehr linear die Energie an
    (Ausgleichsladen, Reserve)."""
    slots = [s for day in day_inputs.values() for s in day]
    window = 12
    rows = []
    i = 0
    while i + window < len(slots):
        chunk = slots[i : i + window + 1]
        i += window
        if chunk[-1][I_TS] - chunk[0][I_TS] != window * SLOT_SECONDS:
            continue
        if any(s[I_SOC] is None or s[I_BAT] is None for s in chunk):
            continue
        if any(s[I_SOC] >= 99 or s[I_SOC] <= min_soc_percent + 2 for s in chunk):
            continue
        charge = discharge = 0.0
        for a, b in zip(chunk, chunk[1:]):
            mean_w = (a[I_BAT] + b[I_BAT]) / 2
            if mean_w < 0:
                charge += -mean_w * SLOT_HOURS / 1000
            else:
                discharge += mean_w * SLOT_HOURS / 1000
        if charge + discharge < 0.3:
            continue
        rows.append(((chunk[-1][I_SOC] - chunk[0][I_SOC]) / 100, charge, discharge, window * SLOT_HOURS))
    return rows


def calibrate(day_inputs: dict[str, list[list]], config: dict) -> Calibration:
    """Kenngrößen des vorhandenen Speichers. Werte aus der Konfiguration
    haben Vorrang; sonst werden sie aus den Messdaten bestimmt; reicht das
    nicht, gelten die DEFAULT_*-Annahmen.

    Kapazität und Wirkungsgrade stammen aus einer linearen Regression über
    einstündige Fenster:

        ΔSoC = a · E_laden - b · E_entladen - s · Stunden
        a = η_laden / C,  b = 1 / (η_entladen · C),  s = Standby / C

    Aus a und b allein sind C, η_laden und η_entladen nicht eindeutig
    bestimmbar (drei Unbekannte, zwei Gleichungen). Ist die nutzbare
    Kapazität eingetragen, folgen beide Wirkungsgrade getrennt; sonst wird
    η_laden = η_entladen angenommen."""
    sources: dict[str, str] = {}
    nominal = config["nominal_capacity_kwh"]
    module = config["module_capacity_kwh"]
    base_modules = max(1, round(nominal / module))

    min_soc = config.get("min_soc_percent")
    if min_soc is not None:
        sources["min_soc"] = "config"
    else:
        min_soc = _measured_min_soc(day_inputs)
        sources["min_soc"] = "measured" if min_soc is not None else "default"
        if min_soc is None:
            min_soc = DEFAULT_MIN_SOC_PERCENT

    measured_charge, measured_discharge = _measured_max_power(day_inputs)
    max_charge = config.get("max_charge_w") or measured_charge
    max_discharge = config.get("max_discharge_w") or measured_discharge
    sources["max_charge"] = "config" if config.get("max_charge_w") else ("measured" if measured_charge else "default")
    sources["max_discharge"] = "config" if config.get("max_discharge_w") else ("measured" if measured_discharge else "default")
    max_charge = max_charge or DEFAULT_MAX_POWER_W
    max_discharge = max_discharge or DEFAULT_MAX_POWER_W

    capacity = config.get("usable_capacity_kwh")
    eta_c = config.get("eta_charge")
    eta_d = config.get("eta_discharge")
    standby = config.get("standby_w")

    windows = _regression_windows(day_inputs, min_soc)
    fit = None
    if len(windows) >= 24:
        arr = np.array(windows)
        design = np.column_stack([arr[:, 1], -arr[:, 2], -arr[:, 3]])
        coef, *_ = np.linalg.lstsq(design, arr[:, 0], rcond=None)
        a, b, s = (float(c) for c in coef)
        if a > 0 and b > 0:
            fit = (a, b, s)

    measured_c = measured_eta_c = measured_eta_d = measured_standby = None
    if fit is not None:
        a, b, s = fit
        if capacity is not None:
            measured_eta_c, measured_eta_d = a * capacity, 1 / (b * capacity)
        elif eta_c is not None:
            measured_c = eta_c / a
            measured_eta_d = 1 / (b * measured_c)
        elif eta_d is not None:
            measured_c = 1 / (b * eta_d)
            measured_eta_c = a * measured_c
        else:
            eta = math.sqrt(a / b)
            measured_eta_c = measured_eta_d = eta
            measured_c = eta / a
        c_for_standby = capacity or measured_c
        measured_standby = s * c_for_standby * 1000 if c_for_standby else None

        # Physikalisch unplausible Ergebnisse (z.B. bei sehr wenigen oder
        # einseitigen Fenstern) nicht übernehmen.
        if measured_c is not None and not (0.5 * nominal <= measured_c <= 1.1 * nominal):
            measured_c = None
        if measured_eta_c is not None and not (0.8 <= measured_eta_c <= 1.0):
            measured_eta_c = None
        if measured_eta_d is not None and not (0.8 <= measured_eta_d <= 1.0):
            measured_eta_d = None
        if measured_standby is not None and not (0 <= measured_standby <= 200):
            measured_standby = None

    def pick(name: str, configured, measured, default):
        if configured is not None:
            sources[name] = "config"
            return configured
        if measured is not None:
            sources[name] = "measured"
            return measured
        sources[name] = "default"
        return default

    charge_fit, discharge_fit = calibrate_conversion(day_inputs, min_soc, max_charge, max_discharge)
    sources["conversion_charge"] = "measured" if charge_fit else "default"
    sources["conversion_discharge"] = "measured" if discharge_fit else "default"
    charge_factor, charge_offset = charge_fit or (1.0, 0.0)
    discharge_factor, discharge_offset = discharge_fit or (1.0, 0.0)

    capacity = pick("capacity", capacity, measured_c, nominal)
    eta_c = pick("eta_charge", eta_c, measured_eta_c, DEFAULT_EFFICIENCY)
    eta_d = pick("eta_discharge", eta_d, measured_eta_d, DEFAULT_EFFICIENCY)
    standby = pick("standby", standby, measured_standby, DEFAULT_STANDBY_W)

    return Calibration(
        params=StorageParams(
            capacity_kwh=capacity,
            min_soc=min_soc / 100,
            max_charge_w=max_charge,
            max_discharge_w=max_discharge,
            eta_charge=eta_c,
            eta_discharge=eta_d,
            standby_w=standby,
            charge_factor=charge_factor,
            charge_offset_w=charge_offset,
            discharge_factor=discharge_factor,
            discharge_offset_w=discharge_offset,
        ),
        nominal_kwh=nominal,
        module_kwh=module,
        base_modules=base_modules,
        inverter_limit_w=config.get("inverter_battery_limit_w"),
        sources=sources,
        regression_windows=len(windows),
    )


def variant_params(calibration: Calibration, extra_modules: int) -> StorageParams:
    """Speicher mit zusätzlichen Modulen: neue Module mit Nennkapazität, die
    vorhandenen mit der kalibrierten (gealterten) Kapazität. Die Lade- und
    Entladeleistung wächst mit der Modulzahl (höhere Systemspannung bei
    gleichem Strom), höchstens bis zur Grenze des Wechselrichters."""
    base = calibration.params
    if extra_modules == 0:
        return base
    scale = (calibration.base_modules + extra_modules) / calibration.base_modules
    limit = calibration.inverter_limit_w or math.inf
    return replace(
        base,
        capacity_kwh=base.capacity_kwh + extra_modules * calibration.module_kwh,
        max_charge_w=min(base.max_charge_w * scale, max(limit, base.max_charge_w)),
        max_discharge_w=min(base.max_discharge_w * scale, max(limit, base.max_discharge_w)),
    )


def extra_usable_kwh(calibration: Calibration, extra_modules: int) -> float:
    """Nutzbare Zusatzkapazität nach Verlusten: zusätzliche Module oberhalb
    der Mindestladung, nach Entladewirkungsgrad (Bezugsgröße der
    Nutzungshäufigkeit)."""
    p = calibration.params
    return extra_modules * calibration.module_kwh * (1 - p.min_soc) * p.eta_discharge


# ---------------------------------------------------------------------------
# Messwert-Auswertungen (Validierung, Netzladung, Abregelung)
# ---------------------------------------------------------------------------

def grid_charge_kwh_per_day(day_inputs: dict[str, list[list]]) -> dict[str, float]:
    """In den Speicher geladene Energie, die nicht aus PV-Überschuss stammen
    kann (Netzladung, z.B. Notladung bei sehr niedrigem Ladestand oder
    zeitgesteuerte Ladung). Die Simulation lädt nie aus dem Netz - diese
    Menge wird deshalb beim Vergleich mit dem gemessenen Netzbezug
    herausgerechnet."""
    result: dict[str, float] = {}
    for day, slots in day_inputs.items():
        total = 0.0
        for slot in slots:
            bat, pv, home, grid = slot[I_BAT], slot[I_PV], slot[I_HOME], slot[I_GRID]
            if None in (bat, pv, home, grid) or bat >= 0 or grid < GRID_CHARGE_MIN_W:
                continue
            from_grid = min(grid, -bat - max(0.0, pv - home))
            if from_grid > GRID_CHARGE_MIN_W:
                total += from_grid * SLOT_HOURS / 1000
        result[day] = total
    return result


def curtailment_summary(day_inputs: dict[str, list[list]], feed_in_limit_w: float | None) -> dict:
    """Hinweis auf Abregelung: PV, die wegen einer Einspeisebegrenzung gar
    nicht erzeugt wurde, taucht in keinem Messwert auf - ein größerer
    Speicher könnte sie aber aufnehmen. Messbar ist nur, wie oft die
    Einspeisung an der Grenze lag; dann wurde womöglich abgeregelt.

    Ohne eingetragene Grenze wird die höchste gemessene Einspeisung als
    mögliche Grenze geprüft: liegen auffällig viele Schritte dicht darunter,
    spricht das für eine Begrenzung (ein „Plateau“)."""
    feeds = [slot[I_FEED] for slots in day_inputs.values() for slot in slots if slot[I_FEED] is not None]
    feeding = [f for f in feeds if f > 50]
    if not feeding:
        return {"limit_w": feed_in_limit_w, "limit_source": "config" if feed_in_limit_w else None,
                "hours_at_limit": 0.0, "days_at_limit": 0, "max_feed_in_w": None, "suspected": False}
    max_feed = max(feeding)
    limit = feed_in_limit_w or max_feed
    threshold = limit * CURTAILMENT_SHARE
    hits = 0
    days_at_limit = 0
    for slots in day_inputs.values():
        day_hits = sum(1 for s in slots if s[I_FEED] is not None and s[I_FEED] >= threshold)
        hits += day_hits
        days_at_limit += 1 if day_hits else 0
    share = hits / len(feeding)
    return {
        "limit_w": feed_in_limit_w,
        "limit_source": "config" if feed_in_limit_w else None,
        "hours_at_limit": round(hits * SLOT_HOURS, 1),
        "days_at_limit": days_at_limit,
        "max_feed_in_w": round(max_feed),
        # Ohne eingetragene Grenze nur bei deutlichem Plateau melden: mehr
        # als 1 % der Einspeiseschritte UND zusammen mindestens eine Stunde.
        "suspected": bool(feed_in_limit_w and hits) or (
            not feed_in_limit_w and share > 0.01 and hits * SLOT_HOURS >= 1
        ),
    }


def validate(day_inputs: dict[str, list[list]], base: SimulationResult, grid_charge: dict[str, float]) -> dict:
    """Simuliertes 11,04-kWh-Modell gegen die Messung.

    Ladestand: mittlere und quadratisch gemittelte Abweichung (%-Punkte)
    über alle Schritte mit gemessenem Ladestand. Netzbezug: über alle Tage,
    an denen ein gemessener Netzbezug vorliegt, gemessen (abzüglich
    Netzladung) gegen simuliert."""
    diffs = [
        base.soc_trace[slot[I_TS]] - slot[I_SOC]
        for slots in day_inputs.values()
        for slot in slots
        if slot[I_SOC] is not None and slot[I_TS] in base.soc_trace
    ]
    measured_grid = sim_grid = 0.0
    days = 0
    for day, slots in day_inputs.items():
        valid = [s for s in slots if s[I_GRID] is not None and s[I_PV] is not None and s[I_HOME] is not None]
        if not valid or len(valid) < COMPLETE_DAY_SHARE * len(slots):
            continue
        days += 1
        measured_grid += sum(max(0.0, s[I_GRID]) for s in valid) * SLOT_HOURS / 1000 - grid_charge.get(day, 0.0)
        sim_grid += base.days[day].grid_kwh if day in base.days else 0.0
    return {
        "soc_points": len(diffs),
        "soc_mean_error": round(float(np.mean(diffs)), 1) if diffs else None,
        "soc_rmse": round(float(np.sqrt(np.mean(np.square(diffs)))), 1) if diffs else None,
        "grid_days": days,
        "grid_measured_kwh": round(measured_grid, 1) if days else None,
        "grid_simulated_kwh": round(sim_grid, 1) if days else None,
        # Absolut UND relativ: bei sehr kleinem Netzbezug (Sommer) wirkt
        # schon eine Abweichung von wenigen kWh prozentual dramatisch.
        "grid_deviation_kwh": round(sim_grid - measured_grid, 1) if days else None,
        "grid_deviation_kwh_per_day": round((sim_grid - measured_grid) / days, 2) if days else None,
        "grid_deviation_percent": (
            round(100 * (sim_grid - measured_grid) / measured_grid, 1)
            if days and measured_grid > 0 else None
        ),
    }


# ---------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------

def usage_category(ratio: float) -> str:
    if ratio > 1.0:
        return USAGE_MULTI
    if ratio >= USAGE_FULL_FROM:
        return USAGE_FULL
    if ratio >= USAGE_UNUSED_BELOW:
        return USAGE_PARTIAL
    return USAGE_UNUSED


def _empty_counts() -> dict[str, int]:
    return {c: 0 for c in USAGE_CATEGORIES}


@dataclass
class _Bucket:
    days: int = 0
    complete_days: int = 0
    avoided: list[float] = field(default_factory=lambda: [0.0, 0.0])
    feed_reduction: list[float] = field(default_factory=lambda: [0.0, 0.0])
    base_grid: float = 0.0
    usage: list[dict[str, int]] = field(default_factory=lambda: [_empty_counts(), _empty_counts()])

    def to_dict(self) -> dict:
        return {
            "days": self.days,
            "complete_days": self.complete_days,
            "base_grid_kwh": round(self.base_grid, 2),
            "avoided_kwh": [round(v, 2) for v in self.avoided],
            "feed_in_reduction_kwh": [round(v, 2) for v in self.feed_reduction],
            "usage": [dict(u) for u in self.usage],
        }


def evaluate(
    day_inputs: dict[str, list[list]],
    calibration: Calibration,
    timezone_name: str,
) -> dict:
    """Simuliert die drei Varianten und fasst je Tag, Monat und gesamt
    zusammen."""
    # Alle Varianten starten mit derselben gespeicherten Energie wie der
    # gemessene Speicher - die zusätzlichen Module also leer.
    start_soc = _start_soc(day_inputs)
    start_kwh = calibration.params.capacity_kwh * start_soc / 100 if start_soc is not None else None
    base = simulate(day_inputs, calibration.params, start_kwh, record_soc=True)
    variants = [simulate(day_inputs, variant_params(calibration, n), start_kwh) for n in (1, 2)]
    extra = [extra_usable_kwh(calibration, n) for n in (1, 2)]

    total = _Bucket()
    last_year = _Bucket()
    months: dict[str, _Bucket] = {}
    daily = []
    days_sorted = sorted(day_inputs)
    last_day = date.fromisoformat(days_sorted[-1]) if days_sorted else None
    for day in days_sorted:
        base_day = base.days.get(day)
        if base_day is None or base_day.slots == 0:
            continue
        complete = base_day.slots >= COMPLETE_DAY_SHARE * expected_slots(date.fromisoformat(day), timezone_name)
        buckets = [total, months.setdefault(day[:7], _Bucket())]
        if last_day and (last_day - date.fromisoformat(day)).days < 365:
            buckets.append(last_year)
        entry = {"date": day, "complete": complete, "base_grid_kwh": round(base_day.grid_kwh, 3),
                 "avoided_kwh": [], "usage_ratio": [], "usage": []}
        for bucket in buckets:
            bucket.days += 1
            bucket.complete_days += 1 if complete else 0
            bucket.base_grid += base_day.grid_kwh
        for i, sim in enumerate(variants):
            v_day = sim.days[day]
            avoided = base_day.grid_kwh - v_day.grid_kwh
            feed_reduction = base_day.feed_kwh - v_day.feed_kwh
            ratio = max(0.0, v_day.discharge_kwh - base_day.discharge_kwh) / extra[i] if extra[i] > 0 else 0.0
            # Gerundet, damit eine exakt ausgeschöpfte Zusatzkapazität nicht
            # durch Gleitkomma-Rest (1,0000001) als "mehrfach" zählt.
            ratio = round(ratio, 3)
            category = usage_category(ratio)
            entry["avoided_kwh"].append(round(avoided, 3))
            entry["usage_ratio"].append(round(ratio, 3))
            entry["usage"].append(category)
            for bucket in buckets:
                bucket.avoided[i] += avoided
                bucket.feed_reduction[i] += feed_reduction
                bucket.usage[i][category] += 1
        daily.append(entry)

    grid_charge = grid_charge_kwh_per_day(day_inputs)
    return {
        "base": base,
        "total": total.to_dict(),
        "last_365_days": last_year.to_dict(),
        "months": [{"month": m, **months[m].to_dict()} for m in sorted(months)],
        "daily": daily,
        "extra_usable_kwh": [round(e, 2) for e in extra],
        "validation": validate(day_inputs, base, grid_charge),
        "grid_charge_kwh": round(sum(grid_charge.values()), 1),
        "grid_charge_days": sum(1 for v in grid_charge.values() if v >= 0.1),
    }


def _financial(config: dict, last_year: dict, full_year: bool) -> dict | None:
    """Einfache Wirtschaftlichkeit, nur wenn Preise und Kosten eingetragen
    sind: Ersparnis = vermiedener Netzbezug × Strompreis - entgangene
    Einspeisung × Vergütung, Amortisation = Kosten / jährliche Ersparnis.
    Amortisation nur bei einem vollen Jahr Daten (sonst wäre die Ersparnis
    saisonal verzerrt)."""
    price = config.get("price_ct_per_kwh")
    feed_in = config.get("feed_in_ct_per_kwh")
    costs = [config.get("cost_plus_one_eur"), config.get("cost_plus_two_eur")]
    if price is None or feed_in is None or all(c is None for c in costs):
        return None
    variants = []
    for i, cost in enumerate(costs):
        savings = (last_year["avoided_kwh"][i] * price - last_year["feed_in_reduction_kwh"][i] * feed_in) / 100
        payback = cost / savings if full_year and cost is not None and savings > 0 else None
        variants.append({
            "cost_eur": cost,
            "savings_eur": round(savings, 2),
            "payback_years": round(payback, 1) if payback is not None else None,
        })
    return {"price_ct_per_kwh": price, "feed_in_ct_per_kwh": feed_in, "full_year": full_year, "variants": variants}


def _assumptions(calibration: Calibration, curtailment: dict, result: dict, coverage: dict) -> list[str]:
    p = calibration.params
    source_text = {"config": "eingetragen", "measured": "aus Messdaten", "default": "Annahme"}

    def src(key: str) -> str:
        return source_text[calibration.sources[key]]

    notes = [
        f"Nutzbare Kapazität der vorhandenen Module: {p.capacity_kwh:.2f} kWh bei 0–100 % Ladestand "
        f"({src('capacity')}; Nennwert {calibration.nominal_kwh:.2f} kWh). Neue Module mit "
        f"Nennkapazität {calibration.module_kwh:.2f} kWh.",
        f"Mindestladung {p.min_soc * 100:.0f} % ({src('min_soc')}), für alle Varianten gleich.",
        f"Wirkungsgrad Laden {p.eta_charge * 100:.1f} % ({src('eta_charge')}), Entladen "
        f"{p.eta_discharge * 100:.1f} % ({src('eta_discharge')}).",
        f"Standby-Verbrauch des Speichers {p.standby_w:.0f} W ({src('standby')}).",
        f"Lade-/Entladeleistung bei {calibration.nominal_kwh:.2f} kWh: {p.max_charge_w / 1000:.1f} / "
        f"{p.max_discharge_w / 1000:.1f} kW "
        f"({src('max_charge')}); wächst bei mehr Modulen anteilig mit der Systemspannung"
        + (f", begrenzt durch den Wechselrichter auf {calibration.inverter_limit_w / 1000:.1f} kW." if calibration.inverter_limit_w else "."),
        f"Wandlungsverluste und Eigenverbrauch des Wechselrichters: beim Laden kommen "
        f"{p.charge_factor:.3f} × Überschuss {p.charge_offset_w:+.0f} W an der Batterie an "
        f"({src('conversion_charge')}), beim Entladen muss sie {p.discharge_factor:.3f} × Bedarf "
        f"{p.discharge_offset_w:+.0f} W liefern ({src('conversion_discharge')}).",
        "Nachgerechnet wird in 5-Minuten-Schritten; Leistungsspitzen innerhalb eines Schritts sind gemittelt.",
        "Die Nachrechnung lädt nur aus PV-Überschuss. Netzladung und prognosebasiertes oder verzögertes Laden "
        "des echten Wechselrichters sind nicht nachgebildet; gemessene Netzladung "
        f"({result['grid_charge_kwh']:.1f} kWh an {result['grid_charge_days']} "
        f"{'Tag' if result['grid_charge_days'] == 1 else 'Tagen'}) ist beim Vergleich mit "
        "dem gemessenen Netzbezug herausgerechnet.",
        "Alle Varianten werden mit identischer Logik nachgerechnet; verglichen werden nur Nachrechnungen "
        "miteinander, nicht Nachrechnung mit Messung.",
        "Verbrauch und Erzeugung der Vergangenheit werden unverändert angenommen (kein geänderter Verbrauch, "
        "kein anderes Wetterjahr).",
    ]
    if coverage["missing_months"]:
        notes.append(
            "Ohne Messdaten (nicht hochgerechnet): " + ", ".join(coverage["missing_months"]) + "."
        )
    if not coverage["full_year"]:
        notes.append(
            f"Erst {coverage['days']} Tage Daten – die Jahreswerte decken noch nicht alle Jahreszeiten ab "
            "und sind nur für den gezeigten Zeitraum aussagekräftig."
        )
    if curtailment["suspected"]:
        notes.append(
            f"Die Einspeisung lag {curtailment['hours_at_limit']:.1f} h an der Grenze – dort wurde "
            "möglicherweise PV abgeregelt, die ein größerer Speicher hätte aufnehmen können. Diese Menge "
            "ist nicht gemessen und nicht im Ergebnis enthalten."
        )
    else:
        notes.append(
            "Keine Hinweise auf Abregelung durch eine Einspeisebegrenzung"
            + (f" (Grenze {curtailment['limit_w'] / 1000:.1f} kW)." if curtailment["limit_w"] else
               " (keine Grenze eingetragen, kein Plateau in der Einspeisung erkennbar).")
        )
    return notes


def _coverage(day_inputs: dict[str, list[list]], daily: list[dict]) -> dict:
    with_data = [d["date"] for d in daily]
    if not with_data:
        return {"first_day": None, "last_day": None, "days": 0, "complete_days": 0,
                "missing_months": [], "full_year": False}
    first, last = date.fromisoformat(with_data[0]), date.fromisoformat(with_data[-1])
    present = {d[:7] for d in with_data}
    # Kalendermonate des letzten Jahres ohne einen einzigen Tag mit Daten.
    missing = []
    year, month = last.year, last.month
    for _ in range(12):
        key = f"{year:04d}-{month:02d}"
        if key not in present:
            missing.append(key)
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return {
        "first_day": with_data[0],
        "last_day": with_data[-1],
        "days": len(with_data),
        "complete_days": sum(1 for d in daily if d["complete"]),
        "missing_months": sorted(missing),
        "full_year": (last - first).days >= 364,
    }


def _config_key(config: dict, last_closed: date) -> str:
    return hashlib.sha256(
        repr((sorted(config.items()), last_closed.isoformat(), CACHE_VERSION, settings.timezone_name)).encode()
    ).hexdigest()


def build_battery_sizing() -> dict:
    """Komplette Auswertung für /api/readings/battery-sizing. Das Ergebnis
    wird bis zum nächsten Tageswechsel, einer Konfigurationsänderung oder
    einem Import im Speicher gehalten - die Eingangsreihen ändern sich in
    dieser Zeit nicht."""
    tz = ZoneInfo(settings.timezone_name)
    config = get_config()
    last_closed = datetime.now(tz).date() - timedelta(days=1)
    key = _config_key(config, last_closed)
    with _memo_lock:
        if key in _memo:
            return _memo[key]  # type: ignore[return-value]

    earliest = _earliest_reading_date()
    day_inputs = load_day_inputs(earliest, last_closed) if earliest else {}
    day_inputs = {d: s for d, s in day_inputs.items() if s}

    calibration = calibrate(day_inputs, config)
    if day_inputs:
        evaluation = evaluate(day_inputs, calibration, settings.timezone_name)
    else:
        evaluation = {
            "total": _Bucket().to_dict(), "last_365_days": _Bucket().to_dict(), "months": [],
            "daily": [], "extra_usable_kwh": [extra_usable_kwh(calibration, n) for n in (1, 2)],
            "validation": None, "grid_charge_kwh": 0.0, "grid_charge_days": 0,
        }
    evaluation.pop("base", None)
    coverage = _coverage(day_inputs, evaluation["daily"])
    curtailment = curtailment_summary(day_inputs, config.get("feed_in_limit_w"))
    p = calibration.params
    result = {
        "variants": [
            {"key": "base", "modules": calibration.base_modules, "extra_modules": 0,
             "nominal_kwh": round(calibration.nominal_kwh, 2)},
            *[
                {"key": f"plus{n}", "modules": calibration.base_modules + n, "extra_modules": n,
                 "nominal_kwh": round(calibration.nominal_kwh + n * calibration.module_kwh, 2)}
                for n in (1, 2)
            ],
        ],
        "calibration": {
            "capacity_kwh": round(p.capacity_kwh, 2),
            "state_of_health_percent": round(100 * p.capacity_kwh / calibration.nominal_kwh, 1),
            "min_soc_percent": round(p.min_soc * 100, 1),
            "max_charge_w": round(p.max_charge_w),
            "max_discharge_w": round(p.max_discharge_w),
            "eta_charge": round(p.eta_charge, 3),
            "eta_discharge": round(p.eta_discharge, 3),
            "standby_w": round(p.standby_w, 1),
            "sources": calibration.sources,
            "regression_windows": calibration.regression_windows,
        },
        "coverage": coverage,
        "curtailment": curtailment,
        "financial": _financial(config, evaluation["last_365_days"], coverage["full_year"]),
        **evaluation,
    }
    result["assumptions"] = _assumptions(calibration, curtailment, evaluation, coverage)
    with _memo_lock:
        _memo.clear()
        _memo[key] = result
    return result
