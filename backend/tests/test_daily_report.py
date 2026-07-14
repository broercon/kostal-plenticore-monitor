"""Tests fuer den taeglichen Zusammenfassungs-Report per Mail
(app/daily_report.py, app/daily_summary.py, app/report_mailer.py).

Deckt ab: Berechnung des naechsten Sendezeitpunkts (next_run_at), den
"aktiv/erreichbar"-Status je Wechselrichter (device_online_map), das
Text-Format der Mail (build_report_text), dass ein fehlgeschlagener
Mailversand abgefangen und in _state vermerkt wird statt den Aufrufer
crashen zu lassen (generate_and_send_daily_report), sowie die beiden neuen
Admin-Endpunkte End-to-End ueber den FastAPI-TestClient.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import daily_report
from app.daily_summary import device_online_map
from app.report_mailer import ReportMailError
from app.schemas import SummaryOut

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


def test_build_report_text_lists_devices_and_totals():
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

    subject, body = daily_report.build_report_text(summaries, online_map, now, "Europe/Berlin")

    assert "14.07.2026" in subject
    assert "Dach Sued" in body
    assert "12.35 kWh" in body  # gerundet
    assert "Garage" in body
    assert "NICHT ERREICHBAR" in body
    assert "keine Daten" in body  # Garage hat keinen PV-Ertragswert
    assert "Gesamt-PV-Ertrag heute (alle Wechselrichter): 15.35 kWh" in body
    assert "Hausverbrauch heute: 8.00 kWh" in body  # nur hausweit, nicht je Geraet
    assert "1 von 2 Wechselrichter(n) aktiv." in body


def test_build_report_text_without_multiple_inverters_has_no_combined_line():
    now = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)
    summaries = [
        SummaryOut(device_id="wr1", device_name="Einziger WR", yield_day_kwh=7.0)
    ]
    subject, body = daily_report.build_report_text(summaries, {"wr1": True}, now, "Europe/Berlin")
    assert "Gesamt-PV-Ertrag" not in body
    assert "PV-Ertrag heute: 7.00 kWh" in body
    assert "1 von 1 Wechselrichter(n) aktiv." in body


# --- generate_and_send_daily_report ----------------------------------------


def test_generate_and_send_daily_report_records_success(monkeypatch):
    sent = {}

    async def fake_send(subject, body, *, html=False):
        sent["subject"] = subject
        sent["body"] = body

    monkeypatch.setattr(daily_report, "build_daily_summaries", lambda: [
        SummaryOut(device_id="wr1", device_name="WR 1", yield_day_kwh=1.0)
    ])
    monkeypatch.setattr(daily_report, "device_online_map", lambda now=None: {"wr1": True})
    monkeypatch.setattr(daily_report, "send_report_mail", fake_send)
    monkeypatch.setattr(daily_report.settings, "daily_report_recipients", ["a@example.com"])

    result = asyncio.run(daily_report.generate_and_send_daily_report())

    assert result["sent"] is True
    assert "subject" in sent
    status = daily_report.get_daily_report_status()
    assert status["last_status"] == "ok"
    assert status["last_sent_at"] is not None


def test_generate_and_send_daily_report_records_failure_without_raising(monkeypatch):
    async def failing_send(subject, body, *, html=False):
        raise ReportMailError("Mail-Service nicht erreichbar: boom")

    monkeypatch.setattr(daily_report, "build_daily_summaries", lambda: [])
    monkeypatch.setattr(daily_report, "device_online_map", lambda now=None: {})
    monkeypatch.setattr(daily_report, "send_report_mail", failing_send)

    result = asyncio.run(daily_report.generate_and_send_daily_report())

    assert result["sent"] is False
    assert "nicht erreichbar" in result["message"]
    status = daily_report.get_daily_report_status()
    assert status["last_status"] == "error"


# --- Admin-Endpunkte (End-to-End ueber TestClient) -------------------------


def _login_admin(client) -> None:
    make_user("admin-report-test", "admin-pw", role="admin")
    res = client.post(
        "/api/auth/login", json={"username": "admin-report-test", "password": "admin-pw"}
    )
    assert res.status_code == 200


def test_daily_report_status_requires_login(client):
    res = client.get("/api/admin/daily-report/status")
    assert res.status_code == 401


def test_daily_report_status_reflects_configuration(client, monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(main_module.settings, "daily_report_enabled", True)
    monkeypatch.setattr(main_module.settings, "daily_report_recipients", ["a@example.com"])
    monkeypatch.setattr(main_module.settings, "mail_service_url", "http://mail-api:8080/send")
    monkeypatch.setattr(main_module.settings, "daily_report_time", "19:00")

    make_user("betreiber-report-test", "pw", role="betreiber")
    client.post("/api/auth/login", json={"username": "betreiber-report-test", "password": "pw"})

    res = client.get("/api/admin/daily-report/status")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["scheduled_time"] == "19:00"
    assert body["recipients"] == ["a@example.com"]


def test_daily_report_trigger_forbidden_for_betreiber(client):
    make_user("betreiber-trigger-test", "pw", role="betreiber")
    client.post("/api/auth/login", json={"username": "betreiber-trigger-test", "password": "pw"})
    res = client.post("/api/admin/daily-report/trigger")
    assert res.status_code == 403


def test_daily_report_trigger_sends_via_mocked_mailer(client, monkeypatch):
    from app import daily_report as daily_report_module

    async def fake_send(subject, body, *, html=False):
        fake_send.called_with = (subject, body)

    monkeypatch.setattr(daily_report_module, "send_report_mail", fake_send)
    monkeypatch.setattr(
        daily_report_module.settings, "daily_report_recipients", ["a@example.com"]
    )

    _login_admin(client)
    res = client.post("/api/admin/daily-report/trigger")

    assert res.status_code == 200
    body = res.json()
    assert body["started"] is True
    assert hasattr(fake_send, "called_with")
