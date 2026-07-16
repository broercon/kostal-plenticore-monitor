"""Regression: ein Wechselrichter, der nachts (Stromsparmodus) nur reduzierte
Prozessdaten (nur Statistik, keine Live-Leistung) liefert, muss tagsueber
automatisch wieder Live-Werte liefern - ohne Reconnect und ohne dass die
verbleibende Verbindung dauerhaft auf dem reduzierten Datensatz haengenbleibt.
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
    verfuegbare Datenpunkte + Werte."""

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


DEGRADED = {
    "scb:statistic:EnergyFlow": ["Statistic:EnergyHome:Day", "Statistic:Yield:Day"],
    "_virt_": ["Statistic:EnergyGrid:Day"],
}
FULL = {
    "devices:local": ["Home_P", "Grid_P", "Dc_P"],
    "devices:local:ac": ["P"],
    "devices:local:battery": ["P", "SoC"],
    "scb:statistic:EnergyFlow": ["Statistic:EnergyHome:Day", "Statistic:Yield:Day"],
    "_virt_": ["pv_P", "Statistic:EnergyGrid:Day"],
}
FULL_VALUES = {
    "devices:local": {"Home_P": 100.0, "Grid_P": -50.0, "Dc_P": 3000.0},
    "devices:local:ac": {"P": 2900.0},
    "devices:local:battery": {"P": 0.0, "SoC": 90.0},
    "_virt_": {"pv_P": 3000.0, "Statistic:EnergyGrid:Day": 0.0},
    "scb:statistic:EnergyFlow": {"Statistic:EnergyHome:Day": 1000.0, "Statistic:Yield:Day": 2000.0},
}


def test_reacquires_live_data_after_degraded_night():
    dev = PlenticoreDevice(InverterConfig(id="wr1", name="WR1", host="h", password="p"))
    dev._connected = True  # Verbindung besteht (wie nachts nach Reconnect)

    # Nachts: nur Statistik verfuegbar -> keine Live-Leistung.
    dev._client = _FakeClient(DEGRADED)
    r1 = asyncio.run(dev.fetch_reading())
    assert r1 is not None
    assert r1["pv_power_w"] is None
    assert r1["home_power_w"] is None

    # Tagsueber wieder voll verfuegbar -> Live-Werte kommen automatisch zurueck
    # (Abfrageliste wird bei jedem Abruf neu bestimmt), ohne Reconnect.
    dev._client = _FakeClient(FULL, FULL_VALUES)
    r2 = asyncio.run(dev.fetch_reading())
    assert r2["pv_power_w"] == 3000.0
    assert r2["home_power_w"] == 100.0
    assert r2["battery_soc_percent"] == 90.0
