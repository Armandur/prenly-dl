"""Jinja-filter för datum och status."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..config import timezone_name

STATUS_CLASSES = {
    "ok": "ok",
    "done": "ok",
    "new": "neutral",
    "unknown": "neutral",
    "queued": "pending",
    "running": "pending",
    "downloading": "pending",
    "auth_error": "warn",
    "subscription_error": "warn",
    "error": "bad",
    "cancelled": "neutral",
}


def local_time(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Formatera en naiv UTC-tidstämpel i konfigurerad tidszon."""
    if value is None:
        return "-"
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo(timezone_name())).strftime(fmt)


def status_class(value: str | None) -> str:
    return STATUS_CLASSES.get(value or "", "neutral")


def register_filters(env) -> None:
    env.filters["local_time"] = local_time
    env.filters["status_class"] = status_class
