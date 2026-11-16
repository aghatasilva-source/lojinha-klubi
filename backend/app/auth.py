from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import settings
from .db import connect, transaction, utc_now


_state_lock = threading.Lock()
_oauth_states: dict[str, datetime] = {}
SESSION_HOURS = 8


def oauth_enabled() -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret and settings.google_oauth_redirect_uri)


def _cleanup_states() -> None:
    now = datetime.now(timezone.utc)
    with _state_lock:
        expired = [key for key, expiry in _oauth_states.items() if expiry <= now]
        for key in expired:
            _oauth_states.pop(key, None)


def google_login_url() -> str:
    if not oauth_enabled():
        raise ValueError("O login corporativo ainda não foi configurado no servidor.")
    _cleanup_states()
    state = secrets.token_urlsafe(32)
    with _state_lock:
        _oauth_states[state] = datetime.now(timezone.utc) + timedelta(minutes=10)
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "hd": settings.google_workspace_domain,
        "prompt": "select_account",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def _consume_state(state: str) -> bool:
    _cleanup_states()
    with _state_lock:
        expiry = _oauth_states.pop(state, None)
    return bool(expiry and expiry > datetime.now(timezone.utc))


def _post_form(url: str, values: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(values).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str, access_token: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def login_from_callback(code: str, state: str) -> str:
    if not _consume_state(state):
        raise ValueError("A sessão de login expirou. Tente novamente.")
    token = _post_form("https://oauth2.googleapis.com/token", {"code": code, "client_id": settings.google_oauth_client_id, "client_secret": settings.google_oauth_client_secret, "redirect_uri": settings.google_oauth_redirect_uri, "grant_type": "authorization_code"})
    profile = _get_json("https://openidconnect.googleapis.com/v1/userinfo", token["access_token"])
    email = str(profile.get("email", "")).strip().lower()
    if not email or not profile.get("email_verified") or not email.endswith(f"@{settings.google_workspace_domain}"):
        raise ValueError("Acesso permitido somente para e-mails corporativos do Klubi.")
    role = "ADMINISTRADOR" if email in settings.admin_emails else "COLABORADOR"
    session_token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(hours=SESSION_HOURS)).isoformat()
    with transaction(immediate=True) as connection:
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now.isoformat(),))
        connection.execute("INSERT INTO auth_sessions(session_token,email,name,role,expires_at,created_at) VALUES (?,?,?,?,?,?)", (session_token, email, str(profile.get("name") or email.split("@")[0]), role, expires_at, now.isoformat()))
    return session_token


def session_from_token(session_token: str | None) -> dict | None:
    if not session_token:
        return None
    with connect() as connection:
        row = connection.execute("SELECT session_token,email,name,role,expires_at FROM auth_sessions WHERE session_token=? AND expires_at>?", (session_token, utc_now())).fetchone()
        return dict(row) if row else None


def revoke_session(session_token: str | None) -> None:
    if session_token:
        with transaction(immediate=True) as connection:
            connection.execute("DELETE FROM auth_sessions WHERE session_token=?", (session_token,))
