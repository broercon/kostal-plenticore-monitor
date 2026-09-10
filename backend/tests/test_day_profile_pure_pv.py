"""day_profile gibt fuer die pv-Kurve die REINE PV aus (Batterie an PV3
herausgerechnet), damit der Tagesvergleich nachts keine Batterie als PV zeigt."""
from __future__ import annotations

from datetime import datetime, timezone

from app.aggregation import day_profile
from app.models import Reading


def _r(minute, pv, battery, home=0.0, grid=0.0, feed=0.0):
    return Reading(
        device_id="wr1", device_name="wr1",
        timestamp=datetime(2026, 7, 14, 2, minute, tzinfo=timezone.utc),
        pv_power_w=pv, battery_power_w=battery,
        home_power_w=home, grid_draw_power_w=grid, feed_in_power_w=feed,
    )


def test_day_profile_pv_is_pure_pv():
    # Nachts: pv_power_w = 4000 spiegelt nur die Batterie (4000) ueber PV3.
    rows = [_r(0, 4000.0, 4000.0), _r(5, 4000.0, 4000.0)]
    days = day_profile(rows, bucket_minutes=15, timezone_name="Europe/Berlin")
    assert len(days) == 1
    point = days[0]["points"][0]
    assert point["pv_power_w"] == 0.0  # reine PV, Batterie herausgerechnet


def test_day_profile_pv_without_battery_unchanged():
    rows = [_r(0, 3000.0, None), _r(5, 3000.0, None)]
    days = day_profile(rows, bucket_minutes=15, timezone_name="Europe/Berlin")
    assert days[0]["points"][0]["pv_power_w"] == 3000.0


def test_day_profile_battery_power_negative_when_charging():
    # Batterie an PV3: Panels 3000 W (pv1+pv2), davon 1000 W in die Batterie
    # (Laden) -> pv_power_w (pv1+pv2+pv3) = 2000, battery_power_w = -1000.
    rows = [_r(0, 2000.0, -1000.0), _r(5, 2000.0, -1000.0)]
    days = day_profile(rows, bucket_minutes=15, timezone_name="Europe/Berlin")
    pt = days[0]["points"][0]
    assert pt["battery_power_w"] == -1000.0  # Laden -> negativ (wie Leistungsverlauf)
    assert pt["pv_power_w"] == 3000.0        # reine PV = 2000 - (-1000)


def test_day_profile_battery_share_detected_with_pv3_battery():
    """Regressionstest: haengt die Batterie am PV3-String (raw pv_power_w =
    reine PV + battery_power_w, siehe pure_pv_power_w), MUSS die Solar-/
    Batterie-Aufteilung des Hausverbrauchs die Entladung trotzdem erkennen.

    Vor der Korrektur rechnete battery_net mit dem ROHEN pv_power_w statt der
    reinen PV - das kuerzt sich fuer PV3-Batterie-Anlagen algebraisch immer
    exakt zu 0 (home + feed_in - pv_roh - grid_draw = home + feed_in -
    reine_pv - battery_power_w - grid_draw, und der Rest ist per Definition
    battery_power_w), sodass die Batterie NIE als Quelle des Hausverbrauchs
    erschien - jeglicher aus dem Speicher gedeckte Verbrauch wurde
    faelschlich komplett "aus Solar" gezaehlt.

    Szenario: Panels liefern 1000 W (pv1+pv2), die Batterie entlaedt
    zusaetzlich 2000 W (battery_power_w=2000, positiv=Entladen) -> raw
    pv_power_w = 1000 + 2000 = 3000 (PV3-Quirk). Kein Netzbezug, keine
    Einspeisung, Hausverbrauch = 3000 W. Reine PV allein (1000 W) kann davon
    nur 1000 W decken - die restlichen 2000 W MUESSEN aus der Batterie
    stammen."""
    rows = [
        _r(0, pv=3000.0, battery=2000.0, home=3000.0, grid=0.0, feed=0.0),
        _r(5, pv=3000.0, battery=2000.0, home=3000.0, grid=0.0, feed=0.0),
    ]
    days = day_profile(rows, bucket_minutes=15, timezone_name="Europe/Berlin")
    point = days[0]["points"][0]

    assert point["pv_power_w"] == 1000.0  # reine PV (Anzeige unveraendert)
    assert point["home_from_battery_w"] == 2000.0  # vor dem Fix: 0.0
    assert point["home_from_solar_w"] == 1000.0
