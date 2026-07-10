"""Hintergrund-Task, der periodisch alle konfigurierten Wechselrichter abfragt
und die Werte in der Datenbank ablegt."""
from __future__ import annotations

import asyncio
import logging

from .config import settings
from .database import SessionLocal
from .models import Reading
from .plenticore_client import PlenticoreDevice

logger = logging.getLogger(__name__)


class Poller:
    def __init__(self) -> None:
        self.devices = [PlenticoreDevice(cfg) for cfg in settings.inverters]
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # Letzter erfolgreich gelesener Datensatz pro Geraet, fuer /api/readings/latest
        self.latest: dict[str, dict] = {}

    def start(self) -> None:
        if not self.devices:
            logger.warning("Poller nicht gestartet: keine Wechselrichter konfiguriert.")
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
        for device in self.devices:
            await device.close()

    async def _run(self) -> None:
        logger.info(
            "Starte Polling fuer %d Wechselrichter, Intervall %ds",
            len(self.devices),
            settings.poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            await asyncio.gather(*(self._poll_once(d) for d in self.devices))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self, device: PlenticoreDevice) -> None:
        reading = await device.fetch_reading()
        if reading is None:
            return
        self.latest[reading["device_id"]] = reading
        session = SessionLocal()
        try:
            session.add(Reading(**reading))
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("Konnte Messwert nicht speichern (%s)", device.cfg.name)
        finally:
            session.close()


poller = Poller()
