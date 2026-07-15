"""Tests fuer den Poller-Watchdog: Stall-Erkennung und Aktualisierung des
Zeitstempels des letzten erfolgreichen Abrufs."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.models import Reading
from app.poller import Poller


class _FakeDevice:
    def __init__(self, device_id="wrX"):
        self.cfg = SimpleNamespace(id=device_id, name="Fake")

    async def fetch_reading(self):
        return {
            "device_id": self.cfg.id,
            "device_name": self.cfg.name,
            "timestamp": datetime.now(timezone.utc),
            "pv_power_w": 1000.0,
        }

    async def close(self):
        pass


def test_should_restart_detects_stall():
    p = Poller()
    p._stall_restart_seconds = 300
    now = time.monotonic()
    p._last_success = now
    assert p._should_restart(now) is False
    assert p._should_restart(now + 299) is False
    assert p._should_restart(now + 301) is True


def test_poll_once_updates_last_success_and_stores(client):
    from app.database import SessionLocal

    p = Poller()
    p._last_success = time.monotonic() - 9999  # kuenstlich "veraltet"
    asyncio.run(p._poll_once(_FakeDevice()))

    # Zeitstempel wurde durch den erfolgreichen Abruf aufgefrischt.
    assert time.monotonic() - p._last_success < 5
    # und der Messwert wurde gespeichert.
    db = SessionLocal()
    try:
        rows = list(db.scalars(select(Reading).where(Reading.device_id == "wrX")))
    finally:
        db.close()
    assert len(rows) == 1


def test_notify_stall_sends_mail_once(client, monkeypatch):
    """Bei einem Haenger wird EINE Mail mit den Report-Zugangsdaten
    verschickt, die auf die Logdatei verweist."""
    import app.poller as poller_mod

    sent = []

    async def fake_send(subject, body, *, cfg, html=False):
        sent.append({"subject": subject, "body": body, "cfg": cfg, "html": html})

    monkeypatch.setattr("app.report_mailer.send_report_mail", fake_send)
    monkeypatch.setattr(
        "app.daily_report_config.get_config",
        lambda: {"recipients": ["a@b.de"], "mail_service_url": "http://x/send"},
    )

    p = poller_mod.Poller()
    asyncio.run(p._notify_stall())

    assert len(sent) == 1
    assert "Polling" in sent[0]["subject"]
    assert "app.log" in sent[0]["body"]
    assert sent[0]["cfg"]["recipients"] == ["a@b.de"]
