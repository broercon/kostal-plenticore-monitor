"""Speicherung und Auswertung bereits erzeugter PV-Prognosen."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from .config import settings
from .database import SessionLocal
from .models import ForecastPrediction


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def save_forecast_predictions(
    predictions: dict[str, dict[datetime, tuple[float, float, float]]],
    methods: dict[str, str],
    generated_at: datetime,
) -> None:
    """Speichert die letzte Vorhersage je noch nicht begonnener Stunde."""
    generated_at = _utc(generated_at)
    rows = []
    for device_id, device_predictions in predictions.items():
        for target, values in device_predictions.items():
            target = _utc(target)
            if target <= generated_at:
                continue
            rows.append(
                {
                    "device_id": device_id,
                    "target_timestamp": target,
                    "expected_w": values[0],
                    "low_w": values[1],
                    "high_w": values[2],
                    "model_method": methods.get(device_id, "standard"),
                    "first_generated_at": generated_at,
                    "updated_at": generated_at,
                }
            )
    if not rows:
        return

    session = SessionLocal()
    try:
        statement = insert(ForecastPrediction).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["device_id", "target_timestamp"],
            set_={
                "expected_w": statement.excluded.expected_w,
                "low_w": statement.excluded.low_w,
                "high_w": statement.excluded.high_w,
                "model_method": statement.excluded.model_method,
                "updated_at": statement.excluded.updated_at,
            },
        )
        session.execute(statement)
        session.commit()
    finally:
        session.close()


def _accuracy_from_absolute_error(abs_error_kwh: float, actual_kwh: float) -> float | None:
    """Genauigkeit auf Basis der Summe der ABSOLUTEN stuendlichen Fehler,
    nicht der Differenz der (ueber mehrere Stunden aufsummierten)
    Gesamtwerte - siehe get_forecast_accuracy() fuer die Begruendung: ein
    Tag mit vormittags zu hoher und nachmittags zu niedriger Prognose darf
    nicht als treffsicher gelten, nur weil sich die Fehler beim Aufsummieren
    gegenseitig aufheben."""
    if actual_kwh < 0.05:
        return None
    return max(0.0, 100.0 * (1.0 - abs_error_kwh / actual_kwh))


def _difference_percent(expected_kwh: float, actual_kwh: float) -> float | None:
    if expected_kwh < 0.05:
        return None
    return 100.0 * (actual_kwh - expected_kwh) / expected_kwh


def _build_accuracy_days(
    matched: list[tuple[str, datetime, str, float, float]],
    device_names: dict[str, str],
) -> tuple[list[dict], float, float]:
    """Baut aus bereits abgeglichenen (date_key, Stunde, device_id,
    expected_w, actual_w)-Tupeln die Genauigkeits-Eintraege je Tag (mit
    Pro-Geraet- UND kombinierter Aufschluesselung). Wird sowohl fuer die
    abgeschlossenen Vergangenheitstage als auch fuer "heute (bisher)"
    verwendet - dieselbe Aggregationslogik (stuendliche absolute Fehler
    statt Netto-Tagesdifferenz, siehe Kommentare in get_forecast_accuracy)
    gilt fuer beide gleichermassen.

    Rueckgabe: (Tages-Eintraege absteigend nach Datum, Summe der absoluten
    Fehler ueber ALLE uebergebenen Tage, Summe des Ist-Ertrags ueber ALLE
    uebergebenen Tage) - die letzten beiden Werte dienen dem Aufrufer zur
    Berechnung einer uebergreifenden Genauigkeit (z.B.
    overall_accuracy_percent)."""
    # Pro Geraet UND Tag: die Abweichung JEDER einzelnen Stunde zaehlt fuer
    # sich (absolut) in die Genauigkeit ein, nicht die Differenz der
    # Tagessumme - sonst wuerde ein Tag, an dem die Prognose z.B. vormittags
    # zu hoch und nachmittags zu niedrig lag, faelschlich als treffsicher
    # gelten, nur weil sich beide Fehler beim Aufsummieren gegenseitig
    # aufheben (Netto-Differenz nahe 0, obwohl JEDE Stunde daneben lag).
    by_day_device: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for date_key, _hour, device_id, expected_w, actual_w in matched:
        by_day_device[(date_key, device_id)].append((expected_w, actual_w))

    device_day_entries: dict[str, list[dict]] = defaultdict(list)
    for (date_key, device_id), samples in by_day_device.items():
        expected_kwh = sum(item[0] for item in samples) / 1000
        actual_kwh = sum(item[1] for item in samples) / 1000
        abs_error_kwh = sum(abs(item[0] - item[1]) for item in samples) / 1000
        device_day_entries[date_key].append(
            {
                "device_id": device_id,
                "device_name": device_names.get(device_id, device_id),
                "expected_kwh": round(expected_kwh, 2),
                "actual_kwh": round(actual_kwh, 2),
                "difference_kwh": round(actual_kwh - expected_kwh, 2),
                "difference_percent": (
                    round(value, 1)
                    if (value := _difference_percent(expected_kwh, actual_kwh))
                    is not None
                    else None
                ),
                "accuracy_percent": (
                    round(value, 1)
                    if (
                        value := _accuracy_from_absolute_error(
                            abs_error_kwh, actual_kwh
                        )
                    )
                    is not None
                    else None
                ),
                "matched_hours": len(samples),
            }
        )

    # Tageswert UEBER ALLE GERAETE: je Stunde zuerst ueber die Geraete
    # summieren (das darf sich ausgleichen - wenn WR1 in einer Stunde zu
    # viel und WR2 zu wenig prognostiziert hat, kann die Gesamtanlage in
    # dieser Stunde trotzdem treffsicher gewesen sein, das ist eine echte
    # physikalische Kombination am selben Hausanschluss), aber die daraus
    # entstandenen STUENDLICHEN Gesamtabweichungen wieder absolut
    # aufsummieren statt die Tagessumme zu bilden - aus demselben Grund wie
    # oben, nur fuer die kombinierte Anlage statt pro Geraet.
    combined_hourly: dict[tuple[str, datetime], list[float]] = defaultdict(
        lambda: [0.0, 0.0]
    )
    for date_key, hour, _device_id, expected_w, actual_w in matched:
        entry = combined_hourly[(date_key, hour)]
        entry[0] += expected_w
        entry[1] += actual_w

    by_day_expected: dict[str, float] = defaultdict(float)
    by_day_actual: dict[str, float] = defaultdict(float)
    by_day_abs_error: dict[str, float] = defaultdict(float)
    for (date_key, _hour), (expected_w, actual_w) in combined_hourly.items():
        by_day_expected[date_key] += expected_w / 1000
        by_day_actual[date_key] += actual_w / 1000
        by_day_abs_error[date_key] += abs(expected_w - actual_w) / 1000

    result_days = []
    total_abs_error = 0.0
    total_actual = 0.0
    for date_key in sorted(by_day_expected, reverse=True):
        devices = sorted(
            device_day_entries[date_key], key=lambda item: item["device_name"]
        )
        expected_kwh = round(by_day_expected[date_key], 2)
        actual_kwh = round(by_day_actual[date_key], 2)
        abs_error_kwh = by_day_abs_error[date_key]
        total_abs_error += abs_error_kwh
        total_actual += by_day_actual[date_key]
        result_days.append(
            {
                "date": date_key,
                "expected_kwh": expected_kwh,
                "actual_kwh": actual_kwh,
                "difference_kwh": round(actual_kwh - expected_kwh, 2),
                "difference_percent": (
                    round(value, 1)
                    if (value := _difference_percent(expected_kwh, actual_kwh))
                    is not None
                    else None
                ),
                "accuracy_percent": (
                    round(value, 1)
                    if (
                        value := _accuracy_from_absolute_error(
                            abs_error_kwh, actual_kwh
                        )
                    )
                    is not None
                    else None
                ),
                "matched_hours": sum(item["matched_hours"] for item in devices),
                "devices": devices,
            }
        )
    return result_days, total_abs_error, total_actual


def get_forecast_accuracy(days: int = 30, now: datetime | None = None) -> dict:
    """Vergleicht abgeschlossene Stundenprognosen mit der echten PV-Leistung.

    "days" bezieht sich ausschliesslich auf ABGESCHLOSSENE, vergangene Tage
    (result["days"]) - der laufende Tag ist bewusst nicht darunter, weil er
    noch nicht vorbei ist und eine Mischung aus vollstaendigen und
    unvollstaendigen Tagen die Statistik verzerren wuerde. Stattdessen
    liefert result["today_so_far"] zusaetzlich (falls schon Messwerte fuer
    heute vorliegen) denselben Vergleich fuer die bereits vergangenen
    Stunden des laufenden Tages - die Abweichung kann dort durchaus groesser
    ausfallen als bei den vollstaendigen Tagen, weil hier absichtlich JEDE
    einzelne Stunde zaehlt und sich noch keine guten UND schlechten Stunden
    ueber einen ganzen Tag ausgleichen konnten."""
    from .energy_forecast import load_hourly_pv_history

    now = _utc(now or datetime.now(timezone.utc))
    local_tz = ZoneInfo(settings.timezone_name)
    today = now.astimezone(local_tz).date()
    today_key = today.isoformat()
    today_start = datetime.combine(today, time.min, tzinfo=local_tz).astimezone(
        timezone.utc
    )
    start = datetime.combine(
        today - timedelta(days=days), time.min, tzinfo=local_tz
    ).astimezone(timezone.utc)

    session = SessionLocal()
    try:
        stored = session.scalars(
            select(ForecastPrediction)
            .where(
                ForecastPrediction.target_timestamp >= start,
                # Obere Grenze ist "now" statt des bisherigen
                # Tagesbeginns: so fliessen auch schon vergangene Stunden
                # des LAUFENDEN Tages mit ein (fuer today_so_far unten),
                # ohne die Historie doppelt abzufragen.
                ForecastPrediction.target_timestamp < now,
            )
            .order_by(ForecastPrediction.target_timestamp)
        ).all()
    finally:
        session.close()
    if not stored:
        return {
            "available": False,
            "message": "Noch keine abgeschlossenen Prognosen zum Vergleichen.",
            "overall_accuracy_percent": None,
            "days": [],
            "today_so_far": None,
        }

    actual_history = load_hourly_pv_history(start, now)
    device_names = {device.id: device.name for device in settings.inverters}

    # Jede Stunde, fuer die eine gespeicherte Vorhersage UND ein passender
    # Messwert existiert: (date_key, Stunde, device_id, expected_w, actual_w).
    matched: list[tuple[str, datetime, str, float, float]] = []
    for prediction in stored:
        target = _utc(prediction.target_timestamp)
        actual = actual_history.get(prediction.device_id, {}).get(
            target + timedelta(hours=1)
        )
        if actual is None:
            continue
        date_key = target.astimezone(local_tz).date().isoformat()
        matched.append(
            (date_key, target, prediction.device_id, prediction.expected_w, actual)
        )

    if not matched:
        return {
            "available": False,
            "message": "Prognosen vorhanden, aber noch keine passenden Messwerte.",
            "overall_accuracy_percent": None,
            "days": [],
            "today_so_far": None,
        }

    # Heutiger (laufender) Tag getrennt von den abgeschlossenen
    # Vergangenheitstagen halten - siehe Docstring oben.
    matched_past = [item for item in matched if item[0] != today_key]
    matched_today = [item for item in matched if item[0] == today_key]

    result_days, total_abs_error, total_actual = _build_accuracy_days(
        matched_past, device_names
    )
    today_days, _today_abs_error, _today_actual = _build_accuracy_days(
        matched_today, device_names
    )
    today_so_far = today_days[0] if today_days else None

    overall = _accuracy_from_absolute_error(total_abs_error, total_actual)
    return {
        "available": True,
        "message": "Vergleich der gespeicherten Prognosen mit echten Messwerten.",
        "overall_accuracy_percent": round(overall, 1) if overall is not None else None,
        "days": result_days,
        "today_so_far": today_so_far,
    }


def get_yesterday_hourly_comparison(now: datetime | None = None) -> dict:
    """Stuendlicher Prognose-vs-Ist-Vergleich fuer den GESTRIGEN (lokalen)
    Kalendertag - das Gegenstueck zur "Stuendliche Prognose heute"-Ansicht
    im Frontend (siehe app.js refreshForecast()), nur fuer den bereits
    vollstaendig abgeschlossenen Vortag: dort hat inzwischen jede Stunde
    einen Ist-Wert, waehrend bei "heute" die noch laufenden/kuenftigen
    Stunden zwangslaeufig fehlen.

    Anders als /api/forecast (aus der WETTERVORHERSAGE gebaut, reicht nur in
    die Zukunft) stammen die Prognosewerte hier aus den gespeicherten
    ForecastPrediction-Zeilen (siehe save_forecast_predictions) - dieselbe
    Datengrundlage wie get_forecast_accuracy(), hier aber auf Stundenebene
    belassen statt zu einem Tageswert verdichtet, inkl. Aufschluesselung je
    Wechselrichter (fuer die Geraete-Filterung im Frontend)."""
    from .energy_forecast import load_hourly_pv_history

    now = _utc(now or datetime.now(timezone.utc))
    local_tz = ZoneInfo(settings.timezone_name)
    today = now.astimezone(local_tz).date()
    yesterday = today - timedelta(days=1)
    date_key = yesterday.isoformat()
    start = datetime.combine(yesterday, time.min, tzinfo=local_tz).astimezone(timezone.utc)
    end = datetime.combine(today, time.min, tzinfo=local_tz).astimezone(timezone.utc)

    session = SessionLocal()
    try:
        stored = session.scalars(
            select(ForecastPrediction)
            .where(
                ForecastPrediction.target_timestamp >= start,
                ForecastPrediction.target_timestamp < end,
            )
            .order_by(ForecastPrediction.target_timestamp)
        ).all()
    finally:
        session.close()

    if not stored:
        return {
            "available": False,
            "message": "Für gestern liegen keine gespeicherten Prognosen vor.",
            "date": date_key,
            "hours": [],
        }

    actual_history = load_hourly_pv_history(start, end)
    device_names = {device.id: device.name for device in settings.inverters}

    # Je Stunde UND Geraet: Prognose (aus ForecastPrediction) UND Ist-Wert
    # (aus den echten Messwerten) zusammenfuehren. Fehlt der Ist-Wert (noch)
    # - z.B. ein Datenausfall - bleibt actual_kw None statt 0, damit das
    # Frontend "keine Messung" von "Nullertrag" unterscheiden kann.
    by_hour_device: dict[datetime, dict[str, dict]] = defaultdict(dict)
    for prediction in stored:
        target = _utc(prediction.target_timestamp)
        actual_w = actual_history.get(prediction.device_id, {}).get(
            target + timedelta(hours=1)
        )
        by_hour_device[target][prediction.device_id] = {
            "expected_w": prediction.expected_w,
            "low_w": prediction.low_w,
            "high_w": prediction.high_w,
            "actual_w": actual_w,
        }

    # Ueber ALLE lokalen Stunden des Tages iterieren (nicht nur die, fuer
    # die tatsaechlich eine gespeicherte Prognose existiert) - so bleibt die
    # Zeitachse im Diagramm immer der komplette Tag. Fehlt fuer eine Stunde
    # jede Prognose (z.B. wegen eines Neustarts/Ausfalls waehrend dieser
    # Stunde), erscheint dort eine Luecke statt den ganzen Tag zu verkuerzen
    # - iteriert wird in UTC-Einzelstunden zwischen den beiden lokalen
    # Mitternachtsgrenzen, das behandelt auch einen 23- oder 25-Stunden-Tag
    # bei einer Sommer-/Winterzeit-Umstellung automatisch richtig.
    hours = []
    target = start
    while target < end:
        per_device = by_hour_device.get(target, {})
        devices = [
            {
                "device_id": device_id,
                "device_name": device_names.get(device_id, device_id),
                "expected_kw": round(values["expected_w"] / 1000, 3),
                "low_kw": round(values["low_w"] / 1000, 3),
                "high_kw": round(values["high_w"] / 1000, 3),
                "actual_kw": (
                    round(values["actual_w"] / 1000, 3)
                    if values["actual_w"] is not None
                    else None
                ),
            }
            for device_id, values in per_device.items()
        ]
        local_start = target.astimezone(local_tz)
        actual_values = [d["actual_kw"] for d in devices if d["actual_kw"] is not None]
        hours.append(
            {
                "timestamp": target,
                # Gleiches Bucket-Format wie ForecastHourOut.local_hour /
                # aggregation.hourly_kwh_per_device - konsistente Zuordnung
                # ohne erneute (fehleranfaellige) Zeitzonen-Umrechnung im
                # Frontend.
                "local_hour": local_start.replace(
                    minute=0, second=0, microsecond=0
                ).strftime("%Y-%m-%dT%H:%M:%S"),
                # Kombination ueber alle Geraete INNERHALB DERSELBEN STUNDE
                # ist eine simple Summe (physikalisch gueltig, siehe
                # _aggregate_bounds/get_forecast_accuracy) - anders als eine
                # Kombination ueber mehrere STUNDEN hinweg, die hier nicht
                # stattfindet (jede Stunde bleibt fuer sich stehen). Ohne
                # jegliche Geraete-Daten fuer diese Stunde bleiben die
                # Summen None statt faelschlich 0.
                "expected_kw": (
                    round(sum(d["expected_kw"] for d in devices), 3) if devices else None
                ),
                "low_kw": round(sum(d["low_kw"] for d in devices), 3) if devices else None,
                "high_kw": round(sum(d["high_kw"] for d in devices), 3) if devices else None,
                "actual_kw": round(sum(actual_values), 3) if actual_values else None,
                "devices": devices,
            }
        )
        target += timedelta(hours=1)

    return {
        "available": True,
        "message": "Stündlicher Vergleich der gespeicherten Prognosen mit den echten Messwerten für gestern.",
        "date": date_key,
        "hours": hours,
    }
