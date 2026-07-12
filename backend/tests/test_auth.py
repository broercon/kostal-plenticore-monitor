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
    assert client.get("/api/auth/me").json()["must_change_password"] is False

    # Altes Passwort funktioniert nach dem Wechsel (neuer Login) nicht mehr,
    # das neue schon.
    client.post("/api/auth/logout")
    old_login = client.post(
        "/api/auth/login", json={"username": "betreiber1-test", "password": "altes-passwort"}
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/auth/login", json={"username": "betreiber1-test", "password": "neues-passwort-123"}
    )
    assert new_login.status_code == 200


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
    client.post("/api/auth/login", json={"username": "admin-test", "password": "admin-pw"})

    listing = client.get("/api/admin/users")
    assert listing.status_code == 200
    usernames = {u["username"] for u in listing.json()}
    assert "betreiber2-test" in usernames

    reset = client.post(f"/api/admin/users/{target.id}/reset-password", json={})
    assert reset.status_code == 200
    new_password = reset.json()["new_password"]
    assert new_password  # ein zufaelliges Passwort wurde erzeugt und zurueckgegeben

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
