"""Pydantic-Schemas fuer die API-Antworten."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DeviceOut(BaseModel):
    id: str
    name: str
    host: str


class ReadingOut(BaseModel):
    device_id: str
    device_name: str
    timestamp: datetime
    home_power_w: float | None = None
    grid_power_w: float | None = None
    feed_in_power_w: float | None = None
    grid_draw_power_w: float | None = None
    pv_power_w: float | None = None
    battery_power_w: float | None = None
    battery_soc_percent: float | None = None
    yield_day_kwh: float | None = None
    home_consumption_day_kwh: float | None = None
    energy_grid_day_kwh: float | None = None

    model_config = {"from_attributes": True}


class HistoryPoint(BaseModel):
    timestamp: datetime
    home_power_w: float | None = None
    feed_in_power_w: float | None = None
    grid_draw_power_w: float | None = None
    pv_power_w: float | None = None
    battery_power_w: float | None = None


class SummaryOut(BaseModel):
    device_id: str
    device_name: str
    yield_day_kwh: float | None = None
    home_consumption_day_kwh: float | None = None
    energy_grid_day_kwh: float | None = None
    as_of: datetime | None = None


class DayProfilePoint(BaseModel):
    minute: int
    pv_power_w: float | None = None
    grid_draw_power_w: float | None = None
    home_from_solar_w: float | None = None
    home_from_battery_w: float | None = None


class DayProfileDay(BaseModel):
    date: str
    points: list[DayProfilePoint]


class DayProfileOut(BaseModel):
    bucket_minutes: int
    days: list[DayProfileDay]


class DailyTotalPoint(BaseModel):
    date: str
    kwh: float | None = None


class DailyTotalsOut(BaseModel):
    metric: str
    days: list[DailyTotalPoint]
