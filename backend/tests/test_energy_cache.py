"""Tests für den Energie-Zeitraum-Cache (app/daily_summary.py:
_cached_daily_totals, invalidate_energy_cache) sowie die zugehörige
DB-Absicherung (WAL-Modus, timestamp-Index, siehe app/database.py).

Hintergrund: /api/readings/pv-yield-summary und /api/readings/feed-in-
summary wurden vom Dashboard alle 5 Minuten abgefragt und luden dabei JEDES
Mal sämtliche Rohmesswerte seit Anfang des Vorjahres neu (mehrere Millionen
Zeilen bei 15s-Poll-Intervall) - dieser Cache sorgt dafür, dass ein bereits
abgeschlossener (vergangener) Kalendertag nur EIN einziges Mal berechnet
wird.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select

from app import daily_summary
from app.daily_summary import _cached_daily_totals, invalidate_energy_cache
from app.database import Base, SessionLocal, engine, init_db
from app.models import DailyEnergyCache, Reading


def test_cached_daily_totals_computes_gap_in_one_call_then_reuses_cache(client):
    calls: list[tuple[date, date]] = []

    def fake_compute(start: date, end_exclusive: date) -> dict[str, float | None]:
        calls.append((start, end_exclusive))
        result = {}
        day = start
        while day < end_exclusive:
            result[day.strftime("%Y-%m-%d")] = 1.5
            day = date.fromordinal(day.toordinal() + 1)
        return result

    earliest = date(2026, 1, 1)
    today = date(2026, 1, 10)

    # Erster Aufruf: die ganze Luecke (1.1. - 9.1., "heute" separat) ist
    # noch nicht gecacht - genau EIN Aufruf fuer die gesamte Luecke plus
    # einer fuer "heute", nicht neun Einzelaufrufe.
    result1 = _cached_daily_totals("test-field", earliest, today, fake_compute)
    assert len(calls) == 2  # ein Aufruf fuer die Luecke, einer fuer "heute"
    assert result1["2026-01-01"] == 1.5
    assert result1["2026-01-09"] == 1.5
    assert result1["2026-01-10"] == 1.5  # "heute"

    # Zweiter Aufruf (z.B. 5 Minuten spaeter): alle abgeschlossenen Tage
    # (1.1.-9.1.) sind jetzt gecacht - nur noch "heute" muss frisch
    # berechnet werden.
    calls.clear()
    result2 = _cached_daily_totals("test-field", earliest, today, fake_compute)
    assert len(calls) == 1
    assert calls[0] == (today, date(2026, 1, 11))
    assert result2 == result1


def test_cached_daily_totals_never_caches_today(client):
    call_count = {"n": 0}

    def fake_compute(start: date, end_exclusive: date) -> dict[str, float | None]:
        call_count["n"] += 1
        return {start.strftime("%Y-%m-%d"): float(call_count["n"])}

    earliest = date(2026, 1, 5)
    today = date(2026, 1, 5)  # Installation "heute" gestartet, keine abgeschlossenen Tage

    result1 = _cached_daily_totals("today-field", earliest, today, fake_compute)
    result2 = _cached_daily_totals("today-field", earliest, today, fake_compute)

    # "Heute" wird bei JEDEM Aufruf neu berechnet (waechst ueber den Tag) -
    # die beiden Werte unterscheiden sich, weil fake_compute hochzaehlt.
    assert result1["2026-01-05"] != result2["2026-01-05"]
    assert call_count["n"] == 2


def test_cached_daily_totals_persists_across_process_via_db(client):
    """Der Cache liegt in der Datenbank (nicht nur im Prozessspeicher) -
    ein zweiter, unabhaengiger Aufruf (z.B. nach einem Neustart) muss ihn
    trotzdem wiederfinden."""
    def fake_compute(start: date, end_exclusive: date) -> dict[str, float | None]:
        return {start.strftime("%Y-%m-%d"): 3.3}

    earliest = date(2026, 2, 1)
    today = date(2026, 2, 2)
    _cached_daily_totals("persist-field", earliest, today, fake_compute)

    session = SessionLocal()
    try:
        row = session.get(DailyEnergyCache, ("persist-field", "2026-02-01"))
        assert row is not None
        assert row.kwh == 3.3
    finally:
        session.close()


def test_invalidate_energy_cache_removes_entries_in_range(client):
    def fake_compute(start: date, end_exclusive: date) -> dict[str, float | None]:
        result = {}
        day = start
        while day < end_exclusive:
            result[day.strftime("%Y-%m-%d")] = 2.0
            day = date.fromordinal(day.toordinal() + 1)
        return result

    earliest = date(2026, 3, 1)
    today = date(2026, 3, 10)
    _cached_daily_totals("invalidate-field", earliest, today, fake_compute)

    session = SessionLocal()
    try:
        count_before = session.scalar(
            select(DailyEnergyCache).where(DailyEnergyCache.field == "invalidate-field")
        )
        assert count_before is not None
    finally:
        session.close()

    invalidate_energy_cache(date(2026, 3, 3), date(2026, 3, 5))

    session = SessionLocal()
    try:
        remaining = list(
            session.scalars(
                select(DailyEnergyCache).where(DailyEnergyCache.field == "invalidate-field")
            )
        )
    finally:
        session.close()
    remaining_dates = {row.date for row in remaining}
    assert "2026-03-03" not in remaining_dates
    assert "2026-03-04" not in remaining_dates
    assert "2026-03-01" in remaining_dates  # ausserhalb des invalidierten Bereichs
    assert "2026-03-09" in remaining_dates


def test_build_pv_yield_summary_second_call_only_queries_today(client, monkeypatch):
    """End-to-End (ueber die oeffentliche Funktion statt _cached_daily_totals
    direkt): der zweite Aufruf darf _load_readings_range nur noch fuer
    'heute' aufrufen, nicht mehr fuer den kompletten Jahres-Zeitraum."""
    calls: list[tuple[date, date]] = []
    original = daily_summary._load_readings_range

    def spy(start, end_exclusive):
        calls.append((start, end_exclusive))
        return original(start, end_exclusive)

    monkeypatch.setattr(daily_summary, "_load_readings_range", spy)

    daily_summary.build_pv_yield_summary()
    first_call_count = len(calls)
    assert first_call_count >= 1

    calls.clear()
    daily_summary.build_pv_yield_summary()
    # Nur noch der schmale "heute"-Aufruf, keine erneute Luecken-Abfrage
    # ueber den gesamten (bis zu ueber ein Jahr zurueckreichenden) Zeitraum.
    assert len(calls) == 1
    span_days = (calls[0][1] - calls[0][0]).days
    assert span_days == 1


# --- DB-Absicherung: WAL-Modus, timestamp-Index ----------------------------


def test_sqlite_uses_wal_journal_mode(client):
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode.lower() == "wal"


def test_readings_timestamp_index_exists(client):
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='readings'"
            )
        }
    assert "ix_readings_timestamp" in names
    assert "ix_readings_device_timestamp" in names


def test_init_db_adds_missing_timestamp_index_on_existing_database(client):
    """Bestandsdatenbank von vor dieser Aenderung: nur der alte
    zusammengesetzte Index existiert, der neue reine timestamp-Index fehlt
    noch. init_db() muss ihn ergaenzen, ohne die Tabelle anzufassen."""
    Base.metadata.drop_all(bind=engine)
    Reading.__table__.create(bind=engine)  # erzeugt nur den in models.py deklarierten Index-Satz

    with engine.connect() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_readings_timestamp")
        names_before = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='readings'"
            )
        }
    assert "ix_readings_timestamp" not in names_before

    init_db()

    with engine.connect() as conn:
        names_after = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='readings'"
            )
        }
    assert "ix_readings_timestamp" in names_after


def test_import_invalidates_cache_only_when_rows_actually_changed(client, monkeypatch):
    import asyncio

    from app import auto_import

    invalidated: list[tuple] = []
    monkeypatch.setattr(
        auto_import, "invalidate_energy_cache", lambda start, end: invalidated.append((start, end))
    )

    class _Cfg:
        id = "wr1"
        name = "WR 1"

    monkeypatch.setattr(auto_import.settings, "inverters", [_Cfg()])

    async def fake_import_no_change(cfg):
        return {
            "device_id": cfg.id,
            "device_name": cfg.name,
            "range_begin": "2026-01-01",
            "range_end": "2026-01-31",
            "status": "ok",
            "message": None,
            "inserted": 0,
            "updated": 0,
            "skipped": 100,
        }

    monkeypatch.setattr(auto_import, "_import_one_device", fake_import_no_change)
    auto_import._state["running"] = True
    asyncio.run(auto_import._run_import_body())
    assert invalidated == []  # nichts geaendert -> Cache bleibt unangetastet

    async def fake_import_with_new_rows(cfg):
        return {
            "device_id": cfg.id,
            "device_name": cfg.name,
            "range_begin": "2026-02-01",
            "range_end": "2026-02-05",
            "status": "ok",
            "message": None,
            "inserted": 42,
            "updated": 3,
            "skipped": 10,
        }

    monkeypatch.setattr(auto_import, "_import_one_device", fake_import_with_new_rows)
    auto_import._state["running"] = True
    asyncio.run(auto_import._run_import_body())
    assert invalidated == [(date(2026, 2, 1), date(2026, 2, 5))]
