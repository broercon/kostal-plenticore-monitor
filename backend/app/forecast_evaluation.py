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


def _accuracy(expected_kwh: float, actual_kwh: float) -> float | None:
    if actual_kwh < 0.05:
        return None
    return max(0.0, 100.0 * (1.0 - abs(actual_kwh - expected_kwh) / actual_kwh))


def _difference_percent(expected_kwh: float, actual_kwh: float) -> float | None:
    if expected_kwh < 0.05:
        return None
    return 100.0 * (actual_kwh - expected_kwh) / expected_kwh


def get_forecast_accuracy(days: int = 30, now: datetime | None = None) -> dict:
    """Vergleicht abgeschlossene Stundenprognosen mit der echten PV-Leistung."""
    from .energy_forecast import load_hourly_pv_history

    now = _utc(now or datetime.now(timezone.utc))
    local_tz = ZoneInfo(settings.timezone_name)
    today = now.astimezone(local_tz).date()
    end = datetime.combine(today, time.min, tzinfo=local_tz).astimezone(timezone.utc)
    start = datetime.combine(
        today - timedelta(days=days), time.min, tzinfo=local_tz
    ).astimezone(timezone.utc)

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
            "message": "Noch keine abgeschlossenen Prognosen zum Vergleichen.",
            "overall_accuracy_percent": None,
            "days": [],
        }

    actual_history = load_hourly_pv_history(start, end)
    device_names = {device.id: device.name for device in settings.inverters}
    by_day_device: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for prediction in stored:
        target = _utc(prediction.target_timestamp)
        actual = actual_history.get(prediction.device_id, {}).get(
            target + timedelta(hours=1)
        )
        if actual is None:
            continue
        date_key = target.astimezone(local_tz).date().isoformat()
        by_day_device[(date_key, prediction.device_id)].append(
            (prediction.expected_w, actual)
        )

    if not by_day_device:
        return {
            "available": False,
            "message": "Prognosen vorhanden, aber noch keine passenden Messwerte.",
            "overall_accuracy_percent": None,
            "days": [],
        }

    by_day: dict[str, list[dict]] = defaultdict(list)
    for (date_key, device_id), samples in by_day_device.items():
        expected_kwh = sum(item[0] for item in samples) / 1000
        actual_kwh = sum(item[1] for item in samples) / 1000
        by_day[date_key].append(
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
                    if (value := _accuracy(expected_kwh, actual_kwh)) is not None
                    else None
                ),
                "matched_hours": len(samples),
            }
        )

    result_days = []
    total_expected = 0.0
    total_actual = 0.0
    for date_key in sorted(by_day, reverse=True):
        devices = sorted(by_day[date_key], key=lambda item: item["device_name"])
        expected_kwh = sum(item["expected_kwh"] for item in devices)
        actual_kwh = sum(item["actual_kwh"] for item in devices)
        total_expected += expected_kwh
        total_actual += actual_kwh
        result_days.append(
            {
                "date": date_key,
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
                    if (value := _accuracy(expected_kwh, actual_kwh)) is not None
                    else None
                ),
                "matched_hours": sum(item["matched_hours"] for item in devices),
                "devices": devices,
            }
        )

    overall = _accuracy(total_expected, total_actual)
    return {
        "available": True,
        "message": "Vergleich der gespeicherten Prognosen mit echten Messwerten.",
        "overall_accuracy_percent": round(overall, 1) if overall is not None else None,
        "days": result_days,
    }
