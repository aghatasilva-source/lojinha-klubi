from __future__ import annotations

import threading
import time
from typing import Any

from clerk_backend_api import AuthenticateRequestOptions, Clerk, authenticate_request

from .config import settings


class _RequestAdapter:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


_cache_lock = threading.Lock()
_identity_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_SECONDS = 300


def clerk_configured() -> bool:
    return bool(settings.clerk_secret_key)


def _email_from_user(user: Any) -> str:
    primary_id = getattr(user, "primary_email_address_id", None)
    addresses = getattr(user, "email_addresses", []) or []
    primary = next((item for item in addresses if getattr(item, "id", None) == primary_id), None) or (addresses[0] if addresses else None)
    return str(getattr(primary, "email_address", "") or "").strip().lower()


def clerk_identity(headers: dict[str, str]) -> dict[str, Any] | None:
    if not clerk_configured():
        return None
    normalized_headers = {key.lower(): value for key, value in headers.items() if value}
    try:
        state = authenticate_request(
            _RequestAdapter(normalized_headers),
            AuthenticateRequestOptions(
                secret_key=settings.clerk_secret_key,
                authorized_parties=list(settings.clerk_authorized_parties),
                accepts_token=["session_token"],
            ),
        )
    except Exception:
        return None
    if not state.is_signed_in or not state.payload:
        return None
    user_id = str(state.payload.get("sub", ""))
    if not user_id:
        return None
    now = time.monotonic()
    with _cache_lock:
        cached = _identity_cache.get(user_id)
        if cached and cached[0] > now:
            return cached[1]
    try:
        user = Clerk(bearer_auth=settings.clerk_secret_key).users.get(user_id=user_id)
        email = _email_from_user(user)
        if not email or not email.endswith(f"@{settings.clerk_workspace_domain}"):
            return None
        first_name = str(getattr(user, "first_name", "") or "").strip()
        last_name = str(getattr(user, "last_name", "") or "").strip()
        identity = {
            "provider": "clerk",
            "user_id": user_id,
            "email": email,
            "name": " ".join(part for part in (first_name, last_name) if part) or email.split("@", 1)[0],
            "role": "ADMINISTRADOR" if email in settings.admin_emails else "COLABORADOR",
        }
    except Exception:
        return None
    with _cache_lock:
        _identity_cache[user_id] = (now + _CACHE_SECONDS, identity)
    return identity
