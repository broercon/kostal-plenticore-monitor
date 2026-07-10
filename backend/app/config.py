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
        )
    ]


class Settings:
    def __init__(self) -> None:
        self.config_path = Path(os.environ.get("CONFIG_PATH", "/app/config/inverters.json"))
        self.db_path = Path(os.environ.get("DB_PATH", "/app/data/kostal.db"))
        self.poll_interval_seconds = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
        self.frontend_dir = Path(os.environ.get("FRONTEND_DIR", "/app/frontend"))

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


settings = Settings()
