"""Sessionsfabrik och schemainitiering för SQLite-databasen."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import db_path
from .models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _make_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        # WAL så webbtjänsten kan läsa medan schemaläggaren skriver.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


# SQLite-typer för de kolumntyper modellerna använder.
_SQLITE_TYPER = {
    "INTEGER": "INTEGER",
    "VARCHAR": "VARCHAR",
    "TEXT": "TEXT",
    "BOOLEAN": "BOOLEAN",
    "DATETIME": "DATETIME",
}


def _add_missing_columns(engine: Engine) -> list[str]:
    """Lägg till kolumner som finns i modellerna men saknas i databasen.

    create_all() skapar bara tabeller som saknas helt, så ett nytt fält på
    en befintlig tabell skulle annars kräva att databasen raderades. Räcker
    för nya nullbara kolumner med default - kolumnbyten och borttagningar
    hanteras inte, och skulle behöva ett riktigt migrationsverktyg.
    """
    tillagda: list[str] = []
    inspector = inspect(engine)
    befintliga_tabeller = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in befintliga_tabeller:
                continue
            har = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in har:
                    continue
                typnamn = column.type.compile(dialect=engine.dialect)
                sats = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {typnamn}"
                default = column.default
                if default is not None and not callable(getattr(default, "arg", None)):
                    varde = default.arg
                    if isinstance(varde, bool):
                        sats += f" DEFAULT {1 if varde else 0}"
                    elif isinstance(varde, int):
                        sats += f" DEFAULT {varde}"
                    elif isinstance(varde, str):
                        sats += f" DEFAULT '{varde}'"
                connection.execute(text(sats))
                tillagda.append(f"{table.name}.{column.name}")
    return tillagda


def make_session_factory(path: Path | None = None) -> sessionmaker[Session]:
    """Skapa (eller återanvänd) sessionsfabriken och se till att schemat finns."""
    global _engine, _session_factory
    target = Path(path) if path else db_path()
    if _session_factory is None or _engine is None or _engine.url.database != str(target):
        _engine = _make_engine(target)
        Base.metadata.create_all(_engine)
        tillagda = _add_missing_columns(_engine)
        if tillagda:
            logger.info("Lade till saknade kolumner: %s", ", ".join(tillagda))
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        logger.info("Databas: %s", target)
    return _session_factory


def init_db(path: Path | None = None) -> None:
    make_session_factory(path)


@contextmanager
def get_session(path: Path | None = None) -> Iterator[Session]:
    """Sessionskontext som committar vid lyckat utfall och rullar tillbaka vid fel."""
    factory = make_session_factory(path)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
