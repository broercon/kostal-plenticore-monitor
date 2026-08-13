"""Gemeinsame Pytest-Fixtures fuer die Backend-Tests.

WICHTIG: Die Umgebungsvariablen fuer eine isolierte Test-Datenbank/-Konfiguration
muessen gesetzt werden, BEVOR irgendein app.*-Modul importiert wird, denn
app/config.py liest sie einmalig beim Modul-Import in das Settings()-Singleton
ein. conftest.py wird von pytest vor allen Testmodulen eines Verzeichnisses
geladen, deshalb passiert das hier ganz oben, noch vor den Imports weiter
unten.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.mkdtemp(prefix="kpm-test-"))
os.environ["DB_PATH"] = str(_TEST_DIR / "test.db")
# Zeigt bewusst auf eine nicht existierende Datei, damit Settings() auf die
# Env-Variablen-Fallback-Wechselrichter-Konfiguration unten zurueckfaellt,
# statt eine echte config/inverters.json vom Entwicklerrechner zu lesen.
os.environ["CONFIG_PATH"] = str(_TEST_DIR / "does-not-exist.json")
# 192.0.2.1 liegt im TEST-NET-1-Bereich (RFC 5737) - reserviert fuer
# Dokumentation/Tests, es gibt dort nie einen echten Wechselrichter. Poller
# und Auto-Import werden in den Tests ohnehin nicht gestartet (siehe
# client()-Fixture), diese Werte muessen nur vorhanden sein, damit
# Settings() ueberhaupt ein Geraet konfiguriert.
os.environ["INVERTER_HOST"] = "192.0.2.1"
os.environ["INVERTER_PASSWORD"] = "test-only-unused"
os.environ["FRONTEND_DIR"] = str(_TEST_DIR / "no-frontend")
os.environ["AUTO_IMPORT_HISTORY"] = "false"
os.environ["TIMEZONE"] = "Europe/Berlin"

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture()
def client():
    """Frische, leere Datenbank je Testfall + FastAPI-TestClient.

    Startet bewusst NICHT den vollen Lifespan (kein `with TestClient(...)`),
    da der sonst Poller und Auto-Import gegen den nicht existierenden
    Test-Wechselrichter (192.0.2.1) anwerfen wuerde. Stattdessen wird hier
    manuell nachgebaut, was main.py's lifespan() beim echten Start erledigt:
    Tabellen anlegen (init_db) und Default-Nutzer seeden.
    """
    Base.metadata.drop_all(bind=engine)
    init_db()
    auth.seed_default_users()
    return TestClient(fastapi_app)


# Fester Referenzzeitpunkt weit von jeder Tagesgrenze entfernt (12:00 UTC =
# 13:00/14:00 Europe/Berlin, je nach Sommerzeit) - fuer Tests, die Messwerte
# "gerade eben" einfuegen und danach ueber einen Endpunkt/eine Funktion die
# TAGES-Aggregation abrufen (z.B. /api/readings/today-summary,
# build_daily_summaries). Ohne eingefrorene Zeit wuerden solche Tests knapp
# vor lokaler Mitternacht sporadisch fehlschlagen: die Testdaten werden mit
# einem "now" kurz VOR Mitternacht eingefuegt, der Endpunkt bestimmt "heute"
# aber ueber einen eigenen, wenige Millisekunden SPAETEREN
# datetime.now()-Aufruf - liegt dazwischen die Tagesgrenze, faellt der
# frisch eingefuegte Messwert faelschlich aus "heute" heraus (siehe
# frozen_now()-Fixture unten).
FROZEN_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def frozen_now(monkeypatch):
    """Friert 'jetzt' auf FROZEN_NOW ein, fuer alle Module, die "heute"
    ueber datetime.now(...) bestimmen (app.timeutil, app.daily_summary,
    app.main) - siehe Kommentar bei FROZEN_NOW oben. Gibt FROZEN_NOW
    zurueck, damit der Test dieselbe Referenzzeit fuer seine Testdaten
    verwendet (statt eines eigenen datetime.now(timezone.utc)-Aufrufs, der
    dann leicht von der eingefrorenen Zeit abweichen wuerde).

    Nur .now() wird ueberschrieben - alles andere (combine, min,
    fromtimestamp, Konstruktor, ...) verhaelt sich wie das echte
    datetime.datetime, da _FrozenDatetime davon erbt statt es zu ersetzen.
    """
    import app.daily_summary as daily_summary_module
    import app.main as main_module
    import app.timeutil as timeutil_module

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return FROZEN_NOW.replace(tzinfo=None)
            return FROZEN_NOW.astimezone(tz)

    for module in (daily_summary_module, main_module, timeutil_module):
        monkeypatch.setattr(module, "datetime", _FrozenDatetime)

    return FROZEN_NOW


def make_user(username: str, password: str, role: str = "betreiber", must_change_password: bool = False) -> User:
    """Legt einen Nutzer mit bekanntem (nicht zufaelligem) Passwort direkt in
    der Test-Datenbank an - fuer Login-Tests braucht man ein Passwort, das
    man kennt, waehrend seed_default_users() bewusst zufaellige Passwoerter
    erzeugt (die nur im Log landen)."""
    from datetime import datetime, timezone as tz

    salt_hex, hash_hex = auth._hash_password(password)
    db = SessionLocal()
    try:
        user = User(
            username=username,
            password_salt=salt_hex,
            password_hash=hash_hex,
            role=role,
            must_change_password=must_change_password,
            created_at=datetime.now(tz.utc),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()
