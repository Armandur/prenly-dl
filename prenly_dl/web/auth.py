"""Valfritt lösenordsskydd.

Databasen innehåller inloggningsuppgifter i klartext, så tjänsten bör inte
ligga öppet. Sätt PRENLY_PASSWORD för att kräva inloggning; utan variabeln
är gränssnittet öppet (rimligt bakom en egen brandvägg).
"""

from __future__ import annotations

import hmac
import os

from fastapi import Request
from fastapi.responses import RedirectResponse

PASSWORD_ENV_VAR = "PRENLY_PASSWORD"
SESSION_KEY = "authenticated"
PUBLIC_PATHS = ("/login", "/static", "/healthz")


def password() -> str:
    return os.environ.get(PASSWORD_ENV_VAR, "")


def auth_enabled() -> bool:
    return bool(password())


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate or "", password())


def is_authenticated(request: Request) -> bool:
    if not auth_enabled():
        return True
    return bool(request.session.get(SESSION_KEY))


async def require_login(request: Request, call_next):
    """Mellanvara som skickar oinloggade till inloggningssidan."""
    path = request.url.path
    if not auth_enabled() or path.startswith(PUBLIC_PATHS) or is_authenticated(request):
        return await call_next(request)
    return RedirectResponse("/login", status_code=303)
