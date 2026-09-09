"""Regressionstest fuer die Wechselwirkung zwischen import_logdata.import_rows()
und bereits verdichteten Altdaten (app/downsampling.py, Reading.is_downsampled).

Ohne die entsprechende Pruefung wuerde ein erneuter Logdaten-Import (z.B.
nach einem manuellen Reimport oder beim automatischen Start-Abgleich, siehe
auto_import.py) die urspruenglichen, laengst durch EINEN Stundenmittelwert
ersetzten Rohmesswerte einer bereits verdichteten Stunde wieder einfuegen -
die Energie dieser Stunde waere dann doppelt gezaehlt (einmal ueber die
verdichtete Zeile, einmal ueber die frisch reimportierten Rohwerte)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database import SessionLocal
from app.import_logdata import ROW_FIELDS, import_rows
from app.models import Reading

TZ = ZoneInfo("Europe/Berlin")


def _log_row(ts_utc: datetime, pv: float) -> dict:
    row = {f: None for f in ROW_FIELDS}
    row["timestamp"] = ts_utc
    row["pv_power_w"] = pv
    return row


def _all_readings() -> list[Reading]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(Reading).order_by(Reading.timestamp)))
    finally:
        db.close()


def test_import_rows_skips_hour_already_downsampled(client):
    hour_start_local = datetime(2026, 6, 1, 10, 0, tzinfo=TZ)
    hour_midpoint_utc = (hour_start_local + timedelta(minutes=30)).astimezone(timezone.utc)

    db = SessionLocal()
    try:
        db.add(
            Reading(
                device_id="wr1",
                device_name="WR1",
                timestamp=hour_midpoint_utc,
                pv_power_w=1001.5,
                is_downsampled=True,
            )
        )
        db.commit()
    finally:
        db.close()

    # Derselbe historische Export wie vor der Verdichtung: 4 Rohmesswerte
    # innerhalb genau dieser Stunde, mit ihren urspruenglichen Zeitstempeln.
    log_rows = [
        _log_row((hour_start_local + timedelta(minutes=15 * i)).astimezone(timezone.utc), 1000.0 + i)
        for i in range(4)
    ]

    inserted, updated, skipped = import_rows("wr1", "WR1", log_rows)

    assert inserted == 0
    assert updated == 0
    assert skipped == 4

    remaining = _all_readings()
    assert len(remaining) == 1  # weiterhin nur die eine verdichtete Zeile
    assert remaining[0].is_downsampled is True
    assert remaining[0].pv_power_w == 1001.5  # unveraendert


def test_import_rows_still_imports_non_downsampled_hours(client):
    """Sanity-Check: die neue Pruefung darf den normalen Import (keine
    verdichteten Daten vorhanden) nicht beeinflussen."""
    base = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    log_rows = [_log_row(base + timedelta(minutes=15 * i), 1000.0 + i) for i in range(4)]

    inserted, updated, skipped = import_rows("wr1", "WR1", log_rows)

    assert inserted == 4
    assert updated == 0
    assert skipped == 0
    assert len(_all_readings()) == 4


def test_import_rows_skips_only_the_downsampled_hour_not_neighbours(client):
    """Eine verdichtete Stunde blockiert den Import NICHT fuer benachbarte,
    noch nicht verdichtete Stunden desselben Imports."""
    hour_start_local = datetime(2026, 6, 1, 10, 0, tzinfo=TZ)
    hour_midpoint_utc = (hour_start_local + timedelta(minutes=30)).astimezone(timezone.utc)

    db = SessionLocal()
    try:
        db.add(
            Reading(
                device_id="wr1", device_name="WR1",
                timestamp=hour_midpoint_utc, pv_power_w=1001.5, is_downsampled=True,
            )
        )
        db.commit()
    finally:
        db.close()

    log_rows = [
        _log_row((hour_start_local + timedelta(minutes=15 * i)).astimezone(timezone.utc), 1000.0)
        for i in range(4)
    ] + [
        # Naechste Stunde - noch nicht verdichtet, muss ganz normal importiert werden.
        _log_row((hour_start_local + timedelta(hours=1, minutes=15 * i)).astimezone(timezone.utc), 500.0)
        for i in range(4)
    ]

    inserted, updated, skipped = import_rows("wr1", "WR1", log_rows)

    assert inserted == 4  # nur die zweite (noch nicht verdichtete) Stunde
    assert skipped == 4  # die erste (verdichtete) Stunde
    assert len(_all_readings()) == 1 + 4  # 1 verdichtet + 4 frisch importiert
