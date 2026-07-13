"""Datenbank-Modelle."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String
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
    # AC-seitige Nettoleistung am Wechselrichter-Anschluss (devices:local:ac/P)
    # - siehe plenticore_client.py fuer Herleitung/Vorzeichen-Konvention. Neu
    # hinzugekommenes Feld; bei vor diesem Update erfassten Zeilen NULL
    # (siehe database._ensure_ac_power_column fuer die Migration bestehender
    # Datenbanken).
    ac_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_power_w: Mapped[float | None] = mapped_column(Float, nullable=True)
    battery_soc_percent: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Tagessummen in kWh (vom Wechselrichter kumuliert, seit Mitternacht)
    yield_day_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_consumption_day_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy_grid_day_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)


class User(Base):
    """Ein Benutzer der Weboberflaeche (Login/Passwort, Rolle)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # PBKDF2-HMAC-SHA256: Salt und Hash getrennt als Hex-Strings gespeichert
    # (siehe auth.py), keine externe Hashing-Bibliothek noetig.
    password_salt: Mapped[str] = mapped_column(String(32), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # "admin" (volle Rechte inkl. Nutzerverwaltung) oder "betreiber" (normaler
    # Zugriff auf die Daten, kann nur das eigene Passwort aendern).
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="betreiber")
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Session(Base):
    """Angemeldete Sitzung (Cookie-Token -> Benutzer), serverseitig
    gespeichert, damit sie sich gezielt invalidieren laesst (Logout,
    Passwort-Aenderung) und Logins Container-Neustarts ueberleben."""

    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
