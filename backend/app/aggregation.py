"""Hilfsfunktionen, um Rohmesswerte fuer Diagramme in Zeit-Buckets zu mitteln."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import Reading

HISTORY_FIELDS = [
    "home_power_w",
    "feed_in_power_w",
    "grid_draw_power_w",
    "pv_power_w",
    "battery_power_w",
]


def _bucket_key(ts: datetime, bucket_seconds: int) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() // bucket_seconds) * bucket_seconds


def aggregate_per_device(
    rows: list[Reading], bucket_seconds: int
) -> dict[str, dict[int, dict[str, float | None]]]:
    """Gruppiert Messwerte pro Geraet in Zeit-Buckets und mittelt sie.

    Rueckgabe: {device_id: {bucket_epoch_sekunden: {feld: mittelwert}}}
    """
    sums: dict[tuple[str, int], dict[str, float]] = {}
    counts: dict[tuple[str, int], dict[str, int]] = {}

    for row in rows:
        bk = _bucket_key(row.timestamp, bucket_seconds)
        key = (row.device_id, bk)
        s = sums.setdefault(key, {f: 0.0 for f in HISTORY_FIELDS})
        c = counts.setdefault(key, {f: 0 for f in HISTORY_FIELDS})
        for field in HISTORY_FIELDS:
            value = getattr(row, field)
            if value is not None:
                s[field] += value
                c[field] += 1

    result: dict[str, dict[int, dict[str, float | None]]] = {}
    for (device_id, bk), s in sums.items():
        c = counts[(device_id, bk)]
        avgs = {f: (s[f] / c[f] if c[f] > 0 else None) for f in HISTORY_FIELDS}
        result.setdefault(device_id, {})[bk] = avgs
    return result


def combine_devices(
    per_device: dict[str, dict[int, dict[str, float | None]]],
) -> dict[int, dict[str, float | None]]:
    """Summiert die pro-Geraet gemittelten Buckets zu einer Gesamtzeitreihe."""
    all_buckets: set[int] = set()
    for buckets in per_device.values():
        all_buckets.update(buckets.keys())

    combined: dict[int, dict[str, float | None]] = {}
    for bk in all_buckets:
        merged: dict[str, float | None] = {}
        for field in HISTORY_FIELDS:
            total = None
            for buckets in per_device.values():
                point = buckets.get(bk)
                if point is None:
                    continue
                value = point.get(field)
                if value is None:
                    continue
                total = (total or 0.0) + value
            merged[field] = total
        combined[bk] = merged
    return combined


def integrate_kwh(rows: list[Reading], field: str) -> float | None:
    """Integriert eine Leistungs-Zeitreihe (Watt) zu einer Energiemenge (kWh),
    per Trapezregel ueber die vorhandenen Messpunkte.

    Wird als Fallback genutzt, wenn der Wechselrichter selbst keinen
    passenden Tages-Statistikwert liefert (z.B. eingeschraenkter Nutzer-Login
    ohne Zugriff auf das Statistik-Modul, oder fehlende Batterie fuer den
    virtuellen Einspeise-Wert).
    """
    points = sorted(
        (
            (row.timestamp, getattr(row, field))
            for row in rows
            if getattr(row, field) is not None
        ),
        key=lambda p: p[0],
    )
    if len(points) < 2:
        return None

    energy_wh = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        dt_hours = (t1 - t0).total_seconds() / 3600
        if dt_hours <= 0:
            continue
        energy_wh += (p0 + p1) / 2 * dt_hours
    return round(energy_wh / 1000, 3)


# Felder, die fuer das Tagesvergleichs-Diagramm gemittelt werden. feed_in_power_w
# wird nur intern fuer die Solar/Batterie-Aufteilung gebraucht (siehe unten) und
# nicht direkt an den Client zurueckgegeben.
DAY_PROFILE_RAW_FIELDS = ["pv_power_w", "home_power_w", "grid_draw_power_w", "feed_in_power_w"]


def day_profile(
    rows: list[Reading], bucket_minutes: int, timezone_name: str
) -> list[dict]:
    """Gruppiert Messwerte nach lokalem Kalendertag und Uhrzeit-Bucket
    (0..1440 Minuten seit lokaler Mitternacht), damit sich mehrere Tage im
    Diagramm ueberlagern und auf einer gemeinsamen 00:00-24:00-Achse
    vergleichen lassen.

    Berechnet zusaetzlich eine Aufteilung des Hausverbrauchs in "aus Solar"
    und "aus Batterie" - rein aus der Leistungsbilanz (PV + Netzbezug +
    Batterie = Hausverbrauch + Einspeisung), OHNE von einer bestimmten
    Vorzeichen-Konvention der Batterieleistung auszugehen (die je nach
    Geraet/Firmware unterschiedlich sein kann). Dafuer werden PV-, Haus- und
    Netzwerte benoetigt; bei importierten Altdaten ohne Netzmessung (KSEM-
    Limitation, siehe import_logdata.py) bleibt die Aufteilung leer - dort
    funktioniert nur die reine PV-Kurve.

    Rueckgabe: Liste von {"date": "YYYY-MM-DD", "points": [...]}, aufsteigend
    nach Datum sortiert (aeltester Tag zuerst).
    """
    tz = ZoneInfo(timezone_name)
    sums: dict[tuple[str, int], dict[str, float]] = {}
    counts: dict[tuple[str, int], dict[str, int]] = {}

    for row in rows:
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        date_str = local.strftime("%Y-%m-%d")
        minute_of_day = local.hour * 60 + local.minute
        bucket = (minute_of_day // bucket_minutes) * bucket_minutes
        key = (date_str, bucket)
        s = sums.setdefault(key, {f: 0.0 for f in DAY_PROFILE_RAW_FIELDS})
        c = counts.setdefault(key, {f: 0 for f in DAY_PROFILE_RAW_FIELDS})
        for field in DAY_PROFILE_RAW_FIELDS:
            value = getattr(row, field)
            if value is not None:
                s[field] += value
                c[field] += 1

    by_date: dict[str, dict[int, dict]] = {}
    for (date_str, bucket), s in sums.items():
        c = counts[(date_str, bucket)]
        avg = {f: (s[f] / c[f] if c[f] > 0 else None) for f in DAY_PROFILE_RAW_FIELDS}

        pv = avg["pv_power_w"]
        home = avg["home_power_w"]
        grid_draw = avg["grid_draw_power_w"]
        feed_in = avg["feed_in_power_w"]

        home_from_solar = None
        home_from_battery = None
        if home is not None and grid_draw is not None and pv is not None and feed_in is not None:
            remaining_home = max(0.0, home - grid_draw)
            # Energiebilanz: positiver Wert = Batterie liefert gerade Leistung
            # (Entladung), negativer Wert = Batterie laedt gerade (nimmt einen
            # Teil der PV-Erzeugung auf).
            battery_net = home + feed_in - pv - grid_draw
            battery_share = min(remaining_home, battery_net) if battery_net > 0 else 0.0
            home_from_battery = round(battery_share, 1)
            home_from_solar = round(remaining_home - battery_share, 1)

        point = {
            "minute": bucket,
            "pv_power_w": round(pv, 1) if pv is not None else None,
            "grid_draw_power_w": round(grid_draw, 1) if grid_draw is not None else None,
            "home_from_solar_w": home_from_solar,
            "home_from_battery_w": home_from_battery,
        }
        by_date.setdefault(date_str, {})[bucket] = point

    days = []
    for date_str in sorted(by_date.keys()):
        buckets = by_date[date_str]
        points = [buckets[bk] for bk in sorted(buckets.keys())]
        days.append({"date": date_str, "points": points})
    return days


def daily_kwh_totals(
    rows: list[Reading], field: str, timezone_name: str
) -> list[dict]:
    """Gruppiert Messwerte nach lokalem Kalendertag und integriert je Tag die
    Energiemenge (kWh) fuer das gegebene Leistungsfeld (Trapezregel, siehe
    integrate_kwh) - fuer Saeulendiagramme wie "Hausverbrauch pro Tag".

    Anders als bei den heutigen Tages-Statistikkarten (get_today_summary)
    wird hier NICHT auf vom Wechselrichter selbst mitgefuehrte Tageswerte
    zurueckgegriffen, sondern immer direkt aus den gespeicherten Messwerten
    integriert - das funktioniert daher auch fuer vergangene Tage und fuer
    per Logdaten-Import nachtraeglich eingespielte Altdaten (home_power_w
    ist dort im Gegensatz zu Netz-/Einspeisewerten verfuegbar).

    Rueckgabe: Liste von {"date": "YYYY-MM-DD", "kwh": float|None},
    aufsteigend nach Datum sortiert.
    """
    tz = ZoneInfo(timezone_name)
    by_date: dict[str, list[Reading]] = {}
    for row in rows:
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        date_str = local.strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(row)

    return [
        {"date": date_str, "kwh": integrate_kwh(day_rows, field)}
        for date_str, day_rows in sorted(by_date.items())
    ]


def hourly_kwh_per_device(
    rows: list[Reading], field: str, timezone_name: str
) -> dict:
    """Gruppiert Messwerte nach Geraet UND lokaler Stunde und integriert je
    Stunde die Energiemenge (kWh) - fuer ein gestapeltes Saeulendiagramm, in
    dem sich z.B. die Einspeisung mehrerer Wechselrichter pro Stunde direkt
    vergleichen laesst (anders als bei den summierten Diagrammen wird hier
    NICHT device-uebergreifend addiert).

    Rueckgabe: {"devices": [{"device_id","device_name"}, ...], "buckets":
    [{"bucket": "YYYY-MM-DDTHH:00:00" (lokale Stundengrenze), "values":
    {device_id: kwh|None}}, ...]}, Buckets aufsteigend sortiert. Jeder
    Bucket enthaelt fuer JEDES bekannte Geraet einen Eintrag (None, wenn
    fuer dieses Geraet in der Stunde keine Messwerte vorliegen), damit das
    Frontend ein sauberes gestapeltes Balkendiagramm ohne Luecken bauen
    kann.
    """
    tz = ZoneInfo(timezone_name)
    groups: dict[tuple[str, str], list[Reading]] = {}
    device_names: dict[str, str] = {}
    all_buckets: set[str] = set()

    for row in rows:
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        bucket_local = local.replace(minute=0, second=0, microsecond=0)
        bucket_key = bucket_local.strftime("%Y-%m-%dT%H:%M:%S")
        all_buckets.add(bucket_key)
        device_names[row.device_id] = row.device_name
        groups.setdefault((row.device_id, bucket_key), []).append(row)

    buckets = []
    for bucket_key in sorted(all_buckets):
        values = {}
        for device_id in device_names:
            group_rows = groups.get((device_id, bucket_key))
            values[device_id] = integrate_kwh(group_rows, field) if group_rows else None
        buckets.append({"bucket": bucket_key, "values": values})

    devices = [
        {"device_id": device_id, "device_name": name}
        for device_id, name in device_names.items()
    ]
    return {"devices": devices, "buckets": buckets}
