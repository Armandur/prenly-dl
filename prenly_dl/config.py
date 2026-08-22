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


def scheduler_enabled() -> bool:
    """Om schemat ska köra. Av med PRENLY_NO_SCHEDULER=1.

    Två instanser mot samma Calibre skapade dubbletter: båda hämtade
    dagens nummer och la in var sin bok, eftersom ingen kände till den
    andras bok-id. En utvecklingsinstans bredvid den skarpa ska kunna
    köras utan att göra samma jobb.
    """
    return os.environ.get("PRENLY_NO_SCHEDULER", "").strip().lower() not in (
        "1", "true", "ja", "yes",
    )


def timezone_name() -> str:
    return os.environ.get(TZ_ENV_VAR, DEFAULT_TZ)


def secret_key() -> str:
    return os.environ.get(SECRET_ENV_VAR, "byt-mig-i-produktion")
