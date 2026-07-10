"""Datenbank-Modelle."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Reading(Base):
    """Ein Messwert-Datensatz von einem Wechselrichter zu einem Zeitpunkt."""

    __tablename__ = "readings"
    __table_args__ = (
        Index("ix_readings_device_timestamp", "device_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Momentanleistungen in Watt
    home_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    grid_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    feed_in_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    grid_draw_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    pv_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_soc_percent: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Tagessummen in kWh (vom Wechselrichter kumuliert, seit Mitternacht)
    yield_day_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_consumption_day_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy_grid_day_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
