"""Tests fuer den Autarkiegrad: taeglicher Wert in
/api/readings/daily-home-breakdown sowie den Jahresvergleich
/api/readings/autarky-yearly-comparison
(app.daily_summary.build_autarky_yearly_comparison) - je Kalendermonat
oder ISO-Kalenderwoche gruppiert nach Jahr, analog zu
build_yearly_comparison() beim PV-Ertrag.

Nutzt die frozen_now-Fixture (siehe conftest.py, FROZEN_NOW = 2026-06-15
12:00 UTC = 14:00 Europe/Berlin), damit "heute"/"dieser Monat"/"letzter
Monat" unabhaengig vom tatsaechlichen Testlaufzeitpunkt eindeutig sind.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import Reading

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    res = client.post("/api/auth/login", json={"username": "tester", "password": "geheim123"})
    assert res.status_code == 200


def _add(rows: list[Reading]) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def _seed_day(device_id: str, day, home_w: float, pv_w: float, grid_draw_w: float) -> None:
    """Zwei Messpunkte (12:00/12:15 Lokalzeit) mit konstanter Leistung, wie
    in test_pv_yield_summary.py - ergibt bei der Integration jeweils
    power_w * 0.25h / 1000 kWh fuer den Anteil, der laut Energiebilanz aus
    PV/Netz/Batterie kommt (siehe aggregation.daily_home_source_breakdown_kwh)."""
    noon_local = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
    rows = [
        Reading(
            device_id=device_id,
            device_name="Wechselrichter",
            timestamp=(noon_local + timedelta(minutes=m)).astimezone(ZoneInfo("UTC")),
            home_power_w=home_w,
            pv_power_w=pv_w,
            grid_draw_power_w=grid_draw_w,
            feed_in_power_w=0.0,
        )
        for m in (0, 15)
    ]
    _add(rows)


def test_daily_home_breakdown_includes_autarky_percent(client, frozen_now):
    """home=1000W, grid_draw=400W, pv=600W -> 600W aus PV, 400W aus Netz,
    Autarkiegrad = 600/1000 = 60%."""
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    _seed_day("wr1", today_local, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)

    res = client.get("/api/readings/daily-home-breakdown?days=1")
    assert res.status_code == 200
    days = res.json()["days"]
    assert len(days) == 1
    assert days[0]["pv_kwh"] == 0.15
    assert days[0]["grid_kwh"] == 0.1
    assert days[0]["autarky_percent"] == 60.0


def test_autarky_is_unknown_without_grid_measurements(client, frozen_now):
    """Importierte Altdaten ohne Netzmessung duerfen nicht als 100 % autark
    erscheinen. Die bestehende Tagesaufteilung bleibt verfuegbar, aber der
    daraus nicht verlaesslich bestimmbare Autarkiegrad ist unbekannt."""
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    noon_local = datetime(
        today_local.year, today_local.month, today_local.day, 12, 0, tzinfo=TZ
    )
    _add(
        [
            Reading(
                device_id="wr1",
                device_name="Wechselrichter",
                timestamp=(noon_local + timedelta(minutes=m)).astimezone(
                    ZoneInfo("UTC")
                ),
                home_power_w=1000.0,
                pv_power_w=600.0,
                grid_draw_power_w=None,
                feed_in_power_w=None,
            )
            for m in (0, 15)
        ]
    )

    daily = client.get("/api/readings/daily-home-breakdown?days=1").json()["days"]
    assert len(daily) == 1
    assert daily[0]["autarky_percent"] is None

    yearly = client.get("/api/readings/autarky-yearly-comparison").json()["years"]
    assert yearly == []


def test_autarky_yearly_comparison_without_data_is_empty(client):
    _login(client)
    res = client.get("/api/readings/autarky-yearly-comparison")
    assert res.status_code == 200
    body = res.json()
    assert body["granularity"] == "month"
    assert body["years"] == []


def test_autarky_yearly_comparison_month_groups_by_calendar_month_and_year(client, frozen_now):
    """Zwei Tage im Juni, ein Jahr auseinander, mit unterschiedlichem
    Autarkiegrad - muessen als zwei separate Jahres-Eintraege auf Position 5
    (Juni = Index 5, 0-basiert) erscheinen, alle anderen 11 Positionen
    bleiben None."""
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()  # 2026-06-15
    last_year_day = today_local.replace(year=today_local.year - 1)  # 2025-06-15

    # Dieses Jahr: 60 % Autarkiegrad (siehe test oben).
    _seed_day("wr1", today_local, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)
    # Letztes Jahr: home=1000W, grid_draw=800W, pv=200W -> 20 % Autarkiegrad.
    _seed_day("wr1", last_year_day, home_w=1000.0, pv_w=200.0, grid_draw_w=800.0)

    res = client.get("/api/readings/autarky-yearly-comparison")
    assert res.status_code == 200
    body = res.json()
    assert body["granularity"] == "month"
    assert [y["year"] for y in body["years"]] == [last_year_day.year, today_local.year]

    last_year_entry, this_year_entry = body["years"]
    assert last_year_entry["values"][5] == 20.0
    assert this_year_entry["values"][5] == 60.0
    # Alle anderen 11 Positionen ohne Daten -> None, nicht 0 oder fehlend.
    assert [v for i, v in enumerate(last_year_entry["values"]) if i != 5] == [None] * 11
    assert len(this_year_entry["values"]) == 12


def test_autarky_yearly_comparison_week_uses_iso_calendar_not_calendar_year(client):
    """Regression: 2024-12-30 gehoert laut Kalenderjahr zu 2024, aber laut
    ISO 8601 zu Kalenderwoche 1 des Jahres 2025 (siehe date.isocalendar()) -
    wie bei build_yearly_comparison()."""
    _login(client)
    boundary_day = date(2024, 12, 30)
    assert boundary_day.isocalendar()[:2] == (2025, 1)
    _seed_day("wr1", boundary_day, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)  # 60 %

    res = client.get("/api/readings/autarky-yearly-comparison?granularity=week")
    assert res.status_code == 200
    body = res.json()
    assert body["granularity"] == "week"
    assert len(body["labels"]) == 53
    assert body["labels"][0] == "KW 1"

    assert [y["year"] for y in body["years"]] == [2025]
    year_entry = body["years"][0]
    assert year_entry["values"][0] == 60.0
    assert all(v is None for v in year_entry["values"][1:])


def test_autarky_yearly_comparison_years_param_limits_to_most_recent(client, frozen_now):
    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    for offset_years in range(3):  # 2026, 2025, 2024
        day = today_local.replace(year=today_local.year - offset_years)
        _seed_day("wr1", day, home_w=1000.0, pv_w=500.0, grid_draw_w=500.0)

    res_all = client.get("/api/readings/autarky-yearly-comparison")
    assert [y["year"] for y in res_all.json()["years"]] == [2024, 2025, 2026]

    res_limited = client.get("/api/readings/autarky-yearly-comparison?years=2")
    assert res_limited.status_code == 200
    assert [y["year"] for y in res_limited.json()["years"]] == [2025, 2026]


def test_autarky_yearly_comparison_years_param_above_five_is_rejected(client):
    _login(client)
    res = client.get("/api/readings/autarky-yearly-comparison?years=6")
    assert res.status_code == 422


def test_autarky_yearly_comparison_invalid_granularity_is_rejected(client):
    _login(client)
    res = client.get("/api/readings/autarky-yearly-comparison?granularity=day")
    assert res.status_code == 422


def test_autarky_yearly_comparison_uses_daily_energy_cache(client, frozen_now):
    """Abgeschlossene Tage (letztes Jahr) muessen nach dem ersten Aufruf im
    daily_energy_cache liegen - wie bei build_pv_yield_summary/
    build_energy_period_summary (siehe test_energy_cache.py)."""
    from app.database import SessionLocal
    from app.models import DailyEnergyCache

    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    last_year_day = today_local.replace(year=today_local.year - 1)
    _seed_day("wr1", last_year_day, home_w=1000.0, pv_w=200.0, grid_draw_w=800.0)
    _seed_day("wr1", today_local, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)

    res = client.get("/api/readings/autarky-yearly-comparison")
    assert res.status_code == 200

    db = SessionLocal()
    try:
        cached_fields = {
            row.field
            for row in db.query(DailyEnergyCache)
            .filter(DailyEnergyCache.date == last_year_day.strftime("%Y-%m-%d"))
            .all()
        }
    finally:
        db.close()
    assert cached_fields == {"home_source_pv", "home_source_battery", "home_source_grid"}

    # Der laufende Tag ("heute") darf NIE im Cache landen (siehe
    # _cached_daily_totals) - er aendert sich noch im Tagesverlauf.
    db = SessionLocal()
    try:
        today_cached = (
            db.query(DailyEnergyCache)
            .filter(DailyEnergyCache.date == today_local.strftime("%Y-%m-%d"))
            .count()
        )
    finally:
        db.close()
    assert today_cached == 0


def test_autarky_yearly_comparison_loads_raw_readings_only_once_per_gap(client, frozen_now, monkeypatch):
    """Regressionstest fuer die Performance-Korrektur: bei einer Cache-
    Luecke duerfen die Rohmesswerte nur EINMAL geladen werden (fuer alle
    drei Anteile PV/Speicher/Netz gemeinsam), nicht dreimal (einmal je
    Anteil) - siehe daily_summary._cached_home_source_breakdown."""
    import app.daily_summary as daily_summary_module

    _login(client)
    today_local = frozen_now.astimezone(TZ).date()
    last_year_day = today_local.replace(year=today_local.year - 1)
    _seed_day("wr1", last_year_day, home_w=1000.0, pv_w=200.0, grid_draw_w=800.0)
    _seed_day("wr1", today_local, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)

    calls = []
    original = daily_summary_module._load_readings_range

    def counting_load_readings_range(start, end_exclusive):
        calls.append((start, end_exclusive))
        return original(start, end_exclusive)

    monkeypatch.setattr(daily_summary_module, "_load_readings_range", counting_load_readings_range)

    res = client.get("/api/readings/autarky-yearly-comparison")
    assert res.status_code == 200

    # Genau EIN Aufruf fuer die (gesamte) historische Luecke plus EIN Aufruf
    # fuer den laufenden Tag ("heute" wird nie gecacht) - nicht sechs (drei
    # Anteile x zwei Bereiche), wie vor der Konsolidierung.
    assert len(calls) == 2
