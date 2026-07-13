"""Regressionstests fuer einen konkreten Praxis-Bug: Das Polling blieb
nachts (offenbar wenn ein Wechselrichter die Verbindung auf ungewoehnliche
Weise trennt, z.B. beim eigenen naechtlichen Neustart/Stromsparmodus) fuer
den Rest des Tages/der Nacht stehen und erholte sich erst nach einem
manuellen Container-Neustart wieder.

Ursache: `PlenticoreDevice.fetch_reading()` fing nur eine feste Liste
"erwarteter" Fehlertypen ab (aiohttp.ClientError, ApiException,
TimeoutError, OSError). Ein davon abweichender Fehlertyp (z.B. ein
JSON-/Typ-Fehler bei einer kaputten Antwort des Geraets) lief ungebremst
durch die Methode hindurch und beendete den Poller-Hintergrund-Task fuer
ALLE Geraete dauerhaft.

Diese Tests stellen sicher, dass (a) fetch_reading() JEDEN Fehlertyp
abfaengt und stattdessen None zurueckgibt, und (b) selbst wenn das doch
irgendwo durchrutscht, der Poller pro Geraet isoliert ist (ein Fehler bei
einem Geraet darf die anderen/den naechsten Zyklus nicht verhindern).
"""
from __future__ import annotations

import asyncio

from app.config import InverterConfig
from app.plenticore_client import PlenticoreDevice
from app.poller import Poller


class _AlwaysFailingClient:
    """Attrappe fuer den pykoplenti-Client: wirft bei jedem Aufruf einen
    Fehlertyp, der VOR dem Fix nicht in der Except-Liste von
    fetch_reading() stand (z.B. ein simulierter Parsing-Fehler)."""

    async def get_process_data_values(self, request):
        raise ValueError("kaputte/unerwartete Antwort vom Wechselrichter")


def test_fetch_reading_never_raises_on_unexpected_error_type():
    """Frueher: ein nicht in der Except-Liste enthaltener Fehlertyp lief
    ungebremst aus fetch_reading() heraus. Jetzt: IMMER None statt einer
    Exception, unabhaengig vom konkreten Fehlertyp."""

    async def run():
        cfg = InverterConfig(id="wr-test", name="Test-WR", host="192.0.2.1", password="x")
        device = PlenticoreDevice(cfg)

        async def fake_connect():
            device._client = _AlwaysFailingClient()
            device._connected = True

        device._connect = fake_connect  # type: ignore[method-assign]
        device._connected = False

        result = await device.fetch_reading()
        assert result is None

    asyncio.run(run())


def test_poller_isolates_a_single_device_failure():
    """Selbst wenn device.fetch_reading() (entgegen seines eigenen Vertrags)
    doch einmal direkt eine Exception wirft, darf das den Poller-Zyklus
    nicht abbrechen - andere Geraete/der naechste Zyklus muessen
    unbeeintraechtigt bleiben."""

    class _FakeCfg:
        name = "Kaputter Wechselrichter"

    class _FakeBrokenDevice:
        cfg = _FakeCfg()

        async def fetch_reading(self):
            raise RuntimeError("ganz unerwarteter Fehler")

    async def run():
        poller_instance = Poller()
        broken_device = _FakeBrokenDevice()

        # Darf NICHT propagieren - genau das war der Bug, der den ganzen
        # Poller-Task (und damit alle Geraete) dauerhaft beendet hat.
        await poller_instance._poll_once(broken_device)  # type: ignore[arg-type]

        # Und der Poller muss danach weiterhin benutzbar sein (kein
        # kaputter/inkonsistenter Zustand).
        assert broken_device.cfg.name not in poller_instance.latest

    asyncio.run(run())
