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
        # Reine Zeitraum-Abfragen ueber ALLE Geraete (kein device_id-Filter -
        # z.B. die Energie-Zeitraum-Uebersichten bei mehreren Wechselrichtern,
        # siehe daily_summary.py) profitieren vom zusammengesetzten Index oben
        # kaum, da er mit device_id beginnt. Migration fuer Bestandsdaten-
        # banken siehe database._ensure_readings_timestamp_index.
        Index("ix_readings_timestamp", "timestamp"),
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


class DailyReportSettings(Base):
    """Über die Admin-Oberfläche editierbare Konfiguration des täglichen
    Mail-Reports (siehe app/daily_report_config.py) - Ergänzung/Override zu
    den Umgebungsvariablen in config.py. Bewusst eine einzelne Zeile
    (id=1): es gibt nur eine app-weite Konfiguration, keine pro Nutzer."""

    __tablename__ = "daily_report_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    report_time: Mapped[str] = mapped_column(String(5), nullable=False, default="19:00")
    # Kommagetrennte Liste von Empfänger-Adressen, als einfacher String
    # gespeichert (kein eigenes Tabellen-Modell nötig für so eine kleine
    # Liste, siehe daily_report_config._row_to_dict für das Parsing).
    recipients: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    mail_service_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    mail_service_api_key: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    mail_service_from_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ForecastSettings(Base):
    """Anlagenweite, im Admin-Bereich editierbare Prognose-Konfiguration."""

    __tablename__ = "forecast_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ForecastPrediction(Base):
    """Zuletzt bekannte Prognose je Wechselrichter und Zielstunde.

    Solange die Zielstunde noch nicht begonnen hat, darf ein neuer Wetterlauf
    den Eintrag aktualisieren. Danach bleibt er unveraendert und kann
    dauerhaft mit den echten Messwerten verglichen werden.
    """

    __tablename__ = "forecast_predictions"
    __table_args__ = (
        Index(
            "ux_forecast_prediction_device_target",
            "device_id",
            "target_timestamp",
            unique=True,
        ),
        Index("ix_forecast_predictions_target", "target_timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expected_w: Mapped[float] = mapped_column(Float, nullable=False)
    low_w: Mapped[float] = mapped_column(Float, nullable=False)
    high_w: Mapped[float] = mapped_column(Float, nullable=False)
    model_method: Mapped[str] = mapped_column(String(32), nullable=False)
    first_generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyEnergyCache(Base):
    """Cache für abgeschlossene (vergangene) Kalendertage der Energie-
    Zeitraum-Übersichten (PV-Ertrag/Einspeisung je Zeitraum, siehe
    daily_summary._cached_daily_totals). Ein abgeschlossener Tag ändert
    sich nicht mehr - außer ein nachträglicher Logdaten-Import ergänzt
    rückwirkend genau diesen Tag, dann wird der betroffene Eintrag gelöscht
    (siehe daily_summary.invalidate_energy_cache, aufgerufen aus
    auto_import.py).

    Ohne diesen Cache würde jede Anfrage (das Dashboard aktualisiert die
    Zeitraum-Übersicht alle 5 Minuten) sämtliche Rohmesswerte seit Anfang
    des Vorjahres neu integrieren - bei 15s-Poll-Intervall potenziell
    mehrere Millionen Zeilen bei jedem einzelnen Aufruf."""

    __tablename__ = "daily_energy_cache"

    # z.B. "pv_yield" oder "feed_in_power_w" - erlaubt mehrere unabhängige
    # Zeitraum-Übersichten im selben Cache, ohne dass sie sich gegenseitig
    # überschreiben.
    field: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[str] = mapped_column(String(10), primary_key=True)  # "YYYY-MM-DD"
    kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Session(Base):
    """Angemeldete Sitzung (Cookie-Token -> Benutzer), serverseitig
    gespeichert, damit sie sich gezielt invalidieren laesst (Logout,
    Passwort-Aenderung) und Logins Container-Neustarts ueberleben."""

    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
