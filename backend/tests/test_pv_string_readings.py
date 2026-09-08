"""Leistung je einzelnem PV-String (devices:local:pv1/pv2/pv3, jeweils "P")
wird rein informativ mit erfasst (siehe models.Reading.pv1_power_w) - fuer
eine spaetere Analyse bzw. eine moegliche stringgenaue Prognose. Die
bestehenden Berechnungen nutzen weiterhin ausschliesslich den vom Geraet
bereits aufsummierten "_virt_"/"pv_P"-Wert (pv_power_w), unveraendert.
"""
from __future__ import annotations

import asyncio

from app.config import InverterConfig
from app.plenticore_client import PlenticoreDevice


class _Val:
    def __init__(self, value):
        self.value = value


class _FakeClient:
    """Minimaler Ersatz fuer ExtendedApiClient: liefert konfigurierbare
    verfuegbare Datenpunkte + Werte (siehe test_plenticore_night_recovery.py)."""

    def __init__(self, available, values=None):
        self._available = available
        self._values = values or {}

    async def get_process_data(self):
        return self._available

    async def get_process_data_values(self, request):
        out = {}
        for module, ids in request.items():
            out[module] = {i: _Val(self._values.get(module, {}).get(i)) for i in ids}
        return out


def _device() -> PlenticoreDevice:
    dev = PlenticoreDevice(InverterConfig(id="wr1", name="WR1", host="h", password="p"))
    dev._connected = True
    return dev


def test_fetch_reading_includes_per_string_pv_power():
    dev = _device()
    dev._client = _FakeClient(
        available={
            "devices:local:pv1": ["P"],
            "devices:local:pv2": ["P"],
            "devices:local:pv3": ["P"],
            "_virt_": ["pv_P"],
        },
        values={
            "devices:local:pv1": {"P": 1200.0},
            "devices:local:pv2": {"P": 800.0},
            "devices:local:pv3": {"P": 500.0},  # z.B. Batterie an PV3
            "_virt_": {"pv_P": 2500.0},
        },
    )
    reading = asyncio.run(dev.fetch_reading())
    assert reading["pv1_power_w"] == 1200.0
    assert reading["pv2_power_w"] == 800.0
    assert reading["pv3_power_w"] == 500.0
    # Der summierte "_virt_"-Wert bleibt die fuer alle Berechnungen genutzte
    # Groesse, unveraendert von der zusaetzlichen Einzelerfassung.
    assert reading["pv_power_w"] == 2500.0


def test_fetch_reading_pv_string_absent_stays_none():
    """Geraete mit nur 2 belegten Stringeingaengen (kein PV3-Modul verfuegbar,
    oder eine Firmware-Version ohne diese Prozessdaten ueberhaupt) duerfen
    nicht zu einem Fehler fuehren - die fehlende Groesse bleibt einfach None,
    wie bei jedem anderen optionalen Datenpunkt (siehe PROCESS_DATA_CANDIDATES)."""
    dev = _device()
    dev._client = _FakeClient(
        available={
            "devices:local:pv1": ["P"],
            "devices:local:pv2": ["P"],
            "_virt_": ["pv_P"],
        },
        values={
            "devices:local:pv1": {"P": 1200.0},
            "devices:local:pv2": {"P": 800.0},
            "_virt_": {"pv_P": 2000.0},
        },
    )
    reading = asyncio.run(dev.fetch_reading())
    assert reading["pv1_power_w"] == 1200.0
    assert reading["pv2_power_w"] == 800.0
    assert reading["pv3_power_w"] is None
