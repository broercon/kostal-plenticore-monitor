"""Hilfsfunktionen, um Rohmesswerte fuer Diagramme in Zeit-Buckets zu mitteln."""
from __future__ import annotations

from datetime import datetime, timezone

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
