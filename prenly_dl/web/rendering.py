"""Delad mallrendering, flash-meddelanden och omdirigeringar."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..db.models import AuthStatus
from .auth import auth_enabled
from .filters import register_filters

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
register_filters(templates.env)

STATUS_LABELS = {
    AuthStatus.OK.value: "OK",
    AuthStatus.UNKNOWN.value: "Okänd",
    AuthStatus.AUTH_ERROR.value: "Utloggad",
    AuthStatus.SUBSCRIPTION_ERROR.value: "Ingen prenumeration",
    AuthStatus.ERROR.value: "Fel",
}


def flash(request: Request, message: str, level: str = "info") -> None:
    messages = request.session.setdefault("flashes", [])
    messages.append({"text": message, "level": level})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flashes", [])


def render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("flashes", pop_flashes(request))
    context.setdefault("auth_enabled", auth_enabled())
    context.setdefault("status_labels", STATUS_LABELS)
    return templates.TemplateResponse(request, template, context)


def back(url: str = "/") -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
