"""Tests fuer den Tages-Cache der Hausverbrauchs-Aufschluesselung
(app.daily_summary.build_daily_home_breakdown).

Hintergrund: die Funktion hat frueher bei JEDEM Aufruf saemtliche
Rohmesswerte des angefragten Zeitraums geladen und in Python
durchgerechnet - bei der Voreinstellung von 30 Tagen rund 330.000 Zeilen.
Gemessen waren das mehrere Sekunden, waehrend alle uebrigen
Zeitraum-Uebersichten (die denselben Cache laengst nutzen) bei rund einer
Zehntelsekunde lagen. Seitdem laeuft auch diese Uebersicht ueber
daily_energy_cache.

Die Tests hier sichern beides ab: dass sich an den ANGEZEIGTEN WERTEN
nichts geaendert hat (inklusive der beiden unterschiedlichen Lesarten,
siehe unten), und dass die Rohmesswerte tatsaechlich nur noch fuer die
Luecke und den laufenden Tag gelesen werden.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.database import SessionLocal
from app.models import DailyEnergyCache, Reading

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")


def _login(client) -> None:
    make_user("tester", "geheim123", role="betreiber")
    assert client.post(
        "/api/auth/login", json={"username": "tester", "password": "geheim123"}
    ).status_code == 200


def _seed_day(day, *, home_w: float, pv_w: float, grid_draw_w: float | None) -> None:
    """Zwei Messpunkte (12:00/12:15 Lokalzeit) mit konstanter Leistung -
    ergibt bei der Integration jeweils power_w * 0.25h / 1000 kWh."""
    noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
    db = SessionLocal()
    try:
        db.add_all(
            [
                Reading(
                    device_id="wr1",
                    device_name="Wechselrichter",
                    timestamp=(noon + timedelta(minutes=m)).astimezone(ZoneInfo("UTC")),
                    home_power_w=home_w,
                    pv_power_w=pv_w,
                    grid_draw_power_w=grid_draw_w,
                    feed_in_power_w=0.0 if grid_draw_w is not None else None,
                )
                for m in (0, 15)
            ]
        )
        db.commit()
    finally:
        db.close()


def _breakdown(client, days: int) -> list[dict]:
    res = client.get(f"/api/readings/daily-home-breakdown?days={days}")
    assert res.status_code == 200
    return res.json()["days"]


def test_values_are_identical_on_the_second_call(client, frozen_now):
    """Der erste Aufruf berechnet und cacht, der zweite liest aus dem Cache -
    herauskommen muss beide Male exakt dasselbe."""
    _login(client)
    today = frozen_now.astimezone(TZ).date()
    for offset, (home, pv, grid) in enumerate(
        [(1000.0, 200.0, 800.0), (1000.0, 600.0, 400.0), (800.0, 800.0, 0.0)]
    ):
        _seed_day(today - timedelta(days=2 - offset), home_w=home, pv_w=pv, grid_draw_w=grid)

    kalt = _breakdown(client, 3)
    warm = _breakdown(client, 3)

    assert len(kalt) == 3
    assert kalt == warm


def test_closed_days_are_cached_but_today_is_not(client, frozen_now):
    """Abgeschlossene Tage landen unter eigenen Feldnamen im Cache - der
    laufende Tag nie, er aendert sich noch."""
    _login(client)
    today = frozen_now.astimezone(TZ).date()
    gestern = today - timedelta(days=1)
    _seed_day(gestern, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)
    _seed_day(today, home_w=1000.0, pv_w=200.0, grid_draw_w=800.0)

    _breakdown(client, 2)

    db = SessionLocal()
    try:
        felder_gestern = {
            row.field
            for row in db.query(DailyEnergyCache)
            .filter(DailyEnergyCache.date == gestern.strftime("%Y-%m-%d"))
            .all()
        }
        anzahl_heute = (
            db.query(DailyEnergyCache)
            .filter(DailyEnergyCache.date == today.strftime("%Y-%m-%d"))
            .count()
        )
    finally:
        db.close()

    # Beide Lesarten werden gemeinsam gecacht (siehe _BREAKDOWN_VARIANT und
    # _AUTARKY_VARIANT) - die milde fuer die angezeigten Werte, die strenge
    # fuer den Autarkiegrad.
    assert felder_gestern == {
        "home_breakdown_pv",
        "home_breakdown_battery",
        "home_breakdown_grid",
        "home_source_pv",
        "home_source_battery",
        "home_source_grid",
    }
    assert anzahl_heute == 0


def test_raw_readings_are_not_reread_for_cached_days(client, frozen_now, monkeypatch):
    """Der eigentliche Zweck der Aenderung: beim zweiten Aufruf duerfen die
    Rohmesswerte der abgeschlossenen Tage NICHT noch einmal gelesen werden.

    Gezaehlt werden die Aufrufe von _load_readings_range. Erster Aufruf:
    einmal fuer die gesamte Cache-Luecke, einmal fuer den laufenden Tag.
    Zweiter Aufruf: nur noch der laufende Tag."""
    import app.daily_summary as daily_summary_module

    _login(client)
    today = frozen_now.astimezone(TZ).date()
    for offset in range(3):
        _seed_day(today - timedelta(days=offset), home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)

    echtes_laden = daily_summary_module._load_readings_range
    aufrufe: list[tuple] = []

    def zaehlend(start, end_exclusive, **kwargs):
        aufrufe.append((start, end_exclusive))
        return echtes_laden(start, end_exclusive, **kwargs)

    monkeypatch.setattr(daily_summary_module, "_load_readings_range", zaehlend)

    _breakdown(client, 3)
    assert len(aufrufe) == 2, f"Erster Aufruf: Luecke + heute erwartet, war {aufrufe}"

    aufrufe.clear()
    _breakdown(client, 3)
    assert len(aufrufe) == 1, f"Zweiter Aufruf: nur heute erwartet, war {aufrufe}"
    assert aufrufe[0] == (today, today + timedelta(days=1))


def test_missing_grid_values_keep_the_breakdown_but_drop_the_autarky(client, frozen_now):
    """Die beiden Lesarten duerfen nicht zusammenfallen.

    Fuer die angezeigte Aufteilung zaehlt ein fehlender Netzwert als 0, der
    Messpunkt bleibt also erhalten. Fuer den Autarkiegrad waere dieselbe
    Annahme irrefuehrend (importierte Altdaten ohne Netzmessung gaelten
    sonst als 100 % autark), dort faellt der Punkt heraus - siehe
    _home_source_breakdown_with_grid.

    Wuerde man beide Lesarten auf eine zusammenziehen, fiele genau dieser
    Unterschied still unter den Tisch, und zwar erst fuer Tage mit
    Messluecken - also kaum bemerkbar. Deshalb dieser Test.
    """
    _login(client)
    today = frozen_now.astimezone(TZ).date()
    gestern = today - timedelta(days=1)
    # Abgeschlossener Tag OHNE Netzmessung (laeuft ueber den Cache) ...
    _seed_day(gestern, home_w=1000.0, pv_w=600.0, grid_draw_w=None)
    # ... und ein laufender Tag MIT Netzmessung als Gegenprobe.
    _seed_day(today, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)

    tage = {d["date"]: d for d in _breakdown(client, 2)}

    ohne_netz = tage[gestern.strftime("%Y-%m-%d")]
    assert ohne_netz["pv_kwh"] == 0.15, "Aufteilung muss trotz fehlendem Netzwert da sein"
    assert ohne_netz["autarky_percent"] is None, "Autarkiegrad ist hier nicht bestimmbar"

    mit_netz = tage[today.strftime("%Y-%m-%d")]
    assert mit_netz["autarky_percent"] == 60.0

    # Auch nach dem Cachen muss der Unterschied bestehen bleiben.
    erneut = {d["date"]: d for d in _breakdown(client, 2)}
    assert erneut[gestern.strftime("%Y-%m-%d")]["autarky_percent"] is None
    assert erneut[gestern.strftime("%Y-%m-%d")]["pv_kwh"] == 0.15


def test_days_without_readings_are_left_out(client, frozen_now):
    """Tage MITTEN im angefragten Zeitraum, fuer die es gar keine Messwerte
    gibt, tauchen nicht als leere Eintraege auf - so wie vorher, als solche
    Tage in daily_home_source_breakdown_kwh() gar nicht erst entstanden
    sind.

    Wichtig fuer die Aussagekraft: die Luecke muss ZWISCHEN zwei Tagen mit
    Messwerten liegen. Laege sie nur am Anfang, begrenzte
    build_daily_home_breakdown() den Zeitraum ohnehin auf den ersten
    gespeicherten Messwert, und der Test wuerde nichts pruefen."""
    _login(client)
    today = frozen_now.astimezone(TZ).date()
    vorgestern = today - timedelta(days=2)
    # gestern (today - 1) bleibt bewusst leer
    _seed_day(vorgestern, home_w=1000.0, pv_w=600.0, grid_draw_w=400.0)
    _seed_day(today, home_w=1000.0, pv_w=200.0, grid_draw_w=800.0)

    tage = [d["date"] for d in _breakdown(client, 3)]
    assert tage == [vorgestern.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")]

    # Auch aus dem Cache gelesen darf der leere Tag nicht auftauchen.
    assert [d["date"] for d in _breakdown(client, 3)] == tage
