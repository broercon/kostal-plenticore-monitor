"""FastAPI-Anwendung: dient die REST-API und das statische Frontend aus."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from typing import Literal

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import auth
from .aggregation import (
    aggregate_per_device,
    combine_devices,
    combine_latest_readings,
    daily_home_source_breakdown_kwh,
    daily_kwh_totals,
    day_profile,
    hourly_kwh_per_device,
    integrate_kwh,
)
from .auto_import import get_import_status, run_auto_import_for_all_devices, trigger_manual_import
from .config import settings
from .daily_report import (
    daily_report_scheduler,
    generate_and_send_daily_report,
    get_daily_report_status,
)
from .daily_report_config import InvalidReportTime, get_config as get_daily_report_config, update_config as update_daily_report_config
from .daily_summary import (
    build_daily_home_breakdown,
    build_daily_summaries,
    build_feed_in_summary,
    build_pv_yield_summary,
)
from .database import SessionLocal, init_db
from .models import Reading, User
from .poller import poller
from .schemas import (
    AdminResetPasswordIn,
    AdminResetPasswordOut,
    AdminUserOut,
    ChangePasswordIn,
    ChangePasswordOut,
    DailyHomeBreakdownOut,
    DailyReportConfigIn,
    DailyReportConfigOut,
    DailyReportStatusOut,
    DailyReportTriggerOut,
    DailyTotalsOut,
    DayProfileOut,
    DeviceOut,
    FeedInPeriod,
    FeedInSummaryOut,
    PvYieldSummaryOut,
    HistoryPoint,
    HourlyPerDeviceOut,
    ImportStatusOut,
    ImportTriggerOut,
    LoginIn,
    MeOut,
    ReadingOut,
    SummaryOut,
)
from .timeutil import local_midnight_utc
from zoneinfo import ZoneInfo

# Metrik-Name (API-Parameter) -> Feld in Reading, fuer /api/readings/daily-totals.
DAILY_TOTAL_FIELDS = {
    "home": "home_power_w",
    "pv": "pv_power_w",
    "grid_draw": "grid_draw_power_w",
    "feed_in": "feed_in_power_w",
}

# Spezielle device_id fuer die vom Backend bereits korrekt zusammengefasste
# "Alle (Summe)"-Ansicht bei mehreren Wechselrichtern (siehe
# aggregation.combine_devices/combine_latest_readings) - kein echtes Geraet.
COMBINED_DEVICE_ID = "_all_"


def _has_grid_meter_map() -> dict[str, bool]:
    return {cfg.id: cfg.has_grid_meter for cfg in settings.inverters}


def _battery_inverted_map() -> dict[str, bool]:
    return {cfg.id: cfg.battery_power_inverted for cfg in settings.inverters}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    auth.seed_default_users()
    poller.start()
    daily_report_scheduler.start()
    auto_import_task = asyncio.create_task(run_auto_import_for_all_devices())
    yield
    auto_import_task.cancel()
    await poller.stop()
    await daily_report_scheduler.stop()


app = FastAPI(title="Kostal Plenticore Monitor", lifespan=lifespan)


@app.middleware("http")
async def no_cache_headers(request, call_next):
    """Verhindert, dass Browser (v.a. Safari) API-Antworten oder die
    Frontend-Dateien zwischenspeichern und dadurch veraltete/leere Daten
    anzeigen, solange die App noch aktiv weiterentwickelt/aktualisiert wird."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    else:
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.post("/api/auth/login", response_model=MeOut)
def post_login(payload: LoginIn, response: Response) -> MeOut:
    user = auth.login(payload.username, payload.password, response)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Benutzername oder Passwort falsch."
        )
    return MeOut(
        id=user.id,
        username=user.username,
        role=user.role,
        must_change_password=user.must_change_password,
    )


@app.post("/api/auth/logout")
def post_logout(response: Response, kpm_session: str | None = Cookie(default=None)) -> dict:
    auth.logout(kpm_session, response)
    return {"success": True}


@app.get("/api/auth/me", response_model=MeOut)
def get_me(user: User = Depends(auth.get_current_user)) -> MeOut:
    return MeOut(
        id=user.id,
        username=user.username,
        role=user.role,
        must_change_password=user.must_change_password,
    )


@app.post("/api/auth/change-password", response_model=ChangePasswordOut)
def post_change_password(
    payload: ChangePasswordIn, user: User = Depends(auth.get_current_user)
) -> ChangePasswordOut:
    ok = auth.change_own_password(user.id, payload.current_password, payload.new_password)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Aktuelles Passwort ist falsch."
        )
    return ChangePasswordOut(success=True, message="Passwort geändert.")


@app.get("/api/admin/users", response_model=list[AdminUserOut])
def get_admin_users(_admin: User = Depends(auth.require_admin)) -> list[AdminUserOut]:
    return [
        AdminUserOut(
            id=u.id, username=u.username, role=u.role, must_change_password=u.must_change_password
        )
        for u in auth.list_users()
    ]


@app.post("/api/admin/users/{user_id}/reset-password", response_model=AdminResetPasswordOut)
def post_admin_reset_password(
    user_id: int, payload: AdminResetPasswordIn, _admin: User = Depends(auth.require_admin)
) -> AdminResetPasswordOut:
    result = auth.admin_reset_password(user_id, payload.new_password)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nutzer nicht gefunden.")
    user, new_password = result
    return AdminResetPasswordOut(
        username=user.username,
        new_password=new_password,
        message=(
            f"Neues Passwort für {user.username} gesetzt. Muss beim naechsten Login "
            "geaendert werden."
        ),
    )


@app.post("/api/admin/import-history", response_model=ImportTriggerOut)
async def post_trigger_import_history(
    _user: User = Depends(auth.get_current_user),
) -> ImportTriggerOut:
    """Stoesst den Logdaten-Abgleich sofort an, statt nur beim naechsten
    Container-Start - z.B. um nach einer Konfigurationsaenderung (etwa
    AUTO_IMPORT_DAYS) direkt zu pruefen, ob der Import durchlaeuft, ohne
    extra neu starten zu muessen. Laeuft im Hintergrund; Fortschritt/Ergebnis
    über GET /api/admin/import-history/status abrufbar."""
    started = trigger_manual_import()
    if started:
        return ImportTriggerOut(started=True, message="Logdaten-Abgleich gestartet.")
    return ImportTriggerOut(
        started=False, message="Läuft bereits - bitte Status abwarten."
    )


@app.get("/api/admin/import-history/status", response_model=ImportStatusOut)
def get_import_history_status(_user: User = Depends(auth.get_current_user)) -> ImportStatusOut:
    return ImportStatusOut(**get_import_status())


@app.get("/api/devices", response_model=list[DeviceOut])
def get_devices(_user: User = Depends(auth.get_current_user)) -> list[DeviceOut]:
    return [
        DeviceOut(id=cfg.id, name=cfg.name, host=cfg.host) for cfg in settings.inverters
    ]


@app.get("/api/readings/latest", response_model=list[ReadingOut])
def get_latest(_user: User = Depends(auth.get_current_user)) -> list[ReadingOut]:
    """Aktuellste Messwerte je Geraet. Bei mehreren konfigurierten
    Wechselrichtern wird zusaetzlich ein synthetischer Eintrag mit
    device_id "_all_" angehaengt, der Hausverbrauch/Netzbezug/Einspeisung
    korrekt ueber die Energiebilanz berechnet (statt die - bei mehreren
    Geraeten am selben Hausanschluss potenziell falschen - Home_P-Werte der
    einzelnen Geraete naiv zu summieren). Siehe aggregation.combine_devices
    fuer die Herleitung. Das Frontend nutzt diesen Eintrag fuer die
    "Alle (Summe)"-Ansicht, wenn vorhanden."""
    readings = list(poller.latest.values())
    result = [ReadingOut(**reading) for reading in readings]

    if len(settings.inverters) > 1 and readings:
        combined = combine_latest_readings(
            readings, _has_grid_meter_map(), _battery_inverted_map()
        )
        if combined is not None:
            newest_ts = max(r["timestamp"] for r in readings)
            result.append(
                ReadingOut(
                    device_id=COMBINED_DEVICE_ID,
                    device_name="Alle (Summe)",
                    timestamp=newest_ts,
                    **combined,
                )
            )
    return result


@app.get("/api/readings/history", response_model=list[HistoryPoint])
def get_history(
    device_id: str | None = Query(default=None, description="Leer = alle Geraete summiert"),
    hours: float = Query(default=24, ge=0.1, le=24 * 90),
    bucket_minutes: float = Query(default=5, ge=1, le=1440),
    _user: User = Depends(auth.get_current_user),
) -> list[HistoryPoint]:
    """Zeitreihe fuer das Hauptdiagramm.

    Hausverbrauch/Einspeisung/Netzbezug sind hausweite Groessen - bei
    mehreren konfigurierten Wechselrichtern werden sie IMMER aus der ueber
    alle Geraete korrigierten Energiebilanz genommen (siehe
    aggregation.combine_devices), auch wenn oben ein einzelnes Geraet
    ausgewaehlt ist. Grund: der eigene Home_P-Wert eines einzelnen
    Wechselrichters kann bei einem zweiten, unbeachteten Wechselrichter am
    selben Hausanschluss stark falsch/negativ sein (siehe README). Nur
    PV-Leistung und Batterieleistung bleiben pro ausgewaehltem Geraet.
    """
    if hours <= 24:
        # Feste lokale Tagesgrenze statt rollierendem 24h-Fenster: sonst
        # verschiebt sich der Start staendig mit der Uhrzeit (z.B. "seit
        # gestern 20 Uhr" statt "seit Mitternacht"), was den Tag im
        # Diagramm von Aufruf zu Aufruf unterschiedlich aussehen laesst.
        since = local_midnight_utc()
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket_seconds = int(bucket_minutes * 60)
    multi = len(settings.inverters) > 1

    session = SessionLocal()
    try:
        stmt = select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)
        if device_id and not multi:
            stmt = stmt.where(Reading.device_id == device_id)
        rows = list(session.scalars(stmt))
    finally:
        session.close()

    per_device = aggregate_per_device(rows, bucket_seconds)

    if device_id and not multi:
        buckets = per_device.get(device_id, {})
    else:
        combined_all = combine_devices(per_device, _has_grid_meter_map(), _battery_inverted_map())
        if device_id:
            own = per_device.get(device_id, {})
            buckets = {}
            for bk in set(combined_all) | set(own):
                c = combined_all.get(bk, {})
                o = own.get(bk, {})
                buckets[bk] = {
                    "home_power_w": c.get("home_power_w"),
                    "feed_in_power_w": c.get("feed_in_power_w"),
                    "grid_draw_power_w": c.get("grid_draw_power_w"),
                    "pv_power_w": o.get("pv_power_w"),
                    "battery_power_w": o.get("battery_power_w"),
                }
        else:
            buckets = combined_all

    points = [
        HistoryPoint(
            timestamp=datetime.fromtimestamp(bk, tz=timezone.utc),
            **values,
        )
        for bk, values in sorted(buckets.items())
    ]
    return points


@app.get("/api/readings/today-summary", response_model=list[SummaryOut])
def get_today_summary(_user: User = Depends(auth.get_current_user)) -> list[SummaryOut]:
    """Tagessummen je Wechselrichter.

    Bevorzugt die vom Geraet selbst gelieferten Tages-Statistikwerte. Manche
    Geraete/Logins liefern diese aber nicht (z.B. eingeschraenkter
    Nutzer-Login ohne Statistik-Modul, oder der virtuelle Einspeise-Wert
    braucht eigentlich eine Batterie). In diesem Fall werden die fehlenden
    Werte aus den seit lokaler Mitternacht gespeicherten Messwerten
    hochgerechnet (Integration der Leistungswerte).

    Bei mehreren konfigurierten Wechselrichtern wird zusaetzlich ein
    synthetischer Eintrag mit device_id "_all_" angehaengt: dessen
    Hausverbrauch wird NICHT durch Summieren der einzelnen (bei mehreren
    Geraeten am selben Hausanschluss potenziell falschen, siehe
    aggregation.combine_devices) Tages-Statistikwerte berechnet, sondern
    durch Integration der ueber die Energiebilanz korrigierten
    Leistungswerte seit lokaler Mitternacht.

    Die eigentliche Berechnung steckt in daily_summary.build_daily_summaries()
    - sie wird auch vom taeglichen Mail-Report (siehe daily_report.py)
    verwendet, damit die Logik nur an einer Stelle gepflegt werden muss.
    """
    return build_daily_summaries()


@app.get("/api/admin/daily-report/status", response_model=DailyReportStatusOut)
def get_daily_report_status_endpoint(
    _user: User = Depends(auth.get_current_user),
) -> DailyReportStatusOut:
    """Stand des taeglichen Mail-Reports: ob er aktiv ist (Konfiguration
    vollstaendig), die eingestellte Uhrzeit/Empfaenger, sowie Zeitpunkt und
    Ausgang (Erfolg/Fehler) des letzten Versands - ohne dafuer die
    Container-Logs durchsuchen zu muessen (analog zu
    /api/admin/import-history/status). Fuer alle angemeldeten Nutzer
    sichtbar (nicht nur admin) - die Konfiguration selbst
    (GET/PUT .../config) ist dagegen admin-only."""
    cfg = get_daily_report_config()
    status_data = get_daily_report_status()
    return DailyReportStatusOut(
        enabled=bool(cfg["enabled"] and cfg["recipients"] and cfg["mail_service_url"]),
        scheduled_time=cfg["report_time"],
        recipients=cfg["recipients"],
        last_sent_at=status_data["last_sent_at"],
        last_status=status_data["last_status"],
        last_message=status_data["last_message"],
    )


def _daily_report_config_out(cfg: dict) -> DailyReportConfigOut:
    return DailyReportConfigOut(
        enabled=cfg["enabled"],
        report_time=cfg["report_time"],
        recipients=cfg["recipients"],
        mail_service_url=cfg["mail_service_url"],
        mail_service_api_key_set=bool(cfg["mail_service_api_key"]),
        mail_service_from_name=cfg["mail_service_from_name"],
    )


@app.get("/api/admin/daily-report/config", response_model=DailyReportConfigOut)
def get_daily_report_config_endpoint(
    _admin: User = Depends(auth.require_admin),
) -> DailyReportConfigOut:
    """Aktuelle Konfiguration des taeglichen Mail-Reports (nur Rolle admin -
    enthaelt u.a. die Empfaenger-Adressen). Der Mail-Service-API-Key selbst
    wird nie zurueckgegeben, nur ob einer hinterlegt ist."""
    return _daily_report_config_out(get_daily_report_config())


@app.put("/api/admin/daily-report/config", response_model=DailyReportConfigOut)
def put_daily_report_config_endpoint(
    payload: DailyReportConfigIn, _admin: User = Depends(auth.require_admin)
) -> DailyReportConfigOut:
    """Speichert die Konfiguration des taeglichen Mail-Reports (nur Rolle
    admin) - komplett ueber die Admin-Oberflaeche editierbar (aktiv/inaktiv,
    Uhrzeit, Empfaenger, Mail-Service-URL/API-Key/Absendername), damit dafuer
    kein Zugriff auf Server/Umgebungsvariablen mehr noetig ist. Wirkt ohne
    Container-Neustart (siehe daily_report.DailyReportScheduler, das die
    Konfiguration bei jedem Zyklus neu liest)."""
    try:
        cfg = update_daily_report_config(
            enabled=payload.enabled,
            report_time=payload.report_time,
            recipients=payload.recipients,
            mail_service_url=payload.mail_service_url,
            mail_service_api_key=payload.mail_service_api_key,
            mail_service_from_name=payload.mail_service_from_name,
        )
    except InvalidReportTime as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _daily_report_config_out(cfg)


@app.post("/api/admin/daily-report/trigger", response_model=DailyReportTriggerOut)
async def post_trigger_daily_report(
    _admin: User = Depends(auth.require_admin),
) -> DailyReportTriggerOut:
    """Verschickt den taeglichen Zusammenfassungs-Report sofort (z.B. um die
    gerade gespeicherte Mail-Konfiguration zu testen, ohne bis zur
    eingestellten Uhrzeit zu warten, und unabhaengig vom "Aktiv"-Schalter).
    Laeuft synchron zum Request - ein einzelner Mailversand dauert
    typischerweise deutlich unter einer Sekunde, ein eigener
    Hintergrund-Task (wie bei /api/admin/import-history) ist dafuer nicht
    noetig."""
    result = await generate_and_send_daily_report()
    return DailyReportTriggerOut(started=result["sent"], message=result["message"])


def _merge_day_profile_own_pv(combined_days: list[dict], device_days: list[dict]) -> list[dict]:
    """Ersetzt in combined_days (hausweite Energiebilanz ueber alle Geraete)
    die pv_power_w-Werte durch die aus device_days (nur das ausgewaehlte
    Einzelgeraet) - Netzbezug und Solar-/Batterie-Aufteilung bleiben hausweit
    korrekt, die PV-Kurve zeigt dann aber gezielt nur die Erzeugung des
    ausgewaehlten Wechselrichters (z.B. zum Tagesvergleich seiner eigenen
    PV-Strings)."""
    own_pv_by_key = {
        (day["date"], point["minute"]): point["pv_power_w"]
        for day in device_days
        for point in day["points"]
    }
    merged = []
    for day in combined_days:
        new_points = []
        for point in day["points"]:
            p = dict(point)
            key = (day["date"], point["minute"])
            if key in own_pv_by_key:
                p["pv_power_w"] = own_pv_by_key[key]
            new_points.append(p)
        merged.append({"date": day["date"], "points": new_points})
    return merged


@app.get("/api/readings/feed-in-summary", response_model=FeedInSummaryOut)
def get_feed_in_summary(_user: User = Depends(auth.get_current_user)) -> FeedInSummaryOut:
    """Gesamte Einspeisung (kWh) fuer mehrere Zeitraeume: heute, gestern,
    vorgestern, diese/letzte Woche (Mo-So), dieser/letzter Kalendermonat
    sowie dieses/letztes Kalenderjahr.

    Einspeisung ist eine hausweite Groesse - bei mehreren Wechselrichtern
    wird sie aus der ueber alle Geraete korrigierten Energiebilanz integriert
    (wie /daily-totals mit metric=feed_in), nicht naiv je Geraet summiert.
    Rein per Logdaten-Import eingespielte Altdaten enthalten in der Regel
    keine Einspeisung (KSEM-Limitation, siehe README) - solche Tage tragen
    dann nichts bei; ein Zeitraum ohne jegliche Daten liefert kwh=None.

    Die eigentliche Berechnung steckt in daily_summary.build_feed_in_summary()
    - sie wird auch vom taeglichen Mail-Report (siehe daily_report.py)
    verwendet, damit die Logik nur an einer Stelle gepflegt werden muss.
    """
    return FeedInSummaryOut(periods=build_feed_in_summary())


@app.get("/api/readings/pv-yield-summary", response_model=PvYieldSummaryOut)
def get_pv_yield_summary(_user: User = Depends(auth.get_current_user)) -> PvYieldSummaryOut:
    """Gesamter PV-Ertrag (kWh) fuer mehrere Zeitraeume: heute, gestern,
    vorgestern, diese/letzte Woche (Mo-So), dieser/letzter Kalendermonat
    sowie dieses/letztes Kalenderjahr.

    PV-Ertrag ist die ueber alle Wechselrichter summierte, integrierte
    PV-Leistung (pv_power_w). Anders als die Einspeisung ist der PV-Ertrag
    auch in per Logdaten-Import eingespielten Altdaten vorhanden, sodass sich
    auch vergangene Monate/Jahre fuellen. Ein Zeitraum ganz ohne Daten
    liefert kwh=None. Berechnung: daily_summary.build_pv_yield_summary().
    """
    return PvYieldSummaryOut(periods=build_pv_yield_summary())


@app.get("/api/readings/day-profile", response_model=DayProfileOut)
def get_day_profile(
    device_id: str | None = Query(default=None, description="Leer = alle Geraete summiert"),
    days: int = Query(default=7, ge=1, le=30, description="Anzahl Tage rueckwirkend inkl. heute"),
    bucket_minutes: int = Query(default=15, ge=5, le=60),
    _user: User = Depends(auth.get_current_user),
) -> DayProfileOut:
    """Liefert je Kalendertag (in TIMEZONE) eine Zeitreihe ueber 00:00-24:00
    Uhr, damit sich mehrere Tage im Diagramm ueberlagern und direkt
    vergleichen lassen (z.B. PV-Erzeugung heute vs. gestern vs. letzte
    Woche). Enthaelt zusaetzlich eine Aufteilung des Hausverbrauchs in
    Solar- und Batterie-Anteil (siehe aggregation.day_profile fuer die
    Herleitung).

    Netzbezug sowie die Solar-/Batterie-Aufteilung sind hausweite Groessen -
    bei mehreren Wechselrichtern werden sie IMMER aus der ueber alle Geraete
    korrigierten Energiebilanz berechnet, auch wenn oben ein einzelnes
    Geraet ausgewaehlt ist (dessen eigener Home_P/Grid_P-Wert kann sonst
    stark falsch sein, siehe README). Die PV-Kurve zeigt bei ausgewaehltem
    Einzelgeraet trotzdem dessen eigene Erzeugung (siehe
    _merge_day_profile_own_pv)."""
    since = local_midnight_utc() - timedelta(days=days - 1)
    multi = len(settings.inverters) > 1

    session = SessionLocal()
    try:
        stmt = select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)
        if device_id and not multi:
            stmt = stmt.where(Reading.device_id == device_id)
        rows = list(session.scalars(stmt))
    finally:
        session.close()

    if multi:
        # Fuer die hausweiten Werte muessen Leistungswerte erst pro Geraet
        # UND Zeitpunkt summiert werden, bevor sie nach Tag/Uhrzeit gebucketet
        # werden - sonst wuerden Messwerte verschiedener Geraete zu
        # unterschiedlichen Zeitpunkten faelschlich einzeln gemittelt statt
        # zeitgleich addiert. Wir nutzen dafuer die bestehende
        # Sekunden-Bucket-Aggregation mit einem feinen Bucket (= Polling-
        # Intervall) und bauen daraus synthetische Reading-aehnliche Objekte.
        per_device = aggregate_per_device(rows, bucket_seconds=60)
        combined = combine_devices(per_device, _has_grid_meter_map(), _battery_inverted_map())
        synthetic_rows = [
            Reading(
                device_id="_combined_",
                device_name="_combined_",
                timestamp=datetime.fromtimestamp(bk, tz=timezone.utc),
                **values,
            )
            for bk, values in combined.items()
        ]
        combined_days = day_profile(synthetic_rows, bucket_minutes, settings.timezone_name)
        if device_id:
            device_rows = [r for r in rows if r.device_id == device_id]
            device_days = day_profile(device_rows, bucket_minutes, settings.timezone_name)
            days_data = _merge_day_profile_own_pv(combined_days, device_days)
        else:
            days_data = combined_days
    else:
        days_data = day_profile(rows, bucket_minutes, settings.timezone_name)

    return DayProfileOut(bucket_minutes=bucket_minutes, days=days_data)


@app.get("/api/readings/daily-totals", response_model=DailyTotalsOut)
def get_daily_totals(
    device_id: str | None = Query(default=None, description="Leer = alle Geraete summiert"),
    metric: Literal["home", "pv", "grid_draw", "feed_in"] = Query(default="home"),
    days: int = Query(default=30, ge=1, le=400, description="Anzahl Tage rueckwirkend inkl. heute"),
    _user: User = Depends(auth.get_current_user),
) -> DailyTotalsOut:
    """Liefert je Kalendertag die integrierte Energiemenge (kWh) fuer ein
    Saeulendiagramm (z.B. Hausverbrauch pro Tag). Anders als die
    "heute"-Kachel wird hier immer direkt aus den Messwerten integriert,
    nicht aus vom Geraet gemeldeten Tageswerten - funktioniert daher auch
    fuer vergangene, per Logdaten-Import nachtraeglich eingespielte Tage
    (jedenfalls fuer home/pv - Netzwerte gibt es dort nicht, siehe README).

    Hausverbrauch, Netzbezug und Einspeisung sind hausweite Groessen - bei
    mehreren Wechselrichtern werden sie IMMER aus der ueber alle Geraete
    korrigierten Energiebilanz integriert, auch wenn oben ein einzelnes
    Geraet ausgewaehlt ist (dessen eigener Rohwert kann sonst stark falsch
    sein, siehe README). Nur die PV-Erzeugung bleibt bei ausgewaehltem
    Einzelgeraet dessen eigene."""
    field = DAILY_TOTAL_FIELDS[metric]
    since = local_midnight_utc() - timedelta(days=days - 1)
    multi = len(settings.inverters) > 1
    house_wide_multi = multi and metric != "pv"

    session = SessionLocal()
    try:
        stmt = select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)
        if device_id and not house_wide_multi:
            stmt = stmt.where(Reading.device_id == device_id)
        rows = list(session.scalars(stmt))
    finally:
        session.close()

    if multi and (not device_id or house_wide_multi):
        # Wie bei /day-profile: Leistungswerte muessen erst pro Geraet UND
        # Zeitpunkt summiert werden, bevor pro Tag integriert wird - sonst
        # wuerden Geraete mit leicht versetzten Polling-Zeitpunkten
        # fehlerhaft einzeln integriert statt zeitgleich addiert. Bei einer
        # hausweiten Metrik (home/grid_draw/feed_in) gilt das unabhaengig
        # davon, ob oben ein einzelnes Geraet ausgewaehlt ist - device_id
        # wird dafuer bewusst ignoriert.
        per_device = aggregate_per_device(rows, bucket_seconds=60)
        combined = combine_devices(per_device, _has_grid_meter_map(), _battery_inverted_map())
        rows = [
            Reading(
                device_id="_combined_",
                device_name="_combined_",
                timestamp=datetime.fromtimestamp(bk, tz=timezone.utc),
                **values,
            )
            for bk, values in combined.items()
        ]

    days_data = daily_kwh_totals(rows, field, settings.timezone_name)
    return DailyTotalsOut(metric=metric, days=days_data)


@app.get("/api/readings/daily-home-breakdown", response_model=DailyHomeBreakdownOut)
def get_daily_home_breakdown(
    days: int = Query(default=30, ge=1, le=400, description="Anzahl Tage rueckwirkend inkl. heute"),
    _user: User = Depends(auth.get_current_user),
) -> DailyHomeBreakdownOut:
    """Wie /api/readings/daily-totals (metric=home), aber zusaetzlich
    aufgeschluesselt danach, zu welchen Anteilen der taegliche
    Hausverbrauch aus PV, Speicher (Batterie) bzw. Netzbezug gedeckt wurde -
    fuer den gestapelt eingefaerbten Balken im "Tagesverbrauch"-Diagramm
    (siehe aggregation.daily_home_source_breakdown_kwh fuer die Herleitung).

    Hausverbrauch ist eine hausweite Groesse und laesst sich nicht sinnvoll
    einem einzelnen Wechselrichter zuordnen - daher kein device_id-Parameter,
    bei mehreren konfigurierten Geraeten wird immer automatisch die ueber
    die Energiebilanz korrigierte Gesamt-Zeitreihe verwendet.

    Die eigentliche Berechnung steckt in
    daily_summary.build_daily_home_breakdown() - der taegliche Mail-Report
    (siehe daily_report.py) nutzt sie fuer den heutigen Tag mit."""
    return DailyHomeBreakdownOut(days=build_daily_home_breakdown(days=days))


@app.get("/api/readings/hourly-per-device", response_model=HourlyPerDeviceOut)
def get_hourly_per_device(
    metric: Literal["feed_in", "pv", "home", "grid_draw"] = Query(default="feed_in"),
    days: int = Query(default=1, ge=1, le=30, description="Anzahl Tage rueckwirkend inkl. heute"),
    _user: User = Depends(auth.get_current_user),
) -> HourlyPerDeviceOut:
    """Liefert stuendliche kWh-Summen JE Wechselrichter (nicht summiert) -
    fuer ein gestapeltes Saeulendiagramm, in dem sich z.B. die Einspeisung
    mehrerer Wechselrichter pro Stunde direkt farblich vergleichen laesst.
    Bei nur einem konfigurierten Geraet zeigt es entsprechend nur eine
    Farbe/Reihe."""
    field = DAILY_TOTAL_FIELDS[metric]
    since = local_midnight_utc() - timedelta(days=days - 1)

    session = SessionLocal()
    try:
        rows = list(
            session.scalars(
                select(Reading).where(Reading.timestamp >= since).order_by(Reading.timestamp)
            )
        )
    finally:
        session.close()

    result = hourly_kwh_per_device(rows, field, settings.timezone_name)
    return HourlyPerDeviceOut(metric=metric, **result)


# Statisches Frontend (index.html, app.js, style.css) unter "/" ausliefern.
if settings.frontend_dir.exists():
    app.mount(
        "/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend"
    )
