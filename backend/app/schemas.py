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


class ImportDeviceResult(BaseModel):
    device_id: str
    device_name: str
    range_begin: str
    range_end: str
    status: str | None = None  # "ok" | "timeout" | "error" | None (Lauf noch nicht beendet)
    message: str | None = None
    inserted: int | None = None
    updated: int | None = None
    skipped: int | None = None


class ImportStatusOut(BaseModel):
    running: bool
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    results: list[ImportDeviceResult] = []


class ImportTriggerOut(BaseModel):
    started: bool
    message: str


class HourlyDeviceInfo(BaseModel):
    device_id: str
    device_name: str


class HourlyBucket(BaseModel):
    bucket: str  # ISO-Zeitstempel (lokale Stundengrenze, ohne Zeitzonen-Suffix)
    values: dict[str, float | None]  # device_id -> kWh


class HourlyPerDeviceOut(BaseModel):
    metric: str
    devices: list[HourlyDeviceInfo]
    buckets: list[HourlyBucket]


class LoginIn(BaseModel):
    username: str
    password: str


class MeOut(BaseModel):
    id: int
    username: str
    role: str
    must_change_password: bool


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordOut(BaseModel):
    success: bool
    message: str


class AdminUserOut(BaseModel):
    id: int
    username: str
    role: str
    must_change_password: bool


class AdminResetPasswordIn(BaseModel):
    # Leer lassen, um automatisch ein zufaelliges neues Passwort zu
    # generieren (wird in der Antwort zurueckgegeben).
    new_password: str | None = None


class AdminResetPasswordOut(BaseModel):
    username: str
    new_password: str
    message: str
