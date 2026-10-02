"""Tests fuer die Benutzerverwaltung: Seeding, Login/Logout, Passwort-Aenderung,
Rollen-Absicherung (admin vs. betreiber) und Session-Cookies.

End-to-end ueber den FastAPI-TestClient (echte HTTP-Requests gegen die
ASGI-App inkl. echter Passwort-Hashes/Cookies), nicht nur einzelne
Funktionsaufrufe - damit genau das getestet wird, was das Frontend auch
tatsaechlich erlebt (Statuscodes, Cookie-Verhalten, Fehlermeldungen).
"""
from __future__ import annotations

from app import auth

from .conftest import make_user


def test_seed_default_users_creates_exactly_three_users_once(client):
    users = {u.username: u for u in auth.list_users()}
    assert set(users) == {"admin", "betreiber1", "betreiber2"}
    assert users["admin"].role == auth.ROLE_ADMIN
    assert users["betreiber1"].role == auth.ROLE_BETREIBER
    assert users["betreiber2"].role == auth.ROLE_BETREIBER
    # Alle Seed-Nutzer muessen ihr (zufaelliges) Initial-Passwort aendern.
    assert all(u.must_change_password for u in users.values())

    # Erneuter Aufruf darf keine Duplikate anlegen (nur beim allerersten
    # Start, bei leerer users-Tabelle, greift das Seeding).
    auth.seed_default_users()
    assert len(auth.list_users()) == 3


def test_unauthenticated_request_is_rejected(client):
    res = client.get("/api/devices")
    assert res.status_code == 401


def test_login_with_wrong_password_is_rejected(client):
    make_user("betreiber1-test", "correct-horse-battery-staple")
    res = client.post(
        "/api/auth/login",
        json={"username": "betreiber1-test", "password": "wrong-password"},
    )
    assert res.status_code == 401
    assert "kpm_session" not in res.cookies


def test_login_with_unknown_username_is_rejected(client):
    res = client.post(
        "/api/auth/login", json={"username": "does-not-exist", "password": "whatever"}
    )
    assert res.status_code == 401


def test_login_success_sets_cookie_and_grants_access(client):
    make_user("betreiber1-test", "correct-horse-battery-staple", role="betreiber")

    res = client.post(
        "/api/auth/login",
        json={"username": "betreiber1-test", "password": "correct-horse-battery-staple"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "betreiber1-test"
    assert body["role"] == "betreiber"
    assert "kpm_session" in res.cookies

    # Mit der Session (TestClient haelt Cookies automatisch ueber Requests
    # hinweg) sind jetzt auch vorher gesperrte Endpunkte erreichbar.
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "betreiber1-test"

    devices = client.get("/api/devices")
    assert devices.status_code == 200


def test_session_cookie_secure_flag_follows_setting(client, monkeypatch):
    make_user("secure-test", "correct-horse-battery-staple", role="betreiber")
    creds = {"username": "secure-test", "password": "correct-horse-battery-staple"}

    monkeypatch.setattr(auth.settings, "cookie_secure", False)
    res = client.post("/api/auth/login", json=creds)
    assert "secure" not in res.headers["set-cookie"].lower()

    monkeypatch.setattr(auth.settings, "cookie_secure", True)
    res = client.post("/api/auth/login", json=creds)
    cookie = res.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie


def test_logout_invalidates_session(client):
    make_user("betreiber1-test", "geheim123")
    client.post("/api/auth/login", json={"username": "betreiber1-test", "password": "geheim123"})
    assert client.get("/api/auth/me").status_code == 200

    logout_res = client.post("/api/auth/logout")
    assert logout_res.status_code == 200

    assert client.get("/api/auth/me").status_code == 401


def test_change_own_password_flow(client):
    make_user("betreiber1-test", "altes-passwort", must_change_password=True)
    client.post("/api/auth/login", json={"username": "betreiber1-test", "password": "altes-passwort"})
    assert client.get("/api/auth/me").json()["must_change_password"] is True

    # Falsches aktuelles Passwort wird abgelehnt.
    wrong = client.post(
        "/api/auth/change-password",
        json={"current_password": "falsch", "new_password": "neues-passwort-123"},
    )
    assert wrong.status_code == 400

    ok = client.post(
        "/api/auth/change-password",
        json={"current_password": "altes-passwort", "new_password": "neues-passwort-123"},
    )
    assert ok.status_code == 200

    # Der Wechsel invalidiert aus Sicherheitsgruenden alle bisherigen
    # Sitzungen, auch die aktuelle. Fuer den weiteren Zugriff ist eine neue
    # Anmeldung mit dem neuen Passwort erforderlich.
    assert client.get("/api/auth/me").status_code == 401

    # Altes Passwort funktioniert nach dem Wechsel nicht mehr, das neue schon.
    old_login = client.post(
        "/api/auth/login", json={"username": "betreiber1-test", "password": "altes-passwort"}
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/auth/login", json={"username": "betreiber1-test", "password": "neues-passwort-123"}
    )
    assert new_login.status_code == 200



def test_change_password_rejects_too_short_new_password(client):
    make_user("betreiber1-test", "altes-passwort")
    client.post("/api/auth/login", json={"username": "betreiber1-test", "password": "altes-passwort"})

    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "altes-passwort", "new_password": "zu-kurz"},
    )
    assert res.status_code == 422
    # Bei abgelehnter Aenderung bleibt die bestehende Sitzung gueltig.
    assert client.get("/api/auth/me").status_code == 200


def test_admin_endpoints_forbidden_for_betreiber(client):
    make_user("betreiber2-test", "betreiber2-pw", role="betreiber")
    client.post("/api/auth/login", json={"username": "betreiber2-test", "password": "betreiber2-pw"})

    # Normale Datenendpunkte bleiben erreichbar ...
    assert client.get("/api/devices").status_code == 200
    # ... aber die Benutzerverwaltung ist Admins vorbehalten.
    assert client.get("/api/admin/users").status_code == 403


def test_admin_can_list_and_reset_other_users_password(client):
    target = make_user("betreiber2-test", "betreiber2-pw", role="betreiber")
    make_user("admin-test", "admin-pw", role="admin")

    # Eine bereits bestehende Sitzung des Zielnutzers simulieren.
    client.post("/api/auth/login", json={"username": "betreiber2-test", "password": "betreiber2-pw"})
    client.post("/api/auth/login", json={"username": "admin-test", "password": "admin-pw"})

    listing = client.get("/api/admin/users")
    assert listing.status_code == 200
    usernames = {u["username"] for u in listing.json()}
    assert "betreiber2-test" in usernames

    reset = client.post(f"/api/admin/users/{target.id}/reset-password", json={})
    assert reset.status_code == 200
    new_password = reset.json()["new_password"]
    assert new_password  # ein zufaelliges Passwort wurde erzeugt und zurueckgegeben

    # Der Reset invalidiert auch alle bereits offenen Sitzungen des Nutzers.
    from app.database import SessionLocal
    from app.models import Session as SessionModel

    db = SessionLocal()
    try:
        assert db.query(SessionModel).filter(SessionModel.user_id == target.id).count() == 0
    finally:
        db.close()

    # Mit dem alten Passwort geht nach dem Reset nichts mehr, mit dem neuen schon.
    client.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login", json={"username": "betreiber2-test", "password": "betreiber2-pw"}
        ).status_code
        == 401
    )
    relogin = client.post(
        "/api/auth/login", json={"username": "betreiber2-test", "password": new_password}
    )
    assert relogin.status_code == 200
    assert relogin.json()["must_change_password"] is True


def test_admin_reset_password_for_unknown_user_returns_404(client):
    make_user("admin-test", "admin-pw", role="admin")
    client.post("/api/auth/login", json={"username": "admin-test", "password": "admin-pw"})

    res = client.post("/api/admin/users/999999/reset-password", json={})
    assert res.status_code == 404


# --- Haertung vor der Veroeffentlichung ---------------------------------


def _bad_login(client, username="throttle-test", password="falsch"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_login_is_throttled_after_too_many_failures(client):
    make_user("throttle-test", "correct-horse-battery-staple")

    for _ in range(auth.LOGIN_MAX_FAILURES):
        assert _bad_login(client).status_code == 401

    res = _bad_login(client)
    assert res.status_code == 429
    assert int(res.headers["retry-after"]) > 0

    # Auch das RICHTIGE Passwort wird waehrend der Sperre abgelehnt, sonst
    # liesse sich die Sperre durch Weiterprobieren umgehen.
    ok = client.post(
        "/api/auth/login",
        json={"username": "throttle-test", "password": "correct-horse-battery-staple"},
    )
    assert ok.status_code == 429
    assert "kpm_session" not in ok.cookies


def test_login_throttle_is_per_username_and_case_insensitive(client):
    make_user("throttle-test", "correct-horse-battery-staple")
    make_user("other-user", "correct-horse-battery-staple")

    for i in range(auth.LOGIN_MAX_FAILURES):
        _bad_login(client, username="Throttle-Test" if i % 2 else "throttle-test")
    assert _bad_login(client, username="THROTTLE-TEST").status_code == 429

    # Andere Nutzer sind nicht betroffen.
    ok = client.post(
        "/api/auth/login",
        json={"username": "other-user", "password": "correct-horse-battery-staple"},
    )
    assert ok.status_code == 200


def test_login_throttle_expires_after_window(client, monkeypatch):
    make_user("throttle-test", "correct-horse-battery-staple")
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])

    for _ in range(auth.LOGIN_MAX_FAILURES):
        _bad_login(client)
    assert _bad_login(client).status_code == 429

    clock[0] += auth.LOGIN_WINDOW_SECONDS + 1
    ok = client.post(
        "/api/auth/login",
        json={"username": "throttle-test", "password": "correct-horse-battery-staple"},
    )
    assert ok.status_code == 200


def test_successful_login_resets_failure_counter(client):
    make_user("throttle-test", "correct-horse-battery-staple")
    creds = {"username": "throttle-test", "password": "correct-horse-battery-staple"}

    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        _bad_login(client)
    assert client.post("/api/auth/login", json=creds).status_code == 200

    # Zaehler wurde zurueckgesetzt: erneut fast bis zur Grenze moeglich.
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        assert _bad_login(client).status_code == 401


def test_unknown_username_still_runs_password_hashing(client, monkeypatch):
    """Gegen Timing-Unterschiede: auch bei unbekanntem Namen wird ein
    PBKDF2-Lauf ausgefuehrt, nicht sofort abgebrochen."""
    calls = []
    real = auth._verify_password

    def spy(password, salt_hex, hash_hex):
        calls.append(salt_hex)
        return real(password, salt_hex, hash_hex)

    monkeypatch.setattr(auth, "_verify_password", spy)
    res = client.post("/api/auth/login", json={"username": "gibt-es-nicht", "password": "x"})
    assert res.status_code == 401
    assert calls == [auth._DUMMY_SALT_HEX]


def test_must_change_password_blocks_data_endpoints_but_not_password_change(client):
    make_user("pending-user", "initial-passwort-1234", must_change_password=True)
    make_user("pending-admin", "initial-passwort-1234", role="admin", must_change_password=True)

    client.post("/api/auth/login", json={"username": "pending-user", "password": "initial-passwort-1234"})
    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/api/devices").status_code == 403
    assert client.get("/api/readings/latest").status_code == 403

    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "pending-admin", "password": "initial-passwort-1234"})
    assert client.get("/api/admin/users").status_code == 403

    change = client.post(
        "/api/auth/change-password",
        json={"current_password": "initial-passwort-1234", "new_password": "neues-passwort-5678"},
    )
    assert change.status_code == 200

    client.post("/api/auth/login", json={"username": "pending-admin", "password": "neues-passwort-5678"})
    assert client.get("/api/devices").status_code == 200
    assert client.get("/api/admin/users").status_code == 200


def test_api_docs_and_openapi_schema_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        res = client.get(path)
        assert res.status_code == 404, path


def test_every_api_route_requires_authentication():
    """Sicherheitsnetz gegen neue Endpunkte ohne Login: Ausser dem Login/Logout
    muss jede /api-Route get_current_user (oder die Passwort-Wechsel-Variante)
    in ihrer Dependency-Kette haben."""
    from app.main import app

    public = {("/api/auth/login", "POST"), ("/api/auth/logout", "POST")}
    guards = {auth.get_current_user, auth.get_current_user_allow_password_change}

    def chain(dep):
        yield dep.call
        for sub in dep.dependencies:
            yield from chain(sub)

    unprotected = []
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None or not route.path.startswith("/api/"):
            continue
        for method in route.methods:
            if (route.path, method) in public:
                continue
            if not guards & set(chain(dependant)):
                unprotected.append((route.path, method))
    assert unprotected == []
