"""Wrapper um pykoplenti, der eine Verbindung zu einem Plenticore haelt und
regelmaessig einen flachen Messwert-Datensatz liefert.

Vorzeichen-Konvention fuer Grid_P (siehe Kostal/pykoplenti-Dokumentation):
- negativ  -> es wird Leistung ins Netz eingespeist (Einspeisung)
- positiv  -> es wird Leistung aus dem Netz bezogen (Netzbezug)

Falls das bei deinem Geraet genau andersherum sein sollte, kann das in
`_split_grid_power` einfach angepasst werden.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp
from pykoplenti import ApiException, ExtendedApiClient

from .config import InverterConfig, settings

logger = logging.getLogger(__name__)

# Welche Prozessdaten wir grundsaetzlich haben wollen (module_id -> [ids]).
# Wird beim Verbinden mit den tatsaechlich am Geraet verfuegbaren Daten
# abgeglichen, damit z.B. Geraete ohne Batterie nicht zu Fehlern fuehren.
PROCESS_DATA_CANDIDATES: dict[str, list[str]] = {
    "devices:local": ["Home_P", "Grid_P", "Dc_P"],
    "devices:local:battery": ["P", "SoC"],
    "scb:statistic:EnergyFlow": ["Statistic:EnergyHome:Day", "Statistic:Yield:Day"],
    "_virt_": ["pv_P", "Statistic:EnergyGrid:Day"],
}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_grid_power(grid_p: float | None) -> tuple[float | None, float | None]:
    """Teilt Grid_P in (Einspeiseleistung, Netzbezug) auf, beide >= 0.

    Die Vorzeichen-Konvention haengt von der Installation ab (Ausrichtung
    des Stromzaehlers/CT-Clamps). Mit GRID_POWER_INVERTED=true (config.py)
    laesst sich das umdrehen, falls Einspeisung/Netzbezug vertauscht
    erscheinen."""
    if grid_p is None:
        return None, None
    if settings.grid_power_inverted:
        grid_p = -grid_p
    feed_in = max(0.0, -grid_p)
    grid_draw = max(0.0, grid_p)
    return feed_in, grid_draw


def _wh_to_kwh(value_wh: Any) -> float | None:
    """Die Energie-Statistikwerte des Plenticore liefern Wh, wir speichern kWh."""
    v = _to_float(value_wh)
    if v is None:
        return None
    return round(v / 1000, 3)


class PlenticoreDevice:
    """Haelt die Verbindung zu genau einem Plenticore-Wechselrichter."""

    def __init__(self, cfg: InverterConfig):
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None
        self._client: ExtendedApiClient | None = None
        self._request: dict[str, list[str]] = {}
        self._connected = False

    async def _connect(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
        client = ExtendedApiClient(self._session, self.cfg.host, port=self.cfg.port)
        await client.login(self.cfg.password)

        available = await client.get_process_data()
        request: dict[str, list[str]] = {}
        for module, keys in PROCESS_DATA_CANDIDATES.items():
            if module in available:
                present = [k for k in keys if k in available[module]]
                if present:
                    request[module] = present

        self._client = client
        self._request = request
        self._connected = True
        logger.info(
            "Verbunden mit %s (%s), verfuegbare Datenpunkte: %s",
            self.cfg.name,
            self.cfg.host,
            request,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._client = None
        self._connected = False

    async def fetch_reading(self) -> dict[str, Any] | None:
        """Liest einen Satz Prozessdaten. Gibt None zurueck, wenn das Geraet
        gerade nicht erreichbar ist (statt eine Exception zu werfen), damit der
        Poller andere Geraete/Zyklen nicht beeintraechtigt."""
        try:
            if not self._connected:
                await self._connect()
            assert self._client is not None
            values = await self._client.get_process_data_values(self._request)
        except (aiohttp.ClientError, ApiException, TimeoutError, OSError) as exc:
            logger.warning(
                "%s (%s) nicht erreichbar (%s) - verbinde neu",
                self.cfg.name,
                self.cfg.host,
                exc,
            )
            await self.close()
            try:
                await self._connect()
                assert self._client is not None
                values = await self._client.get_process_data_values(self._request)
            except Exception as exc2:  # noqa: BLE001
                logger.warning(
                    "Erneuter Verbindungsversuch zu %s fehlgeschlagen: %s",
                    self.cfg.name,
                    exc2,
                )
                await self.close()
                return None

        def val(module: str, key: str) -> Any:
            try:
                return values[module][key].value
            except KeyError:
                return None

        home_p = _to_float(val("devices:local", "Home_P"))
        grid_p = _to_float(val("devices:local", "Grid_P"))
        feed_in_w, grid_draw_w = _split_grid_power(grid_p)

        return {
            "device_id": self.cfg.id,
            "device_name": self.cfg.name,
            "timestamp": datetime.now(timezone.utc),
            "home_power_w": home_p,
            "grid_power_w": grid_p,
            "feed_in_power_w": feed_in_w,
            "grid_draw_power_w": grid_draw_w,
            "pv_power_w": _to_float(val("_virt_", "pv_P")),
            "battery_power_w": _to_float(val("devices:local:battery", "P")),
            "battery_soc_percent": _to_float(val("devices:local:battery", "SoC")),
            "yield_day_kwh": _wh_to_kwh(
                val("scb:statistic:EnergyFlow", "Statistic:Yield:Day")
            ),
            "home_consumption_day_kwh": _wh_to_kwh(
                val("scb:statistic:EnergyFlow", "Statistic:EnergyHome:Day")
            ),
            "energy_grid_day_kwh": _wh_to_kwh(val("_virt_", "Statistic:EnergyGrid:Day")),
        }
