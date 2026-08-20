"""Körtidskonfiguration: sökvägar och miljövariabler."""

from __future__ import annotations

import os
from pathlib import Path

DB_ENV_VAR = "PRENLY_DB"
OUTPUT_ENV_VAR = "PRENLY_OUTPUT"
TZ_ENV_VAR = "PRENLY_TZ"
SECRET_ENV_VAR = "PRENLY_SECRET_KEY"

DEFAULT_DB = "data/prenly.db"
DEFAULT_OUTPUT = "output"
DEFAULT_TZ = "Europe/Stockholm"


def db_path() -> Path:
    return Path(os.environ.get(DB_ENV_VAR, DEFAULT_DB)).expanduser().resolve()


def output_root() -> Path:
    return Path(os.environ.get(OUTPUT_ENV_VAR, DEFAULT_OUTPUT)).expanduser().resolve()


def timezone_name() -> str:
    return os.environ.get(TZ_ENV_VAR, DEFAULT_TZ)


def secret_key() -> str:
    return os.environ.get(SECRET_ENV_VAR, "byt-mig-i-produktion")
