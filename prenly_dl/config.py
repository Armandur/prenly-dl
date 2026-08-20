"""Körtidskonfiguration: sökvägar och miljövariabler."""

from __future__ import annotations

import os
from pathlib import Path

DB_ENV_VAR = "PRENLY_DB"
OUTPUT_ENV_VAR = "PRENLY_OUTPUT"
TZ_ENV_VAR = "PRENLY_TZ"
SECRET_ENV_VAR = "PRENLY_SECRET_KEY"
POLL_ENV_VAR = "PRENLY_POLL_INTERVAL"

DEFAULT_DB = "data/prenly.db"
DEFAULT_OUTPUT = "output"
DEFAULT_TZ = "Europe/Stockholm"
DEFAULT_POLL_MINUTES = 360


def db_path() -> Path:
    return Path(os.environ.get(DB_ENV_VAR, DEFAULT_DB)).expanduser().resolve()


def output_root() -> Path:
    return Path(os.environ.get(OUTPUT_ENV_VAR, DEFAULT_OUTPUT)).expanduser().resolve()


def timezone_name() -> str:
    return os.environ.get(TZ_ENV_VAR, DEFAULT_TZ)


def secret_key() -> str:
    return os.environ.get(SECRET_ENV_VAR, "byt-mig-i-produktion")


def poll_interval_minutes() -> int:
    try:
        return max(5, int(os.environ.get(POLL_ENV_VAR, DEFAULT_POLL_MINUTES)))
    except ValueError:
        return DEFAULT_POLL_MINUTES
