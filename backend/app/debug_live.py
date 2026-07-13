"""Einmaliges Diagnose-Werkzeug: zeigt ALLE aktuell verfuegbaren
Live-Prozessdaten eines Wechselrichters an - nicht nur die kleine Auswahl,
die die App normalerweise nutzt (siehe PROCESS_DATA_CANDIDATES in
plenticore_client.py).

Hintergrund: Bei mehreren Wechselrichtern am selben Hausanschluss (z.B.
einer mit Batterie + KSEM als "Master", ein zweiter ohne eigenen
Netzzaehler, der per AC die Batterie des ersten mitlaedt) ist unklar, ob
das vom Master gemeldete "Grid_P" wirklich der echte Netzbezug/die echte
Einspeisung ist (so wie es das KSEM am Netzanschlusspunkt misst), oder ob
dort ein Mischwert steht, der auch die AC-Einspeisung des zweiten
Wechselrichters mit einschliesst. Dieses Skript macht diese Rohwerte
sichtbar, damit man sie direkt mit der Anzeige im Kostal-Solar-Portal
vergleichen kann (am besten gleichzeitig geoeffnet halten, auf die Uhrzeit
achten - die Werte aendern sich staendig).

Rein lesend, es wird nichts veraendert oder gespeichert.

Nutzung (innerhalb des laufenden Containers):

    docker compose exec kostal-monitor python -m app.debug_live \\
        --host 192.168.1.50 --password DEIN_PASSWORT

Optional --port (Standard 80), falls der Wechselrichter einen anderen
Port fuer die lokale REST-API verwendet.
"""
from __future__ import annotations

import argparse
import asyncio

import aiohttp
from pykoplenti import ExtendedApiClient


async def _run(host: str, password: str, port: int) -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        client = ExtendedApiClient(session, host, port=port)
        await client.login(password)

        available = await client.get_process_data()
        print("=" * 70)
        print("Verfuegbare Module und Kennungen (Uebersicht):")
        for module, keys in sorted(available.items()):
            print(f"  {module}: {', '.join(sorted(keys))}")

        values = await client.get_process_data_values(available)
        print("=" * 70)
        print("Aktuelle Werte (jetzt gerade - bitte mit dem Kostal-Portal zur")
        print("gleichen Zeit vergleichen):")
        for module in sorted(values.keys()):
            for key in sorted(values[module].keys()):
                item = values[module][key]
                unit = getattr(item, "unit", None) or ""
                print(f"  {module}/{key} = {item.value} {unit}".rstrip())
        print("=" * 70)
        print(
            "Besonders interessant fuer die Diagnose: devices:local/Grid_P\n"
            "(vergleiche den Wert mit dem, was das Kostal-Portal gerade als\n"
            "echten Netzbezug/Einspeisung anzeigt - z.B. die kleine Zahl neben\n"
            "dem Strommast-Symbol, NICHT die Zahl am Wechselrichter-Symbol)."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Zeigt ALLE aktuellen Live-Prozessdaten eines Plenticore-Wechselrichters "
            "an (Diagnose-Werkzeug, rein lesend)."
        )
    )
    parser.add_argument("--host", required=True, help="IP-Adresse des Wechselrichters")
    parser.add_argument(
        "--password", required=True, help="Geraetepasswort (wie im Kostal-Webinterface)"
    )
    parser.add_argument("--port", type=int, default=80)
    args = parser.parse_args()

    asyncio.run(_run(args.host, args.password, args.port))


if __name__ == "__main__":
    main()
