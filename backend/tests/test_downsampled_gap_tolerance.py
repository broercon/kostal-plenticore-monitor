"""Regressionstest fuer integrate_kwh()'s Luecken-Toleranz bei verdichteten
Altdaten (siehe downsampling.py und aggregation.gap_hours_for_day).

Ohne diese Anpassung wuerde der normale 1h-Abstand zwischen zwei
verdichteten (auf Stundenmittel reduzierten) Messpunkten von integrate_kwh()
als Datenluecke behandelt und uebersprungen (MAX_INTEGRATION_GAP_HOURS =
30 Minuten) - jede Tagessumme aus verdichteten Daten waere dann 0."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.aggregation import (
    DOWNSAMPLED_DAY_MAX_POINTS,
    gap_hours_for_day,
    integrate_kwh,
)
from app.models import Reading


def _hourly_rows(n_hours: int, power_w: float) -> list[Reading]:
    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    return [
        Reading(
            device_id="wr1",
            device_name="WR1",
            timestamp=base + timedelta(hours=i),
            pv_power_w=power_w,
        )
        for i in range(n_hours)
    ]


def test_gap_hours_for_day_switches_at_threshold():
    assert gap_hours_for_day(DOWNSAMPLED_DAY_MAX_POINTS) == 3.0
    assert gap_hours_for_day(DOWNSAMPLED_DAY_MAX_POINTS + 1) is None


def test_integrate_kwh_default_gap_skips_hourly_spacing():
    """Ohne max_gap_hours (normal aufgeloeste Daten erwartet) wird der
    1h-Abstand als Luecke behandelt - Regressionsschutz, damit klar bleibt,
    WARUM verdichtete Aufrufer explizit einen groesseren Wert brauchen."""
    rows = _hourly_rows(24, 1000.0)
    assert integrate_kwh(rows, "pv_power_w") == 0.0


def test_integrate_kwh_with_downsampled_gap_integrates_normally():
    """Mit der fuer verdichtete Tage vorgesehenen Toleranz (siehe
    gap_hours_for_day) wird derselbe 1h-Abstand korrekt integriert: 24
    Stundenwerte a 1000 W ueber 23 Intervalle a 1h = 23 kWh."""
    rows = _hourly_rows(24, 1000.0)
    gap = gap_hours_for_day(len(rows))
    result = integrate_kwh(rows, "pv_power_w", max_gap_hours=gap)
    assert result == 23.0


def test_integrate_kwh_downsampled_gap_still_catches_real_outage():
    """Ein echter, mehrstuendiger Ausfall INNERHALB bereits verdichteter
    Daten (hier: 5h Luecke statt der normalen 1h) muss weiterhin als Luecke
    erkannt und uebersprungen werden, nicht ueberbrueckt."""
    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows = [
        Reading(device_id="wr1", device_name="WR1", timestamp=base, pv_power_w=1000.0),
        Reading(
            device_id="wr1", device_name="WR1",
            timestamp=base + timedelta(hours=1), pv_power_w=1000.0,
        ),
        # 5h Luecke statt der normalen 1h - z.B. Poller/Server laenger down.
        Reading(
            device_id="wr1", device_name="WR1",
            timestamp=base + timedelta(hours=6), pv_power_w=1000.0,
        ),
    ]
    result = integrate_kwh(rows, "pv_power_w", max_gap_hours=3.0)
    # Nur das erste (1h-)Intervall traegt bei, die 5h-Luecke wird uebersprungen.
    assert result == 1.0
