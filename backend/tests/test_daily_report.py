"""Tests fuer den taeglichen Zusammenfassungs-Report per Mail
(app/daily_report.py, app/daily_report_config.py, app/daily_summary.py,
app/report_mailer.py).

Deckt ab: Berechnung des naechsten Sendezeitpunkts (next_run_at), den
"aktiv/erreichbar"-Status je Wechselrichter (device_online_map), das
Text-Format der Mail (build_report_text), dass ein fehlgeschlagener
Mailversand abgefangen und in _state vermerkt wird statt den Aufrufer
crashen zu lassen (generate_and_send_daily_report), die komplett ueber die
Datenbank editierbare Konfiguration (daily_report_config: Persistenz,
Env-Var-Fallback, API-Key wird nie im Klartext preisgegeben) sowie die
neuen Admin-Endpunkte End-to-End ueber den FastAPI-TestClient.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import daily_report
from app.daily_report_config import InvalidReportTime, get_config, parse_report_time, update_config
from app.daily_summary import (
    build_daily_home_breakdown,
    build_feed_in_summary,
    device_battery_snapshot,
    device_online_map,
)
from app.report_mailer import ReportMailError
from app.schemas import DailyHomeBreakdownDay, FeedInPeriod, SummaryOut

from .conftest import make_user

TZ = ZoneInfo("Europe/Berlin")


# --- next_run_at ---------------------------------------------------------


def test_next_run_at_returns_today_if_target_time_still_ahead():
    now = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)  # 12:00 Berlin (Sommerzeit)
    target = daily_report.next_run_at(now, hour=19, minute=0, tz_name="Europe/Berlin")
    local_target = target.astimezone(TZ)
    assert local_target.date() == datetime(2026, 7, 14).date()
    assert (local_target.hour, local_target.minute) == (19, 0)


def test_next_run_at_rolls_over_to_tomorrow_if_target_time_already_passed():
    now = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)  # 22:00 Berlin, laengst nach 19:00
    target = daily_report.next_run_at(now, hour=19, minute=0, tz_name="Europe/Berlin")
    local_target = target.astimezone(TZ)
    assert local_target.date() == datetime(2026, 7, 15).date()
    assert (local_target.hour, local_target.minute) == (19, 0)


def test_next_run_at_rolls_over_when_exactly_at_target_time():
    # Exakt 19:00 gilt als "schon erreicht" -> naechster Lauf ist erst morgen,
    # damit ein bereits laufender/verschickter Report nicht sofort nochmal
    # ausgeloest wird.
    local_now = datetime(2026, 7, 14, 19, 0, tzinfo=TZ)
    now = local_now.astimezone(timezone.utc)
    target = daily_report.next_run_at(now, hour=19, minute=0, tz_name="Europe/Berlin")
    assert target.astimezone(TZ).date() == datetime(2026, 7, 15).date()


# --- parse_report_time -----------------------------------------------------


def test_parse_report_time_accepts_valid_format():
    assert parse_report_time("19:00") == (19, 0)
    assert parse_report_time("07:05") == (7, 5)


def test_parse_report_time_rejects_invalid_format():
    for bad in ["19", "19:60", "25:00", "abc", "19:00:00"]:
        try:
            parse_report_time(bad)
            assert False, f"{bad!r} haette InvalidReportTime auslösen muessen"
        except InvalidReportTime:
            pass


# --- device_online_map ----------------------------------------------------


def test_device_online_map_marks_recent_reading_as_active(monkeypatch):
    import app.daily_summary as daily_summary_module

    class _Cfg:
        id = "wr1"
        name = "WR 1"

    monkeypatch.setattr(daily_summary_module.settings, "inverters", [_Cfg()])
    monkeypatch.setattr(daily_summary_module.settings, "poll_interval_seconds", 15)

    now = datetime.now(timezone.utc)
    daily_summary_module.poller.latest.clear()
    daily_summary_module.poller.latest["wr1"] = {"timestamp": now - timedelta(seconds=10)}

    result = device_online_map(now=now)
    assert result == {"wr1": True}


def test_device_online_map_marks_stale_reading_as_inactive(monkeypatch):
    import app.daily_summary as daily_summary_module

    class _Cfg:
        id = "wr1"
        name = "WR 1"

    monkeypatch.setattr(daily_summary_module.settings, "inverters", [_Cfg()])
    monkeypatch.setattr(daily_summary_module.settings, "poll_interval_seconds", 15)

    now = datetime.now(timezone.utc)
    daily_summary_module.poller.latest.clear()
    daily_summary_module.poller.latest["wr1"] = {"timestamp": now - timedelta(hours=2)}

    result = device_online_map(now=now)
    assert result == {"wr1": False}


def test_device_online_map_marks_never_seen_device_as_inactive(monkeypatch):
    import app.daily_summary as daily_summary_module

    class _Cfg:
        id = "wr-neu"
        name = "Noch nie erreicht"

    monkeypatch.setattr(daily_summary_module.settings, "inverters", [_Cfg()])
    daily_summary_module.poller.latest.clear()

    result = device_online_map()
    assert result == {"wr-neu": False}


# --- build_report_text -----------------------------------------------------


def test_build_report_html_includes_all_sections():
    now = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)
    summaries = [
        SummaryOut(
            device_id="wr1",
            device_name="Dach Sued",
            yield_day_kwh=12.345,
            home_consumption_day_kwh=5.0,
            energy_grid_day_kwh=1.0,
        ),
        SummaryOut(
            device_id="wr2",
            device_name="Garage",
            yield_day_kwh=None,
            home_consumption_day_kwh=None,
            energy_grid_day_kwh=None,
        ),
        SummaryOut(
            device_id="_all_",
            device_name="Alle (Summe)",
            yield_day_kwh=15.345,
            home_consumption_day_kwh=8.0,
            energy_grid_day_kwh=2.0,
        ),
    ]
    online_map = {"wr1": True, "wr2": False}
    feed_in_periods = [
        FeedInPeriod(key="today", from_date="2026-07-14", to_date="2026-07-14", kwh=9.0),
        FeedInPeriod(key="this_week", from_date="2026-07-13", to_date="2026-07-14", kwh=40.0),
        FeedInPeriod(key="last_month", from_date="2026-06-01", to_date="2026-06-30", kwh=None),
    ]
    home_breakdown = DailyHomeBreakdownDay(
        date="2026-07-14", pv_kwh=5.0, battery_kwh=2.0, grid_kwh=1.0
    )
    battery_snapshot = [
        {"device_id": "wr1", "device_name": "Dach Sued", "battery_soc_percent": 73.4}
    ]

    subject, body = daily_report.build_report_html(
        summaries, online_map, feed_in_periods, home_breakdown, battery_snapshot,
        now, "Europe/Berlin",
    )

    assert "14.07.2026" in subject
    # Wechselrichter-Tabelle
    assert "Dach Sued" in body
    assert "12.35 kWh" in body
    assert "Garage" in body
    assert "Nicht erreichbar" in body
    assert "Aktiv" in body
    # Hero-Kacheln
    assert "15.35 kWh" in body  # Gesamt-PV-Ertrag (combined)
    assert "8.00 kWh" in body  # Gesamt-Hausverbrauch (combined)
    assert "1 / 2" in body  # aktive Wechselrichter
    # Hausverbrauch nach Quelle
    assert "5.00 kWh" in body  # PV-Anteil
    assert "2.00 kWh" in body  # Batterie-Anteil
    # Einspeisung-Tabelle
    assert "Heute" in body
    assert "9.00 kWh" in body
    assert "Diese Woche" in body
    assert "40.00 kWh" in body
    assert "Letzter Monat" in body
    assert "keine Daten" in body
    # Batterie-Ladestand
    assert "73 %" in body


def test_build_report_html_handles_missing_optional_data_gracefully():
    now = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)
    summaries = [SummaryOut(device_id="wr1", device_name="Einziger WR", yield_day_kwh=7.0)]
    subject, body = daily_report.build_report_html(
        summaries, {"wr1": True}, [], None, [], now, "Europe/Berlin"
    )
    assert "7.00 kWh" in body
    assert "Keine Batterie konfiguriert/erkannt." in body
    assert "Noch keine Daten für heute." in body


def test_build_report_html_escapes_device_names():
    now = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)
    summaries = [
        SummaryOut(device_id="wr1", device_name="<script>alert(1)</script>", yield_day_kwh=1.0)
    ]
    _subject, body = daily_report.build_report_html(
        summaries, {"wr1": True}, [], None, [], now, "Europe/Berlin"
    )
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# --- build_feed_in_summary / build_daily_home_breakdown / device_battery_snapshot -


def test_build_feed_in_summary_returns_all_nine_periods_with_no_data(client):
    periods = build_feed_in_summary()
    keys = {p.key for p in periods}
    assert keys == {
        "today", "yesterday", "day_before_yesterday",
        "this_week", "last_week", "this_month", "last_month",
        "this_year", "last_year",
    }
    assert all(p.kwh is None for p in periods)  # frische Test-DB, keine Messwerte


def test_build_daily_home_breakdown_empty_without_data(client):
    assert build_daily_home_breakdown(days=1) == []


def test_build_daily_home_breakdown_returns_objects_and_renders(client):
    """Regression: build_daily_home_breakdown muss DailyHomeBreakdownDay-
    Objekte liefern (nicht rohe Dicts), sonst schlaegt der Mail-Report mit
    'dict object has no attribute pv_kwh' fehl."""
    from datetime import datetime, timedelta, timezone

    from app.config import settings as app_settings
    from app.database import SessionLocal
    from app.models import Reading

    device_id = app_settings.inverters[0].id
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        db.add_all(
            Reading(
                device_id=device_id,
                device_name="Wechselrichter",
                timestamp=now - timedelta(minutes=m),
                pv_power_w=3000.0,
                home_power_w=1000.0,
                grid_draw_power_w=0.0,
                feed_in_power_w=2000.0,
                battery_power_w=0.0,
            )
            for m in (0, 15, 30)
        )
        db.commit()
    finally:
        db.close()

    days = build_daily_home_breakdown(days=1)
    assert days, "erwarte mindestens einen Tag mit Daten"
    today = days[-1]
    assert isinstance(today, DailyHomeBreakdownDay)
    _ = today.pv_kwh  # Attributzugriff darf nicht fehlschlagen

    # End-to-End: der Report muss mit echten Breakdown-Daten rendern.
    from datetime import datetime as _dt

    subject, body = daily_report.build_report_html(
        [], {}, [], today, [], _dt.now(timezone.utc), "Europe/Berlin",
    )
    assert "PV" in body


def test_device_battery_snapshot_skips_devices_without_known_soc(monkeypatch):
    import app.daily_summary as daily_summary_module

    class _Cfg:
        id = "wr1"
        name = "WR 1"

    monkeypatch.setattr(daily_summary_module.settings, "inverters", [_Cfg()])
    daily_summary_module.poller.latest.clear()
    daily_summary_module.poller.latest["wr1"] = {"battery_soc_percent": None}

    assert device_battery_snapshot() == []


def test_device_battery_snapshot_includes_known_soc(monkeypatch):
    import app.daily_summary as daily_summary_module

    class _Cfg:
        id = "wr1"
        name = "WR 1"

    monkeypatch.setattr(daily_summary_module.settings, "inverters", [_Cfg()])
    daily_summary_module.poller.latest.clear()
    daily_summary_module.poller.latest["wr1"] = {"battery_soc_percent": 55.0}

    result = device_battery_snapshot()
    assert result == [{"device_id": "wr1", "device_name": "WR 1", "battery_soc_percent": 55.0}]


# --- daily_report_config: Persistenz, Env-Fallback, API-Key-Geheimhaltung --


def test_get_config_falls_back_to_env_defaults_when_nothing_saved(client):
    cfg = get_config()
    # conftest.py setzt keine DAILY_REPORT_*-Env-Variablen, daher die
    # Defaults aus config.py.
    assert cfg["report_time"] == "19:00"
    assert cfg["recipients"] == []
    assert cfg["mail_service_api_key"] == ""


def test_update_config_persists_and_get_config_reflects_it(client):
    saved = update_config(
        enabled=True,
        report_time="21:30",
        recipients=["a@example.com", "b@example.com"],
        mail_service_url="http://mail-api:8080/send",
        mail_service_api_key="secret-key-123",
        mail_service_from_name="Test-Anlage",
    )
    assert saved["report_time"] == "21:30"
    assert saved["recipients"] == ["a@example.com", "b@example.com"]
    assert saved["mail_service_api_key"] == "secret-key-123"

    reloaded = get_config()
    assert reloaded == saved


def test_update_config_with_blank_api_key_keeps_previous_value(client):
    update_config(
        enabled=True,
        report_time="19:00",
        recipients=["a@example.com"],
        mail_service_url="http://mail-api:8080/send",
        mail_service_api_key="original-key",
        mail_service_from_name="",
    )
    updated = update_config(
        enabled=True,
        report_time="20:00",
        recipients=["a@example.com"],
        mail_service_url="http://mail-api:8080/send",
        mail_service_api_key=None,  # unveraendert lassen
        mail_service_from_name="",
    )
    assert updated["mail_service_api_key"] == "original-key"
    assert updated["report_time"] == "20:00"


def test_update_config_rejects_invalid_time(client):
    try:
        update_config(
            enabled=True,
            report_time="nicht-valide",
            recipients=[],
            mail_service_url="",
            mail_service_api_key=None,
            mail_service_from_name="",
        )
        assert False, "haette InvalidReportTime auslösen muessen"
    except InvalidReportTime:
        pass


# --- generate_and_send_daily_report ----------------------------------------


def test_generate_and_send_daily_report_records_success(client, monkeypatch):
    sent = {}

    async def fake_send(subject, body, *, cfg, html=False):
        sent["subject"] = subject
        sent["body"] = body
        sent["cfg"] = cfg

    update_config(
        enabled=True,
        report_time="19:00",
        recipients=["a@example.com"],
        mail_service_url="http://mail-api:8080/send",
        mail_service_api_key=None,
        mail_service_from_name="",
    )
    monkeypatch.setattr(daily_report, "build_daily_summaries", lambda: [
        SummaryOut(device_id="wr1", device_name="WR 1", yield_day_kwh=1.0)
    ])
    monkeypatch.setattr(daily_report, "device_online_map", lambda now=None: {"wr1": True})
    monkeypatch.setattr(daily_report, "send_report_mail", fake_send)

    result = asyncio.run(daily_report.generate_and_send_daily_report())

    assert result["sent"] is True
    assert sent["cfg"]["recipients"] == ["a@example.com"]
    status = daily_report.get_daily_report_status()
    assert status["last_status"] == "ok"
    assert status["last_sent_at"] is not None


def test_generate_and_send_daily_report_records_failure_without_raising(client, monkeypatch):
    async def failing_send(subject, body, *, cfg, html=False):
        raise ReportMailError("Mail-Service nicht erreichbar: boom")

    monkeypatch.setattr(daily_report, "build_daily_summaries", lambda: [])
    monkeypatch.setattr(daily_report, "device_online_map", lambda now=None: {})
    monkeypatch.setattr(daily_report, "send_report_mail", failing_send)

    result = asyncio.run(daily_report.generate_and_send_daily_report())

    assert result["sent"] is False
    assert "nicht erreichbar" in result["message"]
    status = daily_report.get_daily_report_status()
    assert status["last_status"] == "error"


def test_generate_and_send_daily_report_ignores_enabled_flag(client, monkeypatch):
    """generate_and_send_daily_report() wird auch vom manuellen
    Trigger-Endpoint genutzt - der soll unabhaengig vom "Aktiv"-Schalter
    funktionieren (zum Testen einer noch nicht aktivierten Konfiguration)."""
    sent = {}

    async def fake_send(subject, body, *, cfg, html=False):
        sent["called"] = True

    update_config(
        enabled=False,  # bewusst deaktiviert
        report_time="19:00",
        recipients=["a@example.com"],
        mail_service_url="http://mail-api:8080/send",
        mail_service_api_key=None,
        mail_service_from_name="",
    )
    monkeypatch.setattr(daily_report, "build_daily_summaries", lambda: [])
    monkeypatch.setattr(daily_report, "device_online_map", lambda now=None: {})
    monkeypatch.setattr(daily_report, "send_report_mail", fake_send)

    result = asyncio.run(daily_report.generate_and_send_daily_report())
    assert result["sent"] is True
    assert sent.get("called") is True


# --- Admin-Endpunkte (End-to-End ueber TestClient) -------------------------


def _login_admin(client) -> None:
    make_user("admin-report-test", "admin-pw", role="admin")
    res = client.post(
        "/api/auth/login", json={"username": "admin-report-test", "password": "admin-pw"}
    )
    assert res.status_code == 200


def _login_betreiber(client, username="betreiber-report-test") -> None:
    make_user(username, "pw", role="betreiber")
    res = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    assert res.status_code == 200


def test_daily_report_status_requires_login(client):
    res = client.get("/api/admin/daily-report/status")
    assert res.status_code == 401


def test_daily_report_status_visible_to_any_logged_in_user(client):
    update_config(
        enabled=True,
        report_time="19:00",
        recipients=["a@example.com"],
        mail_service_url="http://mail-api:8080/send",
        mail_service_api_key=None,
        mail_service_from_name="",
    )
    _login_betreiber(client)
    res = client.get("/api/admin/daily-report/status")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["scheduled_time"] == "19:00"
    assert body["recipients"] == ["a@example.com"]


def test_daily_report_config_requires_admin_role(client):
    _login_betreiber(client, "betreiber-config-test")
    assert client.get("/api/admin/daily-report/config").status_code == 403
    assert client.put("/api/admin/daily-report/config", json={
        "enabled": True, "report_time": "19:00",
    }).status_code == 403


def test_daily_report_config_get_never_exposes_api_key(client):
    update_config(
        enabled=True,
        report_time="19:00",
        recipients=["a@example.com"],
        mail_service_url="http://mail-api:8080/send",
        mail_service_api_key="top-secret",
        mail_service_from_name="",
    )
    _login_admin(client)
    res = client.get("/api/admin/daily-report/config")
    assert res.status_code == 200
    body = res.json()
    assert "mail_service_api_key" not in body
    assert body["mail_service_api_key_set"] is True
    assert "top-secret" not in res.text


def test_daily_report_config_put_saves_all_fields_including_recipients(client):
    _login_admin(client)
    res = client.put("/api/admin/daily-report/config", json={
        "enabled": True,
        "report_time": "18:45",
        "recipients": ["betreiber1@example.com", "zweite@example.com"],
        "mail_service_url": "http://192.168.178.50:8080/send",
        "mail_service_api_key": "brand-new-key",
        "mail_service_from_name": "Solaranlage",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["report_time"] == "18:45"
    assert body["recipients"] == ["betreiber1@example.com", "zweite@example.com"]
    assert body["mail_service_url"] == "http://192.168.178.50:8080/send"
    assert body["mail_service_api_key_set"] is True

    # Persistiert - erneutes Abrufen zeigt denselben Stand.
    res2 = client.get("/api/admin/daily-report/config")
    assert res2.json()["recipients"] == ["betreiber1@example.com", "zweite@example.com"]


def test_daily_report_config_put_rejects_invalid_time(client):
    _login_admin(client)
    res = client.put("/api/admin/daily-report/config", json={
        "enabled": True,
        "report_time": "25:99",
        "recipients": [],
    })
    assert res.status_code == 422  # Pydantic-Validierung im Schema greift schon


def test_daily_report_config_put_rejects_invalid_recipient(client):
    _login_admin(client)
    res = client.put("/api/admin/daily-report/config", json={
        "enabled": True,
        "report_time": "19:00",
        "recipients": ["keine-email-adresse"],
    })
    assert res.status_code == 422


def test_daily_report_trigger_forbidden_for_betreiber(client):
    _login_betreiber(client, "betreiber-trigger-test")
    res = client.post("/api/admin/daily-report/trigger")
    assert res.status_code == 403


def test_daily_report_trigger_sends_via_mocked_mailer(client, monkeypatch):
    from app import daily_report as daily_report_module

    async def fake_send(subject, body, *, cfg, html=False):
        fake_send.called_with = (subject, body)

    monkeypatch.setattr(daily_report_module, "send_report_mail", fake_send)
    update_config(
        enabled=True,
        report_time="19:00",
        recipients=["a@example.com"],
        mail_service_url="http://mail-api:8080/send",
        mail_service_api_key=None,
        mail_service_from_name="",
    )

    _login_admin(client)
    res = client.post("/api/admin/daily-report/trigger")

    assert res.status_code == 200
    body = res.json()
    assert body["started"] is True
    assert hasattr(fake_send, "called_with")
