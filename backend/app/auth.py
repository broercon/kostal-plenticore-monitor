"""Einfache Benutzerverwaltung: Login/Logout, Passwoerter, Rollen (admin /
betreiber), Session-Cookies.

Bewusst ohne externe Auth-Bibliothek gebaut (kein passlib/bcrypt/JWT), um
keine zusaetzliche Abhaengigkeit mit Compile-Schritt in ein schlankes
Docker-Image ziehen zu muessen:

- Passwoerter: PBKDF2-HMAC-SHA256 (Python-Standardbibliothek, `hashlib`),
  200.000 Iterationen, individueller Salt je Nutzer.
- Sessions: zufaelliges Token (`secrets.token_urlsafe`), serverseitig in
  der Datenbank gespeichert (Tabelle `sessions`) mit Ablaufzeit - dadurch
  ueberleben Logins einen Container-Neustart, und ein Logout/Passwort-
  Wechsel kann das Token gezielt loeschen/invalidieren.
- Das Session-Token wird als httponly-Cookie gesetzt (SameSite=Lax).

Beim allerersten Start (leere `users`-Tabelle) werden automatisch drei
Nutzer angelegt: "admin" (Rolle admin), "betreiber1" und "betreiber2" (Rolle
betreiber) - mit zufaelligen Initial-Passwoertern, die EINMALIG klar im Log
ausgegeben werden (siehe seed_default_users). Bitte nach dem ersten Login
sofort aendern.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .database import SessionLocal
from .models import Session as SessionModel
from .models import User

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 200_000
SESSION_COOKIE_NAME = "kpm_session"
SESSION_MAX_AGE_DAYS = 30

ROLE_ADMIN = "admin"
ROLE_BETREIBER = "betreiber"


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return secrets.compare_digest(dk.hex(), hash_hex)


def _generate_temp_password() -> str:
    """Gut lesbares Zufallspasswort (keine mehrdeutigen Zeichen wie 0/O/1/l)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(14))


def seed_default_users() -> None:
    """Legt beim allerersten Start die drei vorgesehenen Nutzer an, falls die
    users-Tabelle noch leer ist. Initial-Passwoerter werden zufaellig
    generiert und einmalig im Log ausgegeben - das ist der einzige Weg, sie
    zu erfahren, also unbedingt in den Logs (`docker compose logs`)
    nachsehen und danach ueber "Passwort aendern" ersetzen."""
    session = SessionLocal()
    try:
        existing = session.scalar(select(User).limit(1))
        if existing is not None:
            return

        seed_spec = [
            ("admin", ROLE_ADMIN),
            ("betreiber1", ROLE_BETREIBER),
            ("betreiber2", ROLE_BETREIBER),
        ]
        lines = [
            "=" * 70,
            "ERSTE ANMELDEDATEN (nur jetzt im Log sichtbar - bitte notieren",
            "und nach dem ersten Login ueber \"Passwort aendern\" ersetzen):",
        ]
        now = datetime.now(timezone.utc)
        for username, role in seed_spec:
            temp_password = _generate_temp_password()
            salt_hex, hash_hex = _hash_password(temp_password)
            session.add(
                User(
                    username=username,
                    password_salt=salt_hex,
                    password_hash=hash_hex,
                    role=role,
                    must_change_password=True,
                    created_at=now,
                )
            )
            lines.append(f"  Benutzername: {username:10s}  Passwort: {temp_password}")
        lines.append("=" * 70)
        session.commit()
        for line in lines:
            logger.warning(line)
    finally:
        session.close()


def _create_session(db: OrmSession, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.add(
        SessionModel(
            token=token,
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(days=SESSION_MAX_AGE_DAYS),
        )
    )
    db.commit()
    return token


def login(username: str, password: str, response: Response) -> User | None:
    """Prueft Zugangsdaten, legt bei Erfolg eine Session an und setzt das
    Cookie. Gibt den User zurueck, oder None bei falschen Zugangsdaten."""
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.username == username))
        if user is None or not _verify_password(password, user.password_salt, user.password_hash):
            return None
        token = _create_session(db, user)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_MAX_AGE_DAYS * 24 * 3600,
            path="/",
        )
        # _create_session() committet, wodurch SQLAlchemy die Attribute von
        # user als "expired" markiert (werden erst bei Zugriff neu geladen).
        # Da wir die Session gleich schliessen, hier explizit neu laden und
        # dann vom Session-Objekt loesen (expunge), damit der Aufrufer
        # (main.py) nach dem Schliessen noch gefahrlos user.id/.username/...
        # lesen kann, ohne DetachedInstanceError.
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


def logout(token: str | None, response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    if not token:
        return
    db = SessionLocal()
    try:
        db.query(SessionModel).filter(SessionModel.token == token).delete()
        db.commit()
    finally:
        db.close()


def get_current_user(
    kpm_session: str | None = Cookie(default=None),
) -> User:
    """FastAPI-Dependency: liefert den eingeloggten User oder wirft 401."""
    if not kpm_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nicht angemeldet.")

    db = SessionLocal()
    try:
        sess = db.get(SessionModel, kpm_session)
        if sess is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sitzung ungueltig.")
        expires_at = sess.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            db.delete(sess)
            db.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sitzung abgelaufen.")

        user = db.get(User, sess.user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nutzer nicht gefunden.")
        # Losgeloest von der Session-DB-Instanz zurueckgeben, damit der
        # Aufrufer damit arbeiten kann, auch nachdem diese DB-Session
        # geschlossen wurde.
        db.expunge(user)
        return user
    finally:
        db.close()


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Nur fuer Admins.")
    return user


def change_own_password(user_id: int, current_password: str, new_password: str) -> bool:
    """Aendert das eigene Passwort, nach Pruefung des aktuellen Passworts.
    Gibt False zurueck, wenn das aktuelle Passwort falsch ist."""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None or not _verify_password(current_password, user.password_salt, user.password_hash):
            return False
        salt_hex, hash_hex = _hash_password(new_password)
        user.password_salt = salt_hex
        user.password_hash = hash_hex
        user.must_change_password = False
        db.commit()
        return True
    finally:
        db.close()


def admin_reset_password(target_user_id: int, new_password: str | None) -> tuple[User, str] | None:
    """Setzt das Passwort eines anderen Nutzers direkt (ohne dessen altes
    Passwort zu kennen) - nur fuer Admins gedacht. Der betroffene Nutzer
    muss es beim naechsten Login aendern (must_change_password=True). Wird
    kein neues Passwort uebergeben, wird eines zufaellig generiert und
    zurueckgegeben (damit der Admin es weitergeben kann)."""
    db = SessionLocal()
    try:
        user = db.get(User, target_user_id)
        if user is None:
            return None
        effective_password = new_password or _generate_temp_password()
        salt_hex, hash_hex = _hash_password(effective_password)
        user.password_salt = salt_hex
        user.password_hash = hash_hex
        user.must_change_password = True
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user, effective_password
    finally:
        db.close()


def list_users() -> list[User]:
    db = SessionLocal()
    try:
        users = list(db.scalars(select(User).order_by(User.id)))
        for u in users:
            db.expunge(u)
        return users
    finally:
        db.close()
