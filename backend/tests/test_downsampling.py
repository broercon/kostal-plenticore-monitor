"""Tests fuer app/downsampling.py - Verdichtung alter Rohmesswerte auf
Stundenmittelwerte (siehe dortiger Moduldocstring)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings as app_settings
from app.database import SessionLocal
from app.downsampling import (
    local_hour_start_utc,
    run_downsample_once,
)
from app.models import DownsampleState, Reading

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _add(rows: list[Reading]) -> None:
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def _all_readings() -> list[Reading]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(Reading).order_by(Reading.timestamp)))
    finally:
        db.close()


def _hour_points(local_day: datetime, local_hour: int, device_id="wr1", n=4, pv=1000.0, soc=None):
    """n Messpunkte innerhalb einer lokalen Stunde, gleichmaessig verteilt."""
    hour_start_local = local_day.replace(hour=local_hour, minute=0, second=0, microsecond=0)
    rows = []
    for i in range(n):
        ts_local = hour_start_local + timedelta(minutes=i * (60 // n))
        rows.append(
            Reading(
                device_id=device_id,
                device_name="WR1",
                timestamp=ts_local.astimezone(timezone.utc),
                pv_power_w=pv + i,
                home_power_w=500.0,
                battery_soc_percent=(soc[i] if soc else None),
            )
        )
    return rows


def test_local_hour_start_utc_rounds_down():
    ts = datetime(2026, 6, 15, 14, 37, 22, tzinfo=timezone.utc)  # 16:37 Europe/Berlin (Sommerzeit)
    start = local_hour_start_utc(ts, TZ)
    assert start.astimezone(TZ).hour == 16
    assert start.astimezone(TZ).minute == 0


def test_run_downsample_once_compacts_old_hour(client):
    old_day_local = datetime(2026, 6, 1, tzinfo=TZ)  # weit vor dem Cutoff (60 Tage vor NOW)
    rows = _hour_points(old_day_local, 10, n=4, pv=1000.0)
    _add(rows)

    result = run_downsample_once(now=NOW)
    assert result["up_to_date"] is False
    assert result["rows_deleted"] == 4

    remaining = _all_readings()
    assert len(remaining) == 1
    row = remaining[0]
    assert row.is_downsampled is True
    assert row.device_id == "wr1"
    # Mittelwert von 1000, 1001, 1002, 1003 = 1001.5
    assert row.pv_power_w == 1001.5
    # Reprasentativer Zeitstempel: Stundenmitte (10:30 lokal).
    assert row.timestamp.astimezone(TZ).hour == 10
    assert row.timestamp.astimezone(TZ).minute == 30


def test_run_downsample_once_leaves_recent_data_untouched(client):
    recent_local = datetime(2026, 9, 1, tzinfo=TZ)  # nur 8 Tage vor NOW, < 60 Tage Aufbewahrung
    rows = _hour_points(recent_local, 10, n=4)
    _add(rows)

    result = run_downsample_once(now=NOW)
    assert result["rows_deleted"] == 0

    remaining = _all_readings()
    assert len(remaining) == 4
    assert all(not r.is_downsampled for r in remaining)


def test_run_downsample_once_is_idempotent(client):
    old_day_local = datetime(2026, 6, 1, tzinfo=TZ)
    _add(_hour_points(old_day_local, 10, n=4))

    first = run_downsample_once(now=NOW)
    assert first["rows_deleted"] == 4

    second = run_downsample_once(now=NOW)
    assert second["up_to_date"] is True
    assert second["rows_deleted"] == 0
    assert len(_all_readings()) == 1


def test_run_downsample_once_skips_single_point_hours(client):
    """Eine Stunde mit nur einem Messpunkt (z.B. kurzer Ausfall, oder schon
    verdichtet) wird nicht angefasst."""
    old_day_local = datetime(2026, 6, 1, tzinfo=TZ)
    lonely = Reading(
        device_id="wr1", device_name="WR1",
        timestamp=old_day_local.replace(hour=5, minute=17).astimezone(timezone.utc),
        pv_power_w=42.0,
    )
    _add([lonely])

    run_downsample_once(now=NOW)

    remaining = _all_readings()
    assert len(remaining) == 1
    assert remaining[0].pv_power_w == 42.0
    assert not remaining[0].is_downsampled


def test_run_downsample_once_soc_uses_last_value_not_average(client):
    old_day_local = datetime(2026, 6, 1, tzinfo=TZ)
    rows = _hour_points(old_day_local, 10, n=4, soc=[80.0, 78.0, 76.0, 74.0])
    _add(rows)

    run_downsample_once(now=NOW)

    remaining = _all_readings()
    assert len(remaining) == 1
    assert remaining[0].battery_soc_percent == 74.0  # letzter Wert, nicht Mittel (77.0)


def test_run_downsample_once_energy_matches_within_one_hour_edge_effect(client):
    """End-to-End-Check: fuer konstante Leistung liefert die Integration ueber
    die verdichteten Stundenwerte (mit angepasster Luecken-Toleranz, siehe
    aggregation.gap_hours_for_day) NAHEZU dieselbe Energiemenge wie ueber die
    urspruenglichen Rohmesswerte - mit einer bekannten, bewusst in Kauf
    genommenen Abweichung von bis zu 1 Stunde pro Tag (~4 % bei 24h/Tag):

    Die repraesentativen Zeitstempel eines verdichteten Tages liegen auf den
    STUNDENMITTEN (00:30, 01:30, ..., 23:30 lokal, siehe
    _hour_midpoint_utc) - zusammen genommen ueberspannen sie nur 23h (00:30
    bis 23:30) statt der vollen 24h eines Kalendertags. Die je Kalendertag
    GRUPPIERTE Integration (daily_kwh_totals & Co. - jeder Tag wird EINZELN
    integriert, nicht ueber Tagesgrenzen hinweg) verliert dadurch pro Tag
    das erste/letzte halbe Stundenintervall. Das betrifft ausschliesslich
    eine NEUBERECHNUNG bereits verdichteter Tage (z.B. nach einem
    Logdaten-Reimport, der die zugehoerige Cache-Periode invalidiert) -
    einmal in daily_energy_cache abgelegte Zeitraum-Summen (der Normalfall,
    siehe daily_summary._cached_daily_totals) sind davon nie betroffen, da
    ein Tag laengst vor seiner Verdichtung dauerhaft zwischengespeichert
    wird."""
    from app.aggregation import daily_pv_yield_totals

    old_day_local = datetime(2026, 6, 1, tzinfo=TZ)
    rows = []
    for hour in range(24):
        rows.extend(_hour_points(old_day_local, hour, n=12, pv=1000.0))
    for r in rows:
        # Konstante Leistung - _hour_points() variiert pv sonst um +i.
        r.pv_power_w = 1000.0

    before = daily_pv_yield_totals(rows, "Europe/Berlin")
    assert len(before) == 1
    kwh_before = before[0]["kwh"]
    # 288 Punkte im 5-Minuten-Abstand (00:00 bis 23:55) bei konstant 1000 W:
    # 1000 W * (287 Intervalle * 5 min) = 1000 W * 23,9166h.
    assert kwh_before == round(1000 * (287 * 5 / 60) / 1000, 3)

    _add(rows)
    run_downsample_once(now=NOW)

    after_rows = _all_readings()
    assert all(r.is_downsampled for r in after_rows)
    after = daily_pv_yield_totals(after_rows, "Europe/Berlin")
    # Verdichtet: 24 Stundenmitten-Punkte (00:30..23:30), 23 Intervalle a 1h
    # bei konstant 1000 W = 23,0 kWh - wie oben erlaeutert der erwartete,
    # bewusst in Kauf genommene Randeffekt (~1h/Tag), keine Abweichung durch
    # einen Fehler.
    assert after[0]["kwh"] == 23.0
    assert kwh_before - after[0]["kwh"] < 1.0  # < 1 kWh Differenz bei 1000 W


def test_run_downsample_once_watermark_progresses(client):
    old_day_local = datetime(2026, 6, 1, tzinfo=TZ)
    _add(_hour_points(old_day_local, 10, n=4))

    run_downsample_once(now=NOW)

    db = SessionLocal()
    try:
        state = db.get(DownsampleState, 1)
    finally:
        db.close()
    assert state is not None
    cutoff = (NOW.astimezone(TZ).date() - timedelta(days=app_settings.raw_data_retention_days))
    assert state.downsampled_until_date == cutoff.strftime("%Y-%m-%d")


def test_run_downsample_once_without_any_data_is_noop(client):
    result = run_downsample_once(now=NOW)
    assert result == {"days_processed": 0, "rows_deleted": 0, "up_to_date": True}


def test_run_downsample_once_handles_multiple_devices_independently(client):
    old_day_local = datetime(2026, 6, 1, tzinfo=TZ)
    _add(_hour_points(old_day_local, 10, device_id="wr1", n=4, pv=1000.0))
    _add(_hour_points(old_day_local, 10, device_id="wr2", n=4, pv=2000.0))

    result = run_downsample_once(now=NOW)
    assert result["rows_deleted"] == 8

    remaining = _all_readings()
    assert len(remaining) == 2
    by_device = {r.device_id: r for r in remaining}
    assert by_device["wr1"].pv_power_w == 1001.5
    assert by_device["wr2"].pv_power_w == 2001.5
    # Beide Geraete landen auf demselben repraesentativen Zeitstempel (siehe
    # _hour_midpoint_utc-Docstring) - wichtig fuer das spaetere Kombinieren
    # mehrerer Wechselrichter (aggregate_per_device/combine_devices).
    assert by_device["wr1"].timestamp == by_device["wr2"].timestamp
