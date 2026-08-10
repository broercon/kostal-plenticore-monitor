"""Pydantic-Schemas fuer die API-Antworten."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


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
    ac_power_w: float | None = None
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
    # Vorzeichenbehaftete Batterieleistung (negativ = Laden, positiv =
    # Entladen) - wie im Leistungsverlauf, fuer den Tagesvergleich.
    battery_power_w: float | None = None


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


class DailyHomeBreakdownDay(BaseModel):
    date: str
    pv_kwh: float | None = None
    battery_kwh: float | None = None
    grid_kwh: float | None = None


class DailyHomeBreakdownOut(BaseModel):
    days: list[DailyHomeBreakdownDay]


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
    new_password: str = Field(min_length=12, max_length=256)


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
    new_password: str | None = Field(default=None, min_length=12, max_length=256)


class AdminResetPasswordOut(BaseModel):
    username: str
    new_password: str
    message: str


class FeedInPeriod(BaseModel):
    key: str  # z.B. "today", "this_week", "last_month"
    from_date: str  # "YYYY-MM-DD" (inklusive)
    to_date: str  # "YYYY-MM-DD" (inklusive)
    kwh: float | None = None  # None = fuer keinen Tag des Zeitraums Daten vorhanden


class FeedInSummaryOut(BaseModel):
    periods: list[FeedInPeriod]


class PvYieldSummaryOut(BaseModel):
    periods: list[FeedInPeriod]


class DailyReportStatusOut(BaseModel):
    enabled: bool
    scheduled_time: str  # "HH:MM"
    recipients: list[str]
    last_sent_at: datetime | None = None
    last_status: str | None = None  # "ok" | "error" | None (noch nie gelaufen)
    last_message: str | None = None


class DailyReportTriggerOut(BaseModel):
    started: bool
    message: str


class DailyReportConfigOut(BaseModel):
    enabled: bool
    report_time: str  # "HH:MM"
    recipients: list[str]
    mail_service_url: str
    # Der API-Key selbst wird nie ans Frontend zurückgegeben (siehe
    # daily_report_config.update_config) - nur, ob überhaupt einer
    # hinterlegt ist.
    mail_service_api_key_set: bool
    mail_service_from_name: str


class DailyReportConfigIn(BaseModel):
    enabled: bool
    report_time: str
    recipients: list[str] = []
    mail_service_url: str = ""
    # None oder leer = vorhandenen API-Key beibehalten (siehe
    # daily_report_config.update_config).
    mail_service_api_key: str | None = None
    mail_service_from_name: str = ""

    @field_validator("report_time")
    @classmethod
    def _valid_time_format(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("Uhrzeit muss im Format HH:MM sein")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError("Uhrzeit muss im Format HH:MM sein") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Uhrzeit muss zwischen 00:00 und 23:59 liegen")
        return v

    @field_validator("recipients")
    @classmethod
    def _clean_recipients(cls, v: list[str]) -> list[str]:
        cleaned = [addr.strip() for addr in v if addr.strip()]
        for addr in cleaned:
            if "@" not in addr:
                raise ValueError(f"Keine gültige E-Mail-Adresse: {addr!r}")
        return cleaned


class PVArrayConfigIn(BaseModel):
    device_id: str
    name: str
    module_count: int | None = None
    module_power_wp: float | None = None
    peak_power_kwp: float | None = None
    tilt_degrees: float = 30.0
    azimuth_degrees: float = 0.0
    inverter_limit_kw: float | None = None
    enabled: bool = True

    @field_validator("device_id", "name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("darf nicht leer sein")
        return value

    @field_validator("module_count")
    @classmethod
    def _valid_module_count(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("Modulanzahl muss groesser als 0 sein")
        return value

    @field_validator("module_power_wp", "peak_power_kwp", "inverter_limit_kw")
    @classmethod
    def _positive_optional(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("Leistung muss groesser als 0 sein")
        return value

    @field_validator("tilt_degrees")
    @classmethod
    def _valid_tilt(cls, value: float) -> float:
        if not 0 <= value <= 90:
            raise ValueError("Neigung muss zwischen 0 und 90 Grad liegen")
        return value

    @field_validator("azimuth_degrees")
    @classmethod
    def _valid_azimuth(cls, value: float) -> float:
        if not -180 <= value <= 180:
            raise ValueError("Azimut muss zwischen -180 und 180 Grad liegen")
        return value


class PVArrayConfigOut(PVArrayConfigIn):
    id: int | None = None
    device_name: str
    effective_peak_power_kwp: float


class ForecastConfigIn(BaseModel):
    enabled: bool = False
    location_name: str = ""
    latitude: float | None = None
    longitude: float | None = None
    forecast_days: int = 7
    system_loss_percent: float = 14.0
    arrays: list[PVArrayConfigIn] = []

    @field_validator("latitude")
    @classmethod
    def _valid_latitude(cls, value: float | None) -> float | None:
        if value is not None and not -90 <= value <= 90:
            raise ValueError("Breitengrad muss zwischen -90 und 90 liegen")
        return value

    @field_validator("longitude")
    @classmethod
    def _valid_longitude(cls, value: float | None) -> float | None:
        if value is not None and not -180 <= value <= 180:
            raise ValueError("Laengengrad muss zwischen -180 und 180 liegen")
        return value

    @field_validator("forecast_days")
    @classmethod
    def _valid_forecast_days(cls, value: int) -> int:
        if not 1 <= value <= 16:
            raise ValueError("Prognosezeitraum muss zwischen 1 und 16 Tagen liegen")
        return value

    @field_validator("system_loss_percent")
    @classmethod
    def _valid_losses(cls, value: float) -> float:
        if not 0 <= value <= 80:
            raise ValueError("Systemverluste muessen zwischen 0 und 80 Prozent liegen")
        return value


class ForecastConfigOut(BaseModel):
    enabled: bool
    location_name: str
    latitude: float | None = None
    longitude: float | None = None
    forecast_days: int
    system_loss_percent: float
    source: str
    arrays: list[PVArrayConfigOut]
