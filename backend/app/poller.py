"""Hintergrund-Task, der periodisch alle konfigurierten Wechselrichter abfragt
und die Werte in der Datenbank ablegt."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings
from .database import SessionLocal
from .models import Reading
from .plenticore_client import PlenticoreDevice

logger = logging.getLogger(__name__)


class Poller:
    def __init__(self) -> None:
        self.devices = [PlenticoreDevice(cfg) for cfg in settings.inverters]
        self._task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # Letzter erfolgreich gelesener Datensatz pro Geraet, fuer /api/readings/latest
        self.latest: dict[str, dict] = {}
        # Zeitpunkt (monotone Uhr) des letzten erfolgreichen Abrufs - der
        # Watchdog nutzt ihn, um einen haengenden Poller zu erkennen.
        self._last_success = time.monotonic()
        # Kommt so lange KEIN einziger erfolgreicher Abruf zustande, gilt der
        # Poller als haengend und wird intern neu gestartet (Selbstheilung
        # gegen den beobachteten naechtlichen "Polling stoppt bis Rebuild"-
        # Effekt). Mind. 5 Minuten bzw. 20 Poll-Intervalle.
        self._stall_restart_seconds = max(300, settings.poll_interval_seconds * 20)
        # Nur EINE Benachrichtigungs-Mail je Haenger-Episode (bis wieder ein
        # Abruf gelingt), damit es keine Mail-Flut gibt.
        self._stall_notified = False

    def start(self) -> None:
        if not self.devices:
            logger.warning("Poller nicht gestartet: keine Wechselrichter konfiguriert.")
            return
        self._stop_event.clear()
        self._last_success = time.monotonic()
        self._task = asyncio.create_task(self._run())
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        if self._task is not None:
            await self._task
        for device in self.devices:
            await device.close()

    def _should_restart(self, now: float) -> bool:
        """True, wenn seit `_stall_restart_seconds` kein erfolgreicher Abruf
        mehr gelang - dann gilt der Poller als haengend."""
        return (now - self._last_success) >= self._stall_restart_seconds

    async def _watchdog(self) -> None:
        """Erkennt einen haengenden Poll-Task (kein erfolgreicher Abruf seit
        `_stall_restart_seconds`) und startet das Polling intern neu, statt auf
        einen manuellen Container-Neustart zu warten. Ein blockierender await
        im Poll-Task blockiert den Event-Loop nicht, daher laeuft dieser
        Watchdog auch dann weiter und kann eingreifen."""
        check_interval = max(30, self._stall_restart_seconds // 5)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=check_interval)
                return  # sauberes Herunterfahren
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            if self._should_restart(time.monotonic()):
                logger.error(
                    "Poller haengt (kein erfolgreicher Abruf seit >= %ds) - "
                    "starte Polling intern neu.",
                    self._stall_restart_seconds,
                )
                if not self._stall_notified:
                    self._stall_notified = True
                    await self._notify_stall()
                try:
                    await self._restart_polling()
                except Exception:  # noqa: BLE001
                    logger.exception("Interner Poller-Neustart fehlgeschlagen")

    async def _notify_stall(self) -> None:
        """Schickt EINE Benachrichtigungs-Mail (mit denselben Zugangsdaten wie
        der taegliche Report), wenn ein Polling-Haenger erkannt wurde, und
        verweist auf die persistente Logdatei zum Herauskopieren. Fehler beim
        Versand duerfen den Watchdog NICHT stoppen."""
        try:
            from .daily_report_config import get_config
            from .report_mailer import ReportMailError, send_report_mail

            cfg = get_config()
            when = datetime.now(ZoneInfo(settings.timezone_name)).strftime("%d.%m.%Y %H:%M:%S")
            minutes = self._stall_restart_seconds // 60
            subject = "[Kostal Plenticore Monitor] Polling-Haenger erkannt"
            body = (
                f"Am {when} wurde ein Polling-Haenger erkannt (seit mindestens "
                f"{minutes} Minuten kein erfolgreicher Abruf). Das Polling wurde "
                f"automatisch neu gestartet.\n\n"
                f"Zur Ursachenanalyse liegen die Logs in der Datei:\n"
                f"  data/logs/app.log\n"
                f"(im Projektverzeichnis neben docker-compose.yml; im Container: "
                f"{settings.log_file}).\n\n"
                f"Bitte diese Datei herauskopieren und zur Diagnose weiterleiten."
            )
            await send_report_mail(subject, body, cfg=cfg, html=False)
            logger.info("Stall-Benachrichtigung per Mail verschickt.")
        except ReportMailError as exc:
            logger.warning("Stall-Benachrichtigung nicht versendet: %s", exc)
        except Exception:  # noqa: BLE001
            logger.exception("Fehler beim Versand der Stall-Benachrichtigung")

    async def _restart_polling(self) -> None:
        """Bricht den (evtl. haengenden) Poll-Task ab, schliesst alle Geraete-
        Verbindungen, baut frische Geraete-Objekte auf und startet den
        Poll-Task neu. Wirkt wie ein Neustart des Pollings ohne Container-
        Neustart und ist auch dann unschaedlich, wenn ein Wechselrichter
        nachts schlicht schlaeft (dann wird nur erneut versucht)."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for device in self.devices:
            try:
                await device.close()
            except Exception:  # noqa: BLE001
                pass
        self.devices = [PlenticoreDevice(cfg) for cfg in settings.inverters]
        self._last_success = time.monotonic()  # Gnadenfrist nach Neustart
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        logger.info(
            "Starte Polling fuer %d Wechselrichter, Intervall %ds",
            len(self.devices),
            settings.poll_interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                # return_exceptions=True: ein einzelnes Geraet, das in
                # _poll_once() unerwartet doch eine Exception durchlaesst,
                # darf nicht die anderen Geraete im selben Zyklus abwuergen.
                results = await asyncio.gather(
                    *(self._poll_once(d) for d in self.devices), return_exceptions=True
                )
                for device, result in zip(self.devices, results):
                    if isinstance(result, Exception):
                        logger.exception(
                            "Unerwarteter Fehler beim Abfragen von %s - "
                            "naechster Versuch in %ds",
                            device.cfg.name,
                            settings.poll_interval_seconds,
                            exc_info=result,
                        )
            except asyncio.CancelledError:
                raise  # Sauberes Herunterfahren (poller.stop()) nicht verschlucken.
            except Exception:  # noqa: BLE001
                # Letztes Sicherheitsnetz: sollte dank der Absicherungen in
                # _poll_once()/fetch_reading() eigentlich nie noetig sein,
                # verhindert aber, dass ein unvorhergesehener Fehler das
                # Polling fuer den Rest des Tages/der Nacht komplett
                # lahmlegt (das genaue Symptom, das zu dieser Absicherung
                # gefuehrt hat: Polling stoppte naechtens dauerhaft, bis der
                # Container manuell neu gestartet wurde).
                logger.exception(
                    "Unerwarteter Fehler im Poll-Zyklus - naechster Versuch in %ds",
                    settings.poll_interval_seconds,
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self, device: PlenticoreDevice) -> None:
        try:
            reading = await device.fetch_reading()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # fetch_reading() ist so gebaut, dass es bei Verbindungsproblemen
            # None zurueckgibt statt zu werfen (siehe plenticore_client.py).
            # Dieses Abfangen hier ist ein zusaetzliches Sicherheitsnetz,
            # falls trotzdem einmal etwas durchrutscht - ein einzelnes
            # fehlerhaftes Geraet darf nie den ganzen Poller-Task beenden.
            logger.exception("Unerwarteter Fehler beim Abfragen von %s", device.cfg.name)
            return
        if reading is None:
            return
        self.latest[reading["device_id"]] = reading
        self._last_success = time.monotonic()
        self._stall_notified = False
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
