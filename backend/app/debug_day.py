"""Einmaliges Diagnose-Werkzeug: zeigt die in der Datenbank gespeicherten
Rohmesswerte eines einzelnen Kalendertags an - sowohl pro Geraet (rohe
Werte, wie vom jeweiligen Wechselrichter gemeldet) als auch die vom Backend
daraus berechnete "Alle (Summe)"-Energiebilanz (combine_devices()) und die
Tagesverbrauch-Aufteilung nach PV/Speicher/Netz
(daily_home_source_breakdown_kwh()) - also GENAU das, was auch das
Dashboard fuer diesen Tag anzeigen wuerde.

Hintergrund: Anders als debug_live.py (das nur AKTUELLE Live-Werte zeigt)
hilft dieses Skript, einen VERGANGENEN Tag nachzuvollziehen, bei dem eine
Kennzahl im Dashboard (z.B. "0.0 kWh aus PV, obwohl das an einem normalen
Tag kaum sein kann") unplausibel wirkt - es zeigt die zugrunde liegenden
Rohwerte, damit sich unterscheiden laesst, ob es sich um echte Wetterdaten
(z.B. ein durchgehend bewoelkter Tag) oder um eine Datenluecke/einen
Rechenfehler handelt.

Rein lesend, es wird nichts veraendert oder gespeichert.

Nutzung (innerhalb des laufenden Containers):

    docker compose exec kostal-monitor python -m app.debug_day --date 2026-07-11

Optional --device-id, um nur die Rohwerte eines einzelnen Geraets zu sehen
(die kombinierte Energiebilanz wird trotzdem immer fuer ALLE Geraete
berechnet, wie im echten Betrieb).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .aggregation import (
    HISTORY_FIELDS,
    aggregate_per_device,
    combine_devices,
    daily_home_source_breakdown_kwh,
    daily_kwh_totals,
)
from .config import settings
from .database import SessionLocal
from .models import Reading


def _field_stats(rows: list[Reading], field: str) -> str:
    values = [getattr(r, field) for r in rows]
    present = [v for v in values if v is not None]
    missing = len(values) - len(present)
    if not present:
        return f"keine Werte ({missing} von {len(values)} Zeilen fehlen)"
    return (
        f"min={min(present):.1f}  max={max(present):.1f}  "
        f"mittel={sum(present) / len(present):.1f}  "
        f"({missing} von {len(values)} Zeilen fehlen)"
    )


def _run(date_str: str, only_device_id: str | None) -> None:
    tz = ZoneInfo(settings.timezone_name)
    day = datetime.strptime(date_str, "%Y-%m-%d")
    local_start = day.replace(tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(timezone.utc)
    utc_end = local_end.astimezone(timezone.utc)

    print("=" * 70)
    print(f"Tag {date_str} (Zeitzone {settings.timezone_name}) -> UTC-Fenster "
          f"{utc_start.isoformat()} bis {utc_end.isoformat()}")

    session = SessionLocal()
    try:
        rows = list(
            session.scalars(
                select(Reading)
                .where(Reading.timestamp >= utc_start, Reading.timestamp < utc_end)
                .order_by(Reading.timestamp)
            )
        )
    finally:
        session.close()

    if not rows:
        print("Keine gespeicherten Messwerte fuer diesen Tag gefunden.")
        return

    by_device: dict[str, list[Reading]] = {}
    for r in rows:
        by_device.setdefault(r.device_id, []).append(r)

    print("=" * 70)
    print("Rohwerte pro Geraet (so, wie das jeweilige Geraet sie gemeldet hat):")
    for device_id, device_rows in sorted(by_device.items()):
        if only_device_id and device_id != only_device_id:
            continue
        name = device_rows[0].device_name
        print(f"\n  Geraet {device_id} ({name}) - {len(device_rows)} Messwerte:")
        for field in ("pv_power_w", "ac_power_w", "home_power_w", "grid_draw_power_w",
                      "feed_in_power_w", "battery_power_w"):
            print(f"    {field}: {_field_stats(device_rows, field)}")

    if len(settings.inverters) <= 1:
        print("\nNur ein Geraet konfiguriert - keine kombinierte Energiebilanz noetig.")
        return

    has_grid_meter = {cfg.id: cfg.has_grid_meter for cfg in settings.inverters}
    battery_inverted = {cfg.id: cfg.battery_power_inverted for cfg in settings.inverters}

    print("=" * 70)
    print(f"Konfiguration: has_grid_meter={has_grid_meter}, "
          f"battery_power_inverted={battery_inverted}")

    per_device = aggregate_per_device(rows, bucket_seconds=60)
    combined = combine_devices(per_device, has_grid_meter, battery_inverted)
    synthetic_rows = [
        Reading(
            device_id="_combined_",
            device_name="_combined_",
            timestamp=datetime.fromtimestamp(bk, tz=timezone.utc),
            **{f: values.get(f) for f in HISTORY_FIELDS},
        )
        for bk, values in combined.items()
    ]

    print("=" * 70)
    print("Kombinierte Energiebilanz (so, wie sie 'Alle (Summe)' im Dashboard nutzt):")
    for field in ("pv_power_w", "ac_power_w", "home_power_w", "grid_draw_power_w",
                  "feed_in_power_w", "battery_power_w"):
        print(f"    {field}: {_field_stats(synthetic_rows, field)}")

    for field, label in (
        ("home_power_w", "Hausverbrauch"),
        ("pv_power_w", "PV-Ertrag"),
        ("feed_in_power_w", "Einspeisung"),
    ):
        totals = daily_kwh_totals(synthetic_rows, field, settings.timezone_name)
        for entry in totals:
            print(f"    Tagessumme {label}: {entry['kwh']} kWh")

    print("=" * 70)
    print("Tagesverbrauch-Aufteilung (PV/Speicher/Netz, wie im Tagesverbrauch-Diagramm):")
    breakdown = daily_home_source_breakdown_kwh(synthetic_rows, settings.timezone_name)
    for entry in breakdown:
        print(f"    {entry}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Zeigt gespeicherte Rohmesswerte und die daraus berechnete "
            "Energiebilanz fuer einen vergangenen Kalendertag an (Diagnose-Werkzeug, "
            "rein lesend)."
        )
    )
    parser.add_argument("--date", required=True, help="Datum im Format YYYY-MM-DD")
    parser.add_argument(
        "--device-id", help="Optional: nur die Rohwerte dieses einen Geraets anzeigen"
    )
    args = parser.parse_args()
    _run(args.date, args.device_id)


if __name__ == "__main__":
    main()
