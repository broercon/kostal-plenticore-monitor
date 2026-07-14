"""Konfiguration der Anwendung: Wechselrichter-Liste, Poll-Intervall, DB-Pfad."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class InverterConfig:
    id: str
    name: str
    host: str
    password: str
    port: int = 80
    # Bei mehreren Wechselrichtern am selben Hausanschluss (z.B. einer mit
    # Batterie + Netzzaehler/KSEM als "Master", ein zweiter ohne eigenen
    # Zaehler, der per AC die Batterie des ersten mitlaedt) meldet i.d.R. nur
    # EIN Geraet den echten Netzbezug/die echte Einspeisung - das andere hat
    # gar keinen Zaehler ("kein Sensor verwendet" in seinem Energiemanagement)
    # und sein eigener Grid_P-Wert ist dann bedeutungslos bzw. falsch. Genau
    # EIN Geraet sollte has_grid_meter=True haben (Standard: True, damit
    # bestehende Ein-Geraet-Installationen unveraendert funktionieren - bei
    # mehreren Geraeten muss das explizit in inverters.json gesetzt werden,
    # siehe README "Mehrere Wechselrichter: Hausverbrauch/Netz korrekt
    # berechnen").
    has_grid_meter: bool = True
    # Vorzeichen-Konvention der Batterieleistung (devices:local:battery/P):
    # bei den meisten Geraeten ist negativ = Laden, positiv = Entladen -
    # falls das bei einem Geraet umgekehrt ist, hier auf True setzen (siehe
    # README).
    battery_power_inverted: bool = False


def _load_inverters_from_file(path: Path) -> list[InverterConfig]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    inverters: list[InverterConfig] = []
    for entry in raw:
        inverters.append(
            InverterConfig(
                id=entry["id"],
                name=entry.get("name", entry["id"]),
                host=entry["host"],
                password=entry["password"],
                port=int(entry.get("port", 80)),
                has_grid_meter=bool(entry.get("has_grid_meter", True)),
                battery_power_inverted=bool(entry.get("battery_power_inverted", False)),
            )
        )
    return inverters


def _load_inverters_from_env() -> list[InverterConfig]:
    """Fallback fuer einen einzelnen Wechselrichter ueber Umgebungsvariablen."""
    host = os.environ.get("INVERTER_HOST")
    password = os.environ.get("INVERTER_PASSWORD")
    if not host or not password:
        return []
    return [
        InverterConfig(
            id=os.environ.get("INVERTER_ID", "wr1"),
            name=os.environ.get("INVERTER_NAME", "Wechselrichter"),
            host=host,
            password=password,
            port=int(os.environ.get("INVERTER_PORT", "80")),
            has_grid_meter=os.environ.get("INVERTER_HAS_GRID_METER", "true").strip().lower()
            not in ("false", "0", "no"),
            battery_power_inverted=os.environ.get(
                "INVERTER_BATTERY_POWER_INVERTED", "false"
            ).strip().lower()
            in ("true", "1", "yes"),
        )
    ]


class Settings:
    def __init__(self) -> None:
        self.config_path = Path(os.environ.get("CONFIG_PATH", "/app/config/inverters.json"))
        self.db_path = Path(os.environ.get("DB_PATH", "/app/data/kostal.db"))
        self.poll_interval_seconds = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
        self.frontend_dir = Path(os.environ.get("FRONTEND_DIR", "/app/frontend"))
        # Fuer die Berechnung von "heute" (Tagessummen) bei lokaler Mitternacht statt UTC.
        self.timezone_name = os.environ.get("TIMEZONE", "Europe/Berlin")

        # Automatischer Abgleich mit dem internen Datenlogger der Wechselrichter
        # beim Start der Anwendung (fuellt z.B. Luecken durch Ausfallzeiten).
        self.auto_import_enabled = os.environ.get(
            "AUTO_IMPORT_HISTORY", "true"
        ).strip().lower() not in ("false", "0", "no")
        # Standard 35 Tage (statt nur 7), damit der "30 Tage"-Button im
        # Dashboard nach einem Neustart auch tatsaechlich Daten fuer den
        # vollen Zeitraum zeigt (begrenzt durch die Speichertiefe des
        # internen Loggers am Wechselrichter selbst, geraeteabhaengig).
        #
        # Mit "unbegrenzt" (oder "0"/"all") wird stattdessen so weit wie
        # moeglich zurueck abgeglichen (siehe auto_import.py) - dann liefert
        # der Wechselrichter beim naechsten Neustart einfach so viel Historie,
        # wie sein interner Logger tatsaechlich noch vorhaelt.
        raw_days = os.environ.get("AUTO_IMPORT_DAYS", "35").strip().lower()
        self.auto_import_days: int | None
        if raw_days in ("unbegrenzt", "unlimited", "all", "0", "-1"):
            self.auto_import_days = None
        else:
            self.auto_import_days = int(raw_days)

        # Manche Installationen liefern Grid_P mit umgekehrtem Vorzeichen
        # (haengt von der Ausrichtung des Stromzaehlers/CT-Clamps ab). Mit
        # GRID_POWER_INVERTED=true die Interpretation von Einspeisung/Netzbezug
        # umdrehen, falls sie im Dashboard vertauscht erscheinen.
        self.grid_power_inverted = os.environ.get(
            "GRID_POWER_INVERTED", "false"
        ).strip().lower() in ("true", "1", "yes")

        # --- Täglicher Zusammenfassungs-Report per Mail ---
        # Verschickt einmal täglich zu einer festen Uhrzeit (lokale
        # TIMEZONE) einen Überblick über den Tag (aktive Wechselrichter,
        # PV-Ertrag je Gerät + Summe) an eine oder mehrere Empfänger-
        # Adressen, über den zentralen Mail-Service (siehe
        # https://github.com/broercon/Mailserver). Bleibt inaktiv, solange
        # nicht mindestens Empfänger UND MAIL_SERVICE_URL gesetzt sind (siehe
        # Warnung unten sowie daily_report.DailyReportScheduler.start()).
        self.daily_report_enabled = os.environ.get(
            "DAILY_REPORT_ENABLED", "true"
        ).strip().lower() not in ("false", "0", "no")

        raw_time = os.environ.get("DAILY_REPORT_TIME", "19:00").strip()
        self.daily_report_time = raw_time
        try:
            hour_str, minute_str = raw_time.split(":", 1)
            hour, minute = int(hour_str), int(minute_str)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            self.daily_report_hour = hour
            self.daily_report_minute = minute
        except (ValueError, AttributeError):
            logger.warning(
                "DAILY_REPORT_TIME=%r ist ungültig (erwartet HH:MM), verwende 19:00.",
                raw_time,
            )
            self.daily_report_time = "19:00"
            self.daily_report_hour = 19
            self.daily_report_minute = 0

        self.daily_report_recipients = [
            addr.strip()
            for addr in os.environ.get("DAILY_REPORT_RECIPIENTS", "").split(",")
            if addr.strip()
        ]
        # Basis-URL der Mailserver-REST-API inkl. Pfad, z.B.
        # "http://mail-api:8080/send" (siehe broercon/Mailserver). Da beide
        # Anwendungen üblicherweise in getrennten docker-compose-Projekten
        # laufen, muss der Host von außerhalb des Mailserver-Containers
        # erreichbar sein - z.B. die LAN-IP/Hostname des Servers plus den
        # veröffentlichten Port 8080, oder ein gemeinsames externes
        # Docker-Netzwerk (siehe README).
        self.mail_service_url = os.environ.get("MAIL_SERVICE_URL", "").strip()
        self.mail_service_api_key = os.environ.get("MAIL_SERVICE_API_KEY", "").strip()
        self.mail_service_from_name = os.environ.get(
            "MAIL_SERVICE_FROM_NAME", "Kostal Plenticore Monitor"
        ).strip()

        if self.daily_report_enabled and (
            not self.daily_report_recipients or not self.mail_service_url
        ):
            logger.warning(
                "DAILY_REPORT_ENABLED=true, aber DAILY_REPORT_RECIPIENTS und/oder "
                "MAIL_SERVICE_URL fehlen - der tägliche Mail-Report bleibt "
                "inaktiv, bis beides gesetzt ist."
            )

        self.inverters: list[InverterConfig] = []
        if self.config_path.is_file():
            try:
                self.inverters = _load_inverters_from_file(self.config_path)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Konnte %s nicht lesen, ignoriere Datei.", self.config_path
                )
        if not self.inverters:
            self.inverters = _load_inverters_from_env()

        if not self.inverters:
            logger.warning(
                "Keine Wechselrichter konfiguriert. Bitte %s anlegen oder "
                "INVERTER_HOST/INVERTER_PASSWORD setzen.",
                self.config_path,
            )

        if len(self.inverters) > 1:
            metered = [inv for inv in self.inverters if inv.has_grid_meter]
            if len(metered) == len(self.inverters):
                logger.warning(
                    "Mehrere Wechselrichter konfiguriert, aber keiner ist explizit "
                    "als einziger Netz-Zaehler markiert (has_grid_meter in %s). "
                    "Hausverbrauch/Netzbezug fuer 'Alle (Summe)' werden dann durch "
                    "einfaches Summieren berechnet, was falsche Werte liefert, "
                    "sobald mehr als ein Geraet am selben Hausanschluss haengt "
                    "(z.B. ein zweiter Wechselrichter, der per AC eine an einem "
                    "anderen Geraet haengende Batterie mitlaedt). Siehe "
                    "README-Abschnitt 'Mehrere Wechselrichter: Hausverbrauch/Netz "
                    "korrekt berechnen'.",
                    self.config_path,
                )
            elif len(metered) != 1:
                logger.warning(
                    "Mehrere Wechselrichter konfiguriert, aber %d davon mit "
                    "has_grid_meter=true markiert (erwartet: genau 1 - das Geraet "
                    "mit dem echten Netzzaehler/KSEM am Netzanschlusspunkt). "
                    "Hausverbrauch/Netzbezug fuer 'Alle (Summe)' koennten dadurch "
                    "falsch berechnet werden.",
                    len(metered),
                )


settings = Settings()
