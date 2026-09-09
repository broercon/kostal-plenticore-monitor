"""Hilfsfunktionen, um Rohmesswerte fuer Diagramme in Zeit-Buckets zu mitteln."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .models import Reading

HISTORY_FIELDS = [
    "home_power_w",
    "feed_in_power_w",
    "grid_draw_power_w",
    "pv_power_w",
    "battery_power_w",
    "ac_power_w",
]


def _bucket_key(ts: datetime, bucket_seconds: int) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() // bucket_seconds) * bucket_seconds


def aggregate_per_device(
    rows: list[Reading], bucket_seconds: int
) -> dict[str, dict[int, dict[str, float | None]]]:
    """Gruppiert Messwerte pro Geraet in Zeit-Buckets und mittelt sie.

    Rueckgabe: {device_id: {bucket_epoch_sekunden: {feld: mittelwert}}}
    """
    sums: dict[tuple[str, int], dict[str, float]] = {}
    counts: dict[tuple[str, int], dict[str, int]] = {}

    for row in rows:
        bk = _bucket_key(row.timestamp, bucket_seconds)
        key = (row.device_id, bk)
        s = sums.setdefault(key, {f: 0.0 for f in HISTORY_FIELDS})
        c = counts.setdefault(key, {f: 0 for f in HISTORY_FIELDS})
        for field in HISTORY_FIELDS:
            value = getattr(row, field)
            if value is not None:
                s[field] += value
                c[field] += 1

    result: dict[str, dict[int, dict[str, float | None]]] = {}
    for (device_id, bk), s in sums.items():
        c = counts[(device_id, bk)]
        avgs = {f: (s[f] / c[f] if c[f] > 0 else None) for f in HISTORY_FIELDS}
        result.setdefault(device_id, {})[bk] = avgs
    return result


def build_battery_soc_day_series(
    rows: list[Reading], bucket_minutes: int, timezone_name: str
) -> dict:
    """Speicherstand (Ladezustand, %) je lokalem Kalendertag - wie
    day_profile(), aber fuer battery_soc_percent und bewusst GETRENNT von
    HISTORY_FIELDS/combine_devices: ein Prozentwert darf beim Kombinieren
    mehrerer Geraete ("Alle (Summe)" wie beim Leistungsverlauf) nicht
    aufsummiert werden (zwei Batterien bei je 50 % waeren zusammen nicht
    "100 %"). Jedes Geraet mit Batterie bekommt daher weiterhin eine
    eigene Kurve - hier zusaetzlich je Kalendertag, damit sich einzelne
    Tage auf einer gemeinsamen 00:00-24:00-Achse direkt vergleichen
    lassen, statt in einer einzigen langen Linie ueber mehrere Tage hinweg
    zu verschwimmen.

    Rueckgabe: {"devices": [{"device_id","device_name"}, ...], "days": [
    {"date": "YYYY-MM-DD", "points": [{"minute": int, "values":
    {device_id: prozent|None}}, ...]}, ...]}, Tage aufsteigend nach Datum
    sortiert (aeltester Tag zuerst, wie day_profile()). Jeder Punkt
    enthaelt fuer JEDES Geraet mit Batterie im GESAMTEN angefragten
    Zeitraum einen Eintrag (None, wenn an diesem Tag/Bucket kein Messwert
    vorliegt), damit das Frontend pro Geraet eine luekenlose Kurve bauen
    kann (Chart.js "spanGaps"). Geraete ganz ohne SoC-Messwert im Zeitraum
    (z. B. weil sie keine Batterie haben) tauchen gar nicht erst auf.
    """
    tz = ZoneInfo(timezone_name)
    sums: dict[tuple[str, int, str], float] = {}
    counts: dict[tuple[str, int, str], int] = {}
    device_names: dict[str, str] = {}

    for row in rows:
        if row.battery_soc_percent is None:
            continue
        device_names.setdefault(row.device_id, row.device_name)
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        date_str = local.strftime("%Y-%m-%d")
        minute_of_day = local.hour * 60 + local.minute
        bucket = (minute_of_day // bucket_minutes) * bucket_minutes
        key = (date_str, bucket, row.device_id)
        sums[key] = sums.get(key, 0.0) + row.battery_soc_percent
        counts[key] = counts.get(key, 0) + 1

    device_ids = list(device_names.keys())

    by_date: dict[str, dict[int, dict[str, float]]] = {}
    for (date_str, bucket, device_id), total in sums.items():
        avg = total / counts[(date_str, bucket, device_id)]
        by_date.setdefault(date_str, {}).setdefault(bucket, {})[device_id] = avg

    days = []
    for date_str in sorted(by_date.keys()):
        buckets = by_date[date_str]
        points = [
            {
                "minute": bucket,
                "values": {d: buckets[bucket].get(d) for d in device_ids},
            }
            for bucket in sorted(buckets.keys())
        ]
        days.append({"date": date_str, "points": points})

    devices = [{"device_id": d, "device_name": device_names[d]} for d in device_ids]
    return {"devices": devices, "days": days}


def combine_devices(
    per_device: dict[str, dict[int, dict[str, float | None]]],
    has_grid_meter: dict[str, bool] | None = None,
    battery_power_inverted: dict[str, bool] | None = None,
    *,
    raw_battery_output: bool = False,
) -> dict[int, dict[str, float | None]]:
    """Kombiniert die pro-Geraet gemittelten Buckets zu einer Gesamtzeitreihe
    ("Alle (Summe)").

    Standardverhalten (has_grid_meter=None, oder alle Geraete darin True -
    also unveraendert gegenueber frueheren Versionen): jedes Feld wird
    einfach ueber alle Geraete summiert. Das ist korrekt, solange entweder
    nur ein Geraet konfiguriert ist, oder jedes Geraet tatsaechlich einen
    eigenen, unabhaengigen Netzanschluss hat.

    Sobald aber has_grid_meter fuer MINDESTENS EIN Geraet explizit auf False
    gesetzt ist (typischer Fall: zwei Wechselrichter am selben
    Hausanschluss, nur einer hat den echten Netzzaehler/KSEM, der andere
    laedt z.B. per AC dessen Batterie mit), wird stattdessen eine korrigierte
    Energiebilanz verwendet:

    - PV-Leistung wird weiter ueber ALLE Geraete summiert (jedes Geraet
      kennt zuverlaessig nur seine eigenen PV-Strings, das ist unabhaengig
      vom Hausanschluss korrekt) - dient nur der Anzeige, nicht mehr der
      Hausverbrauchs-Berechnung (siehe unten, ac_power_w).
    - Batterieleistung wird ebenfalls ueber alle Geraete summiert (nur das
      Geraet mit Batterie liefert ueberhaupt einen Wert), je Geraet optional
      vorzeichenkorrigiert (battery_power_inverted).
    - Netzbezug/Einspeisung werden NICHT summiert, sondern NUR von den als
      has_grid_meter=True markierten Geraeten uebernommen - ein zweites,
      nicht gemessenes (oder dupliziertes) Grid_P wuerde den echten Wert
      sonst verfaelschen.
    - Hausverbrauch wird nicht aus den einzelnen (potenziell falschen)
      Home_P-Werten summiert, sondern aus der Energiebilanz neu berechnet -
      bevorzugt ueber die AC-seitige Nettoleistung jedes Geraets
      (ac_power_w, positiv = Leistung geht vom Geraet Richtung Hausnetz,
      negativ = Leistung kommt von aussen ins Geraet):

          Home = AC-Leistung_gesamt + Netzbezug_echt - Einspeisung_echt

      Das ist genauer als die Variante mit pv_power_w (DC, siehe unten),
      weil ac_power_w bereits die tatsaechlich am Hausnetz ankommende/
      abgehende Leistung ist (nach Wechselrichter-eigenen DC->AC-
      Umwandlungsverlusten) UND das eigene Batterieladen/-entladen jedes
      Geraets automatisch mit einschliesst (rein DC-seitige Ladung aus
      eigener PV taucht in ac_power_w gar nicht erst auf - nur was
      tatsaechlich die AC-Seite quert).

      Fallback fuer Messwerte von VOR diesem Feature (ac_power_w noch
      nicht erfasst, also NULL): Home = PV_gesamt (DC) + Netzbezug_echt -
      Einspeisung_echt + Batterieleistung. Das ist etwas ungenauer, weil
      PV hier die DC-Erzeugung VOR den Umwandlungsverlusten ist - der
      Wechselrichter-eigene Umwandlungsverlust (typischerweise einige
      Prozent) erscheint dabei faelschlich als zusaetzlicher
      "Hausverbrauch".

      Hintergrund: Ein Wechselrichter, der nicht weiss, dass ein zweiter
      Wechselrichter am selben Hausanschluss Energie einspeist, rechnet sich
      bei geladener Batterie sonst ein negatives/unsinniges "Home_P" zusammen
      (siehe README-Abschnitt "Mehrere Wechselrichter: Hausverbrauch/Netz
      korrekt berechnen").

      Absicherung: Hausverbrauch kann physikalisch nie negativ sein - sein
      gueltiger Wertebereich ist [0, unendlich). Kommt obige Formel (aus
      Mess-/Zeitversatz zwischen KSEM und Wechselrichter-Sensoren oder
      ungewoehnlich hohen Umwandlungsverlusten an einzelnen Zeitpunkten)
      dennoch auf einen negativen Wert, wird er auf 0 begrenzt (nicht auf
      "unbekannt"/None gesetzt) - ein leicht negativer Rohwert bedeutet in
      der Praxis "Verbrauch ungefaehr 0", das ist eine plausible, konkrete
      Aussage und keine unbekannte Groesse. Diese Begrenzung gilt fuer BEIDE
      Varianten (Standard-Summe und korrigierte Energiebilanz), da die
      physikalische Grenze unabhaengig vom Berechnungsweg gilt.
    """
    # Energie-Auswertungen ziehen den Batterieanteil vom rohen PV-Wert ab.
    # Dafuer muss auch die ausgegebene Batterie roh bleiben; die Hausbilanz
    # verwendet unabhaengig davon weiterhin das korrigierte Vorzeichen.
    has_grid_meter = has_grid_meter or {}
    battery_power_inverted = battery_power_inverted or {}
    device_ids = list(per_device.keys())

    # WICHTIG: ob die korrigierte Energiebilanz greift, wird anhand der
    # STATISCHEN KONFIGURATION entschieden (has_grid_meter, von main.py immer
    # aus ALLEN konfigurierten Geraeten gebaut), NICHT anhand dessen, welche
    # Geraete zufaellig Messwerte fuer das aktuell betrachtete Zeitfenster
    # haben. Sonst wuerde z.B. an einem Tag, an dem der nicht gemessene
    # zweite Wechselrichter (has_grid_meter=false) voruebergehend keine
    # Messwerte lieferte (Ausfall/noch nicht verbunden), device_ids nur den
    # ersten Wechselrichter enthalten - und die korrigierte Logik wuerde
    # faelschlich deaktiviert, sodass wieder dessen rohe (potenziell falsche)
    # Home_P-Werte durchgereicht wuerden, obwohl der zweite Wechselrichter
    # physisch trotzdem Energie eingespeist haben kann (nur eben ohne
    # gespeicherte Messwerte fuer dieses Zeitfenster).
    explicit_non_metered = [d for d, metered in has_grid_meter.items() if metered is False]
    use_corrected_logic = len(explicit_non_metered) > 0

    all_buckets: set[int] = set()
    for buckets in per_device.values():
        all_buckets.update(buckets.keys())

    if not use_corrected_logic:
        combined: dict[int, dict[str, float | None]] = {}
        for bk in all_buckets:
            merged: dict[str, float | None] = {}
            for field in HISTORY_FIELDS:
                total = None
                for buckets in per_device.values():
                    point = buckets.get(bk)
                    if point is None:
                        continue
                    value = point.get(field)
                    if value is None:
                        continue
                    total = (total or 0.0) + value
                merged[field] = total
            # Hausverbrauch kann physikalisch nicht negativ sein (siehe
            # Docstring "Absicherung") - gilt auch fuer die einfache Summe.
            if merged.get("home_power_w") is not None and merged["home_power_w"] < 0:
                merged["home_power_w"] = 0.0
            combined[bk] = merged
        return combined

    metered_devices = [d for d in device_ids if has_grid_meter.get(d, True)]
    if not metered_devices:
        # Sollte nicht vorkommen (dann waere use_corrected_logic=False), aber
        # sicherheitshalber lieber alle Geraete verwenden als gar keinen
        # Netzwert zu haben.
        metered_devices = device_ids

    def _sum_field(field: str, devices: list[str], bk: int) -> float | None:
        total = None
        for d in devices:
            point = per_device.get(d, {}).get(bk)
            if point is None:
                continue
            value = point.get(field)
            if value is None:
                continue
            total = (total or 0.0) + value
        return total

    combined = {}
    for bk in all_buckets:
        pv_total = _sum_field("pv_power_w", device_ids, bk)
        ac_total = _sum_field("ac_power_w", device_ids, bk)
        grid_draw_true = _sum_field("grid_draw_power_w", metered_devices, bk)
        feed_in_true = _sum_field("feed_in_power_w", metered_devices, bk)

        battery_total = None
        for d in device_ids:
            point = per_device.get(d, {}).get(bk)
            if point is None:
                continue
            value = point.get("battery_power_w")
            if value is None:
                continue
            if battery_power_inverted.get(d, False):
                value = -value
            battery_total = (battery_total or 0.0) + value

        home_true = None
        if grid_draw_true is not None and feed_in_true is not None:
            if ac_total is not None:
                # Bevorzugt: AC-seitige Nettoleistung (siehe Docstring) -
                # schliesst Batterieladung/-entladung bereits mit ein.
                home_true = ac_total + grid_draw_true - feed_in_true
            elif pv_total is not None:
                # Fallback fuer Messwerte von vor diesem Feature (kein
                # ac_power_w vorhanden) - etwas ungenauer, siehe Docstring.
                home_true = pv_total + grid_draw_true - feed_in_true + (battery_total or 0.0)

        if home_true is not None and home_true < 0:
            # Hausverbrauch kann physikalisch nicht negativ sein (siehe
            # Docstring "Absicherung") - ein negativer Wert bedeutet, dass
            # die Energiebilanz fuer genau diesen Zeitpunkt leicht daneben
            # liegt (z.B. weil KSEM und Wechselrichter-Sensoren nicht exakt
            # zeitgleich gemessen haben, oder bei der DC-Fallback-Formel die
            # geraeteeigenen Umwandlungsverluste an diesem Punkt ungewoehnlich
            # hoch ausgefallen sind). Auf 0 begrenzen statt eine negative
            # Zahl (oder gar einen negativen "Netzbezug-Anteil" im
            # Tagesverbrauch-Diagramm) anzuzeigen.
            home_true = 0.0

        combined[bk] = {
            "home_power_w": home_true,
            "feed_in_power_w": feed_in_true,
            "grid_draw_power_w": grid_draw_true,
            "pv_power_w": pv_total,
            "battery_power_w": (
                _sum_field("battery_power_w", device_ids, bk)
                if raw_battery_output else battery_total
            ),
            "ac_power_w": ac_total,
        }
    return combined


def combine_latest_readings(
    readings: list[dict],
    has_grid_meter: dict[str, bool] | None = None,
    battery_power_inverted: dict[str, bool] | None = None,
) -> dict[str, float | None] | None:
    """Wie combine_devices(), aber fuer eine einzelne Momentaufnahme (z.B.
    die aktuellsten Werte je Geraet aus dem Poller) statt einer Zeitreihe -
    fuer die Live-Kacheln im Dashboard. `readings` ist eine Liste flacher
    Dicts mit mindestens "device_id" und den HISTORY_FIELDS. Nutzt intern
    dieselbe Logik wie combine_devices() (ein einzelner "Bucket")."""
    if not readings:
        return None
    per_device = {
        reading["device_id"]: {0: {field: reading.get(field) for field in HISTORY_FIELDS}}
        for reading in readings
    }
    combined = combine_devices(per_device, has_grid_meter, battery_power_inverted)
    return combined.get(0)


# Maximale Zeitluecke zwischen zwei aufeinanderfolgenden Messpunkten, die
# integrate_kwh() noch per Trapezregel ueberbrueckt (interpoliert). Bei
# laengeren Luecken (z.B. Poller-Ausfall, Wechselrichter voruebergehend
# nicht erreichbar, fehlende Netzwerte bei einem Teil der Ablesungen) wuerde
# das lineare Ueberbruecken den letzten bekannten Wert ueber Stunden hinweg
# fortschreiben und so die Energiemenge stark verfaelschen (beobachtet:
# ein Tag mit vielen Datenluecken ergab eine unplausible PV-Tagessumme von
# über 100 kWh fuer eine deutlich kleinere Anlage). Bei einer Luecke ueber
# dieser Schwelle wird das Intervall stattdessen uebersprungen (traegt 0 bei),
# statt ueber die Luecke hinweg zu interpolieren.
MAX_INTEGRATION_GAP_HOURS = 0.5  # 30 Minuten

# Verdichtete Altdaten (siehe downsampling.py: alte Rohmesswerte werden ab
# RAW_DATA_RETENTION_DAYS auf einen Punkt pro Stunde und Geraet reduziert)
# haben normalerweise einen Abstand von genau 1h zwischen zwei Punkten -
# das ist dort KEINE Datenluecke und darf nicht wie oben uebersprungen
# werden. Ein Tag mit hoechstens DOWNSAMPLED_DAY_MAX_POINTS Punkten gilt
# als verdichtet (ein normaler Tag hat bei 15s-Polling mehrere tausend, bei
# importierten Logdaten immer noch typischerweise hunderte Punkte - die
# Luecke zwischen "verdichtet" und "normal aufgeloest" ist strukturell
# riesig, ein Schwellwert reicht daher zur Unterscheidung). Der grosszuegige
# Faktor (3h statt genau 1h) toleriert dabei weiterhin kleinere
# Unregelmaessigkeiten, faengt aber einen ECHTEN mehrstuendigen Ausfall
# innerhalb bereits verdichteter Daten weiterhin als Luecke ab.
DOWNSAMPLED_DAY_MAX_POINTS = 30
DOWNSAMPLED_MAX_GAP_HOURS = 3.0


def gap_hours_for_day(point_count: int) -> float | None:
    """Welche max_gap_hours integrate_kwh() fuer eine Gruppe von Messpunkten
    EINES Kalendertages verwenden sollte, anhand von deren Anzahl - siehe
    Konstanten oben. None bedeutet "Standard" (MAX_INTEGRATION_GAP_HOURS,
    normal aufgeloeste Daten). Separate, kleine Funktion, damit alle
    taeglich gruppierenden Aufrufer (daily_kwh_totals, daily_pv_yield_totals,
    daily_home_source_breakdown_kwh) dieselbe Schwelle verwenden."""
    return DOWNSAMPLED_MAX_GAP_HOURS if point_count <= DOWNSAMPLED_DAY_MAX_POINTS else None


def gap_hours_for_points(points: list[tuple[datetime, float]]) -> float | None:
    """Wie gap_hours_for_day(), aber fuer eine (bereits nach Zeit sortierte)
    Punktreihe, die MEHRERE Tage umspannen kann (siehe
    daily_battery_energy_flows, das - anders als die uebrigen daily_*-
    Funktionen - nicht vorab pro Kalendertag gruppiert, sondern
    Tagesgrenzen erst waehrend der Integration selbst beruecksichtigt).
    Entscheidet daher anhand der DICHTE (Punkte pro Tag im ueberspannten
    Zeitraum) statt der absoluten Anzahl. WICHTIG: der ueberspannte
    Zeitraum darf NICHT nach unten auf einen ganzen Tag begrenzt werden -
    sonst wuerden z.B. zwei Punkte im Abstand von nur 1h (offensichtlich
    normal aufgeloeste Daten, nur zufaellig wenige) faelschlich als
    "verdichtet" gelten (2 Punkte / 1 Tag = niedrige Dichte trotz
    tatsaechlich engem Abstand)."""
    if len(points) < 2:
        return None
    span_hours = max(1e-9, (points[-1][0] - points[0][0]).total_seconds() / 3600)
    density_per_day = len(points) * 24 / span_hours
    return DOWNSAMPLED_MAX_GAP_HOURS if density_per_day <= DOWNSAMPLED_DAY_MAX_POINTS else None


def integrate_kwh(
    rows: list[Reading], field: str, max_gap_hours: float | None = None
) -> float | None:
    """Integriert eine Leistungs-Zeitreihe (Watt) zu einer Energiemenge (kWh),
    per Trapezregel ueber die vorhandenen Messpunkte.

    Wird als Fallback genutzt, wenn der Wechselrichter selbst keinen
    passenden Tages-Statistikwert liefert (z.B. eingeschraenkter Nutzer-Login
    ohne Zugriff auf das Statistik-Modul, oder fehlende Batterie fuer den
    virtuellen Einspeise-Wert).

    Intervalle, die laenger als max_gap_hours auseinanderliegen (Standard:
    MAX_INTEGRATION_GAP_HOURS, siehe Konstante oben fuer die Begruendung -
    z.B. durch eine Datenluecke), werden NICHT interpoliert, sondern
    uebersprungen - das unterschaetzt die tatsaechliche Energiemenge in der
    Luecke leicht (dort fehlen dann echte Messwerte), ist aber deutlich
    naeher an der Wahrheit als eine grobe lineare Fortschreibung ueber
    Stunden hinweg. Aufrufer mit verdichteten Altdaten (siehe
    gap_hours_for_day) uebergeben hier einen groesseren Wert.
    """
    gap_limit = MAX_INTEGRATION_GAP_HOURS if max_gap_hours is None else max_gap_hours
    points = sorted(
        (
            (row.timestamp, getattr(row, field))
            for row in rows
            if getattr(row, field) is not None
        ),
        key=lambda p: p[0],
    )
    if len(points) < 2:
        return None

    energy_wh = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        dt_hours = (t1 - t0).total_seconds() / 3600
        if dt_hours <= 0 or dt_hours > gap_limit:
            continue
        energy_wh += (p0 + p1) / 2 * dt_hours
    return round(energy_wh / 1000, 3)


def pure_pv_power_w(pv_power_w: float, battery_power_w: float | None) -> float:
    """Reine PV-Leistung (W) - PV-Leistung OHNE Batterie-Anteil, fuer einen
    einzelnen Messpunkt.

    Bei Anlagen, deren Batterie am dritten PV-String (PV3) haengt, enthaelt
    pv_power_w (= pv1+pv2+pv3, siehe pykoplenti-Virtualwert pv_P) auch die
    Batterie. Da dort pv3 = Batterie ist, gilt reine PV = pv1+pv2 =
    pv_power_w - battery_power_w. Beide Groessen sind ROH gespeichert; die
    Subtraktion ist damit vorzeichensicher (die rohe Batteriegroesse, die in
    pv_power_w steckt, wird exakt wieder abgezogen) - unabhaengig davon, ob die
    Batterie gerade laedt oder entlaedt. Auf >= 0 begrenzt (Messrauschen).
    Geraete ohne Batterie (battery_power_w = None) liefern schlicht
    pv_power_w. Nachts ist pv1+pv2 = 0, daher auch die reine PV = 0.

    Einzige Quelle dieser Formel - wird sowohl von integrate_pure_pv_kwh()
    hier als auch (als aequivalenter SQL-Ausdruck, siehe dortiger Kommentar)
    von energy_forecast.load_hourly_pv_history() verwendet. Ein Cross-Check-
    Test (test_energy_forecast.py) stellt sicher, dass beide Implementierungen
    dieselben Werte liefern.
    """
    return max(0.0, pv_power_w - (battery_power_w or 0.0))


def integrate_pure_pv_kwh(
    rows: list[Reading], max_gap_hours: float | None = None
) -> float | None:
    """Reine PV-Erzeugung (kWh) ueber mehrere Messpunkte - siehe
    pure_pv_power_w() fuer die zugrunde liegende Formel je Messpunkt.
    max_gap_hours wird unveraendert an integrate_kwh() durchgereicht (siehe
    dort/gap_hours_for_day - fuer verdichtete Altdaten)."""
    points = [
        SimpleNamespace(
            timestamp=r.timestamp,
            value=pure_pv_power_w(r.pv_power_w, r.battery_power_w),
        )
        for r in rows
        if r.pv_power_w is not None
    ]
    return integrate_kwh(points, "value", max_gap_hours=max_gap_hours)


# Felder, die fuer das Tagesvergleichs-Diagramm gemittelt werden. feed_in_power_w
# wird nur intern fuer die Solar/Batterie-Aufteilung gebraucht (siehe unten) und
# nicht direkt an den Client zurueckgegeben.
DAY_PROFILE_RAW_FIELDS = ["pv_power_w", "home_power_w", "grid_draw_power_w", "feed_in_power_w", "battery_power_w"]


def day_profile(
    rows: list[Reading], bucket_minutes: int, timezone_name: str
) -> list[dict]:
    """Gruppiert Messwerte nach lokalem Kalendertag und Uhrzeit-Bucket
    (0..1440 Minuten seit lokaler Mitternacht), damit sich mehrere Tage im
    Diagramm ueberlagern und auf einer gemeinsamen 00:00-24:00-Achse
    vergleichen lassen.

    Berechnet zusaetzlich eine Aufteilung des Hausverbrauchs in "aus Solar"
    und "aus Batterie" - rein aus der Leistungsbilanz (reine PV + Netzbezug +
    Batterie = Hausverbrauch + Einspeisung), OHNE von einer bestimmten
    Vorzeichen-Konvention der Batterieleistung auszugehen (die je nach
    Geraet/Firmware unterschiedlich sein kann). Dafuer werden PV-, Haus- und
    Netzwerte benoetigt; bei importierten Altdaten ohne Netzmessung (KSEM-
    Limitation, siehe import_logdata.py) bleibt die Aufteilung leer - dort
    funktioniert nur die reine PV-Kurve.

    WICHTIG: die Bilanz MUSS mit der reinen PV (pv_pure, Batterie am PV3-
    String herausgerechnet) statt dem rohen pv_power_w rechnen. Haengt die
    Batterie am PV3-String, gilt pv_power_w = pv_pure + battery_power_w (siehe
    pure_pv_power_w) - mit dem rohen pv_power_w wuerde battery_net dann
    IMMER zu 0 aufgehen (home + feed_in - pv_power_w - grid_draw =
    home + feed_in - pv_pure - battery_power_w - grid_draw, und der erste
    Teil ist per Definition battery_power_w), die Aufteilung wuerde also
    jegliche Batterie-Entladung faelschlich komplett der Solarerzeugung
    zuschlagen. Mit pv_pure kuerzt sich das korrekt zu battery_power_w.

    Rueckgabe: Liste von {"date": "YYYY-MM-DD", "points": [...]}, aufsteigend
    nach Datum sortiert (aeltester Tag zuerst).
    """
    tz = ZoneInfo(timezone_name)
    sums: dict[tuple[str, int], dict[str, float]] = {}
    counts: dict[tuple[str, int], dict[str, int]] = {}

    for row in rows:
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        date_str = local.strftime("%Y-%m-%d")
        minute_of_day = local.hour * 60 + local.minute
        bucket = (minute_of_day // bucket_minutes) * bucket_minutes
        key = (date_str, bucket)
        s = sums.setdefault(key, {f: 0.0 for f in DAY_PROFILE_RAW_FIELDS})
        c = counts.setdefault(key, {f: 0 for f in DAY_PROFILE_RAW_FIELDS})
        for field in DAY_PROFILE_RAW_FIELDS:
            value = getattr(row, field)
            if value is not None:
                s[field] += value
                c[field] += 1

    by_date: dict[str, dict[int, dict]] = {}
    for (date_str, bucket), s in sums.items():
        c = counts[(date_str, bucket)]
        avg = {f: (s[f] / c[f] if c[f] > 0 else None) for f in DAY_PROFILE_RAW_FIELDS}

        pv = avg["pv_power_w"]
        home = avg["home_power_w"]
        grid_draw = avg["grid_draw_power_w"]
        feed_in = avg["feed_in_power_w"]
        battery = avg["battery_power_w"]
        # Reine PV-Erzeugung: die ggf. am PV3-String haengende Batterie
        # herausrechnen (siehe integrate_pure_pv_kwh) - fuer die Anzeige UND
        # fuer die Energiebilanz unten (battery_net), siehe Docstring oben.
        pv_pure = max(0.0, pv - (battery or 0.0)) if pv is not None else None

        home_from_solar = None
        home_from_battery = None
        if home is not None and grid_draw is not None and pv is not None and feed_in is not None:
            remaining_home = max(0.0, home - grid_draw)
            # Energiebilanz: positiver Wert = Batterie liefert gerade Leistung
            # (Entladung), negativer Wert = Batterie laedt gerade (nimmt einen
            # Teil der PV-Erzeugung auf). Mit der REINEN PV (pv_pure), nicht
            # dem rohen pv - siehe Docstring oben (PV3-Batterie-Faelle sonst
            # immer 0).
            battery_net = home + feed_in - pv_pure - grid_draw
            battery_share = min(remaining_home, battery_net) if battery_net > 0 else 0.0
            home_from_battery = round(battery_share, 1)
            home_from_solar = round(remaining_home - battery_share, 1)

        point = {
            "minute": bucket,
            "pv_power_w": round(pv_pure, 1) if pv_pure is not None else None,
            "grid_draw_power_w": round(grid_draw, 1) if grid_draw is not None else None,
            "home_from_solar_w": home_from_solar,
            "home_from_battery_w": home_from_battery,
            # Vorzeichenbehaftete Batterieleistung (negativ = Laden), damit der
            # Tagesvergleich das Laden wie der Leistungsverlauf abbilden kann.
            "battery_power_w": round(battery, 1) if battery is not None else None,
        }
        by_date.setdefault(date_str, {})[bucket] = point

    days = []
    for date_str in sorted(by_date.keys()):
        buckets = by_date[date_str]
        points = [buckets[bk] for bk in sorted(buckets.keys())]
        days.append({"date": date_str, "points": points})
    return days


def daily_kwh_totals(
    rows: list[Reading], field: str, timezone_name: str
) -> list[dict]:
    """Gruppiert Messwerte nach lokalem Kalendertag und integriert je Tag die
    Energiemenge (kWh) fuer das gegebene Leistungsfeld (Trapezregel, siehe
    integrate_kwh) - fuer Saeulendiagramme wie "Hausverbrauch pro Tag".

    Anders als bei den heutigen Tages-Statistikkarten (get_today_summary)
    wird hier NICHT auf vom Wechselrichter selbst mitgefuehrte Tageswerte
    zurueckgegriffen, sondern immer direkt aus den gespeicherten Messwerten
    integriert - das funktioniert daher auch fuer vergangene Tage und fuer
    per Logdaten-Import nachtraeglich eingespielte Altdaten (home_power_w
    ist dort im Gegensatz zu Netz-/Einspeisewerten verfuegbar).

    Rueckgabe: Liste von {"date": "YYYY-MM-DD", "kwh": float|None},
    aufsteigend nach Datum sortiert. Fuer Tage mit verdichteten Altdaten
    (siehe downsampling.py) wird eine groessere Luecken-Toleranz verwendet,
    damit der normale 1h-Abstand zwischen verdichteten Punkten nicht
    faelschlich als Datenluecke uebersprungen wird (siehe
    gap_hours_for_day)."""
    tz = ZoneInfo(timezone_name)
    by_date: dict[str, list[Reading]] = {}
    for row in rows:
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        date_str = local.strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(row)

    return [
        {
            "date": date_str,
            "kwh": integrate_kwh(day_rows, field, max_gap_hours=gap_hours_for_day(len(day_rows))),
        }
        for date_str, day_rows in sorted(by_date.items())
    ]


def daily_pv_yield_totals(rows: list[Reading], timezone_name: str) -> list[dict]:
    """PV-Ertrag (kWh) je lokalem Kalendertag, hausweit ueber alle Geraete.

    PV-Ertrag = reine PV-Erzeugung (pv1+pv2, siehe integrate_pure_pv_kwh:
    pv_power_w - battery_power_w, um die am PV3-String haengende Batterie
    herauszurechnen), je Geraet und Tag integriert und ueber die Geraete
    summiert (PV ist additiv). Bewusst NICHT der geraeteeigene Tageszaehler
    Statistic:Yield:Day, der beim Hybrid den Wechselrichter-Ausgang inkl.
    Batterieentladung misst und dadurch nachts einen "PV-Ertrag" > 0 zeigt.

    Rueckgabe: Liste von {"date": "YYYY-MM-DD", "kwh": float}, aufsteigend
    nach Datum sortiert; Tage ganz ohne Daten fehlen (statt kwh=None)."""
    tz = ZoneInfo(timezone_name)
    by_day_device: dict[tuple[str, str], list[Reading]] = {}
    for row in rows:
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        date_str = ts.astimezone(tz).strftime("%Y-%m-%d")
        by_day_device.setdefault((date_str, row.device_id), []).append(row)

    per_day: dict[str, float] = {}
    for (date_str, _device_id), day_rows in by_day_device.items():
        # Groessere Luecken-Toleranz fuer verdichtete Altdaten (siehe
        # gap_hours_for_day/downsampling.py).
        device_total = integrate_pure_pv_kwh(
            day_rows, max_gap_hours=gap_hours_for_day(len(day_rows))
        )
        if device_total is None:
            continue
        per_day[date_str] = per_day.get(date_str, 0.0) + device_total

    return [{"date": d, "kwh": round(per_day[d], 3)} for d in sorted(per_day)]


BATTERY_CHARGE = "charge"
BATTERY_DISCHARGE = "discharge"


def battery_flow_power_w(
    battery_power_w: float, direction: str, inverted: bool = False
) -> float:
    """Lade- ODER Entladeleistung (W, immer >= 0) fuer einen einzelnen
    Messpunkt, aus der vorzeichenbehafteten Batterieleistung.

    Vorzeichen-Konvention (siehe day_profile/models.Reading): positiv =
    Batterie gibt Leistung ab (Entladen), negativ = Batterie nimmt Leistung
    auf (Laden). Geraete/Firmwares mit umgekehrter Konvention werden ueber
    `inverted` (config.battery_power_inverted, wie in combine_devices)
    korrigiert.

    Fuer Energiewerte muss zusaetzlich der Nulldurchgang zwischen zwei
    Messpunkten beruecksichtigt werden, siehe daily_battery_energy_flows.
    """
    signed = -battery_power_w if inverted else battery_power_w
    if direction == BATTERY_CHARGE:
        return max(0.0, -signed)
    if direction == BATTERY_DISCHARGE:
        return max(0.0, signed)
    raise ValueError(f"Unbekannte Richtung: {direction!r}")


def daily_battery_energy_totals(
    rows: list[Reading],
    timezone_name: str,
    direction: str,
    battery_power_inverted: dict[str, bool] | None = None,
) -> list[dict]:
    """In den Speicher geladene (direction=BATTERY_CHARGE) bzw. aus ihm
    entnommene (BATTERY_DISCHARGE) Energie (kWh) je lokalem Kalendertag,
    hausweit ueber alle Geraete summiert.

    Direkt aus der gemessenen Batterieleistung integriert (Trapezregel, siehe
    integrate_kwh) - bewusst NICHT aus der Energiebilanz
    (home + feed_in - pv - grid_draw, wie sie day_profile fuer die
    Solar-/Batterie-Aufteilung des Hausverbrauchs nutzt) hergeleitet: die
    Batterieleistung ist ein direkt gemessener Wert und damit unabhaengig
    davon, ob PV-, Haus- und Netzwerte zum selben Zeitpunkt vorliegen.
    Geraete ohne Batterie (battery_power_w = None) tragen nichts bei; bei
    mehreren Geraeten mit Batterie wird je Geraet integriert und dann
    summiert (Batterieleistungen sind additiv, siehe combine_devices).

    Rueckgabe: Liste von {"date": "YYYY-MM-DD", "kwh": float}, aufsteigend
    nach Datum sortiert; Tage ganz ohne Batteriedaten fehlen (statt
    kwh=None) - analog zu daily_pv_yield_totals."""
    if direction not in (BATTERY_CHARGE, BATTERY_DISCHARGE):
        raise ValueError(f"Unbekannte Richtung: {direction!r}")
    return [
        {"date": day["date"], "kwh": day[direction]}
        for day in daily_battery_energy_flows(rows, timezone_name, battery_power_inverted)
    ]


def daily_battery_energy_flows(
    rows: list[Reading],
    timezone_name: str,
    battery_power_inverted: dict[str, bool] | None = None,
) -> list[dict]:
    """Beide Energieflussrichtungen gemeinsam integrieren.

    Zwischen Messungen gilt die lineare Interpolation der Trapezregel.
    Intervalle werden am Nulldurchgang und an lokalen Tagesgrenzen geteilt.
    Luecken ueber 30 Minuten werden wie bei integrate_kwh ausgelassen - fuer
    verdichtete Altdaten (siehe downsampling.py, ca. 1 Punkt/Stunde statt
    mehrerer tausend pro Tag) wird diese Schwelle je Geraet automatisch
    groesser gewaehlt (siehe gap_hours_for_points), sonst waere der normale
    1h-Abstand zwischen verdichteten Punkten immer eine "Luecke".
    """
    inverted_map = battery_power_inverted or {}
    tz = ZoneInfo(timezone_name)
    by_device: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows:
        if row.battery_power_w is None:
            continue
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        signed = (
            -row.battery_power_w
            if inverted_map.get(row.device_id, False) else row.battery_power_w
        )
        by_device.setdefault(row.device_id, []).append(
            (ts.astimezone(timezone.utc), signed)
        )

    totals: dict[str, dict[str, float]] = {}
    for points in by_device.values():
        points.sort()
        # Groessere Luecken-Toleranz fuer verdichtete Altdaten (siehe
        # gap_hours_for_points/downsampling.py) - anders als bei den
        # uebrigen daily_*-Funktionen ist "points" hier nicht vorab auf
        # einen einzelnen Kalendertag begrenzt, die Erkennung laeuft daher
        # ueber die Punktdichte statt eine absolute Tages-Anzahl.
        gap_limit_hours = gap_hours_for_points(points) or MAX_INTEGRATION_GAP_HOURS
        gap_limit_seconds = gap_limit_hours * 3600
        for (t0, p0), (t1, p1) in zip(points, points[1:]):
            seconds = (t1 - t0).total_seconds()
            if seconds <= 0 or seconds > gap_limit_seconds:
                continue
            boundaries = [t0, t1]
            if p0 * p1 < 0:
                zero_fraction = abs(p0) / (abs(p0) + abs(p1))
                boundaries.append(t0 + (t1 - t0) * zero_fraction)
            next_day = t0.astimezone(tz).date() + timedelta(days=1)
            midnight = datetime.combine(
                next_day, datetime.min.time(), tzinfo=tz
            ).astimezone(timezone.utc)
            if t0 < midnight < t1:
                boundaries.append(midnight)
            boundaries.sort()
            for start, end in zip(boundaries, boundaries[1:]):
                start_power = p0 + (p1 - p0) * (start - t0).total_seconds() / seconds
                end_power = p0 + (p1 - p0) * (end - t0).total_seconds() / seconds
                hours = (end - start).total_seconds() / 3600
                signed_kwh = (start_power + end_power) / 2 * hours / 1000
                day = totals.setdefault(
                    start.astimezone(tz).date().isoformat(),
                    {BATTERY_CHARGE: 0.0, BATTERY_DISCHARGE: 0.0},
                )
                direction = BATTERY_CHARGE if signed_kwh < 0 else BATTERY_DISCHARGE
                day[direction] += abs(signed_kwh)
    return [
        {"date": day, **{key: round(value, 3) for key, value in values.items()}}
        for day, values in sorted(totals.items())
    ]


def hourly_kwh_per_device(
    rows: list[Reading], field: str, timezone_name: str
) -> dict:
    """Gruppiert Messwerte nach Geraet UND lokaler Stunde und integriert je
    Stunde die Energiemenge (kWh) - fuer ein gestapeltes Saeulendiagramm, in
    dem sich z.B. die Einspeisung mehrerer Wechselrichter pro Stunde direkt
    vergleichen laesst (anders als bei den summierten Diagrammen wird hier
    NICHT device-uebergreifend addiert).

    Rueckgabe: {"devices": [{"device_id","device_name"}, ...], "buckets":
    [{"bucket": "YYYY-MM-DDTHH:00:00" (lokale Stundengrenze), "values":
    {device_id: kwh|None}}, ...]}, Buckets aufsteigend sortiert. Jeder
    Bucket enthaelt fuer JEDES bekannte Geraet einen Eintrag (None, wenn
    fuer dieses Geraet in der Stunde keine Messwerte vorliegen), damit das
    Frontend ein sauberes gestapeltes Balkendiagramm ohne Luecken bauen
    kann.
    """
    tz = ZoneInfo(timezone_name)
    groups: dict[tuple[str, str], list[Reading]] = {}
    device_names: dict[str, str] = {}
    all_buckets: set[str] = set()

    for row in rows:
        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        bucket_local = local.replace(minute=0, second=0, microsecond=0)
        bucket_key = bucket_local.strftime("%Y-%m-%dT%H:%M:%S")
        all_buckets.add(bucket_key)
        device_names[row.device_id] = row.device_name
        groups.setdefault((row.device_id, bucket_key), []).append(row)

    buckets = []
    for bucket_key in sorted(all_buckets):
        values = {}
        for device_id in device_names:
            group_rows = groups.get((device_id, bucket_key))
            values[device_id] = integrate_kwh(group_rows, field) if group_rows else None
        buckets.append({"bucket": bucket_key, "values": values})

    devices = [
        {"device_id": device_id, "device_name": name}
        for device_id, name in device_names.items()
    ]
    return {"devices": devices, "buckets": buckets}


def daily_home_source_breakdown_kwh(
    rows: list[Reading], timezone_name: str
) -> list[dict]:
    """Wie daily_kwh_totals(field="home_power_w"), aber zusaetzlich
    aufgeschluesselt danach, zu welchen Anteilen der taegliche Hausverbrauch
    aus PV (direkt), Speicher (Batterieentladung) bzw. Netzbezug gedeckt
    wurde - fuer den gestapelt eingefaerbten Balken im
    "Tagesverbrauch"-Diagramm (dieselbe Faerbung/Aufteilung wie beim
    Tagesvergleich "Verbrauch aus Solar & Batterie"/"aus dem Netz").

    Nutzt dieselbe Energiebilanz-Logik wie day_profile() (siehe dortigen
    Docstring: reine PV + Netzbezug + Batterie = Hausverbrauch + Einspeisung,
    ohne von einer bestimmten Vorzeichen-Konvention der Batterieleistung
    auszugehen - UND mit der reinen PV statt dem rohen pv_power_w, sonst
    ergibt battery_net bei einer Batterie am PV3-String immer 0, siehe dort),
    aber direkt auf den unveraenderten Messzeitpunkten (nicht auf
    15-Minuten-Mittelwerte gebucketet) und ueber den ganzen Kalendertag
    hinweg integriert statt nur gemittelt - fuer eine Energiemenge (kWh)
    statt einer Momentanleistung.

    Rueckgabe: Liste von {"date": "YYYY-MM-DD", "pv_kwh": float|None,
    "battery_kwh": float|None, "grid_kwh": float|None}, aufsteigend nach
    Datum sortiert. Die drei Werte summieren sich (bis auf Rundung) zum
    gesamten Hausverbrauch des Tages (siehe daily_kwh_totals(field=
    "home_power_w")). Fehlen fuer einen Messpunkt Haus- oder PV-Werte (z.B.
    bei importierten Altdaten ohne Netzmessung, oder wenn der
    Wechselrichter selbst voruebergehend keine Werte meldet), wird dieser
    Punkt uebersprungen. Fehlen dagegen NUR Netzbezug/Einspeisung (in der
    Praxis haeufiger, z.B. wenn die Zaehler-Abfrage kurzzeitig fehlschlaegt,
    waehrend Haus-/PV-Werte weiter vorhanden sind), wird dafuer 0
    angenommen (kein bekannter Netzbezug/Einspeisung) statt den ganzen
    Punkt zu verwerfen - sonst wuerde die Aufteilung bei lueckenhaften
    Netzwerten einen erheblichen Teil des Tages verlieren und nicht mehr
    zur tatsaechlichen Tagessumme passen.

    Hausverbrauch/Netzbezug koennen physikalisch nicht negativ sein (siehe
    combine_devices() fuer die Herleitung) - ein an dieser Stelle dennoch
    negativer Rohwert wird auf 0 begrenzt, damit weder eine einzelne Quelle
    noch die Summe aller drei Werte je negativ werden.
    """
    tz = ZoneInfo(timezone_name)
    by_date: dict[str, list[tuple[datetime, float, float, float]]] = {}

    for row in rows:
        home = row.home_power_w
        pv = row.pv_power_w
        if home is None or pv is None:
            continue
        # Netzbezug/Einspeisung fehlen in der Praxis oefter als Haus-/
        # PV-Werte (z.B. wenn die Zaehler-Abfrage kurz fehlschlaegt) - dann
        # lieber 0 annehmen (kein bekannter Netzbezug/keine bekannte
        # Einspeisung) statt den ganzen Messpunkt zu verwerfen, siehe
        # Docstring.
        grid_draw = row.grid_draw_power_w if row.grid_draw_power_w is not None else 0.0
        feed_in = row.feed_in_power_w if row.feed_in_power_w is not None else 0.0
        # Hausverbrauch/Netzbezug koennen physikalisch nicht negativ sein
        # (siehe combine_devices()) - auf 0 begrenzen statt eine
        # irrefuehrende negative Saeule zu zeigen.
        home = max(0.0, home)
        grid_draw = max(0.0, grid_draw)
        # Reine PV (Batterie am PV3-String herausgerechnet) - siehe
        # pure_pv_power_w sowie den Docstring oben, warum battery_net
        # NICHT mit dem rohen pv gerechnet werden darf.
        pv_pure = pure_pv_power_w(pv, row.battery_power_w)

        # Gleiche Herleitung wie in day_profile(): Anteil direkt aus dem Netz
        # kann Hausverbrauch nicht uebersteigen, Rest wird zwischen PV und
        # Batterie aufgeteilt (Batterie nur, wenn sie gerade tatsaechlich
        # per Energiebilanz Leistung abgibt - battery_net > 0).
        remaining_home = max(0.0, home - grid_draw)
        battery_net = home + feed_in - pv_pure - grid_draw
        battery_share = min(remaining_home, battery_net) if battery_net > 0 else 0.0
        home_from_battery = battery_share
        home_from_pv = remaining_home - battery_share
        home_from_grid = home - remaining_home

        ts = row.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        date_str = local.strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(
            (row.timestamp, home_from_pv, home_from_battery, home_from_grid)
        )

    def _integrate(series: list[tuple[datetime, float]], gap: float | None) -> float | None:
        objs = [SimpleNamespace(timestamp=ts, value=v) for ts, v in series]
        return integrate_kwh(objs, "value", max_gap_hours=gap)

    result = []
    for date_str in sorted(by_date.keys()):
        entries = by_date[date_str]
        # Groessere Luecken-Toleranz fuer verdichtete Altdaten (siehe
        # gap_hours_for_day/downsampling.py) - alle drei Anteile stammen aus
        # denselben Messpunkten dieses Tages, daher dieselbe Punktzahl.
        gap = gap_hours_for_day(len(entries))
        result.append(
            {
                "date": date_str,
                "pv_kwh": _integrate([(ts, pv) for ts, pv, _bat, _grid in entries], gap),
                "battery_kwh": _integrate([(ts, bat) for ts, _pv, bat, _grid in entries], gap),
                "grid_kwh": _integrate([(ts, grid) for ts, _pv, _bat, grid in entries], gap),
            }
        )
    return result
